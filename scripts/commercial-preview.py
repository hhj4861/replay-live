#!/usr/bin/env python3
"""Keep a loopback-only commercial UI preview open until Ctrl+C.

Uses temporary SQLite/media and the real API/worker. The default login is a
development adapter; --google uses real Google identity verification instead.
This is not a production deployment test. No external streaming is allowed.
Existing development servers and application source files are not changed.
"""
import argparse
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn
from cryptography.fernet import Fernet
from fastapi.responses import JSONResponse
from sqlalchemy import select

from server.auth import AuthConfig, JWTAuthenticator
from server.google_auth import GoogleAuthConfig, GoogleAuthenticator
from server.media_runtime import validate_media
from server.production_app import create_production_app
from server.repository import Repository, jobs, NONTERMINAL
from server.request_boundary import BoundaryMiddleware
from server.secrets import LocalKeyringProvider
from server.settings import Settings
from server.storage import LocalStorage
from server.worker import run_job


def require_free_ports(ports):
    if len(set(ports)) != len(ports) or any(not 1024 <= port <= 65535 for port in ports):
        raise ValueError('PREVIEW_PORT_INVALID')
    for port in ports:
        with socket.socket() as check:
            check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            check.bind(('127.0.0.1', port))


GOOGLE_SERVER_ENV_KEYS = ('REPLAY_GOOGLE_ALLOWED_EMAILS', 'REPLAY_GOOGLE_ALLOWED_SUBJECTS',
                          'REPLAY_GOOGLE_ALLOW_SIGNUPS', 'REPLAY_SITE_ADMIN_EMAILS')
GOOGLE_ENV_KEYS = ('REPLAY_GOOGLE_CLIENT_ID', *GOOGLE_SERVER_ENV_KEYS)


class PreviewConfigurationError(ValueError):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail
        super().__init__(code)


def google_configuration(env_file=None):
    """Read the public client ID and server-only account policy; never execute dotenv."""
    environment = {name: os.environ.get(name, '0' if name == 'REPLAY_GOOGLE_ALLOW_SIGNUPS' else '') for name in GOOGLE_ENV_KEYS}
    if env_file:
        try:
            path = Path(env_file)
            if path.stat().st_size > 65536:
                raise ValueError()
            contents = path.read_text(encoding='utf-8')
            seen = set()
            for line in contents.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                name, separator, value = line.partition('=')
                name, value = name.strip(), value.strip()
                if not separator or name not in GOOGLE_ENV_KEYS or name in seen:
                    raise ValueError()
                seen.add(name)
                if value.startswith(('"', "'")):
                    if len(value) < 2 or value[-1] != value[0]:
                        raise ValueError()
                    value = value[1:-1]
                environment[name] = value
        except (OSError, UnicodeError, ValueError):
            raise PreviewConfigurationError('PREVIEW_GOOGLE_ENV_FILE_INVALID',
                'Google 전용 설정 파일을 확인하세요. .env.google.example에 안내된 클라이언트 ID·접속 정책·사이트 관리자 항목만 지원합니다.') from None
    if not environment['REPLAY_GOOGLE_CLIENT_ID']:
        raise PreviewConfigurationError('PREVIEW_GOOGLE_CLIENT_ID_REQUIRED',
            '실제 Google 로그인을 사용하려면 REPLAY_GOOGLE_CLIENT_ID에 Google 웹 클라이언트 ID를 설정하세요.')
    # Apply the service's exact environment parsing/validation without retaining
    # file values in this process environment or passing account policy to Vite.
    previous = {name: os.environ.get(name) for name in GOOGLE_ENV_KEYS}
    try:
        for name, value in environment.items():
            os.environ[name] = value
        config = GoogleAuthConfig.from_env()
    except ValueError:
        raise PreviewConfigurationError('PREVIEW_GOOGLE_CONFIG_INVALID',
            'Google 웹 클라이언트 ID, 허용 계정, 가입 정책 또는 사이트 관리자 이메일 형식을 확인하세요.') from None
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if not (config.allowed_emails or config.allowed_subjects or config.allow_signups):
        raise PreviewConfigurationError('PREVIEW_GOOGLE_ACCESS_POLICY_REQUIRED',
            'REPLAY_GOOGLE_ALLOWED_EMAILS에 허용할 Google 계정을 설정하세요. 전체 가입 허용은 REPLAY_GOOGLE_ALLOW_SIGNUPS=1로 명시해야 합니다.')
    return config


def preview_build_environment(api, node, google_config=None):
    # Client ID is public. Account policy and other Google variables stay out of
    # the frontend build process, including values inherited from the shell.
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith('REPLAY_GOOGLE_') and name not in GOOGLE_SERVER_ENV_KEYS}
    return {**environment, 'PATH': str(node.parent) + os.pathsep + os.environ.get('PATH', ''),
            'REPLAY_COMMERCIAL': '1', 'REPLAY_CLOUD': '0', 'REPLAY_API_URL': api,
            'REPLAY_LOGIN_PROVIDER': 'google',
            'REPLAY_GOOGLE_CLIENT_ID': google_config.client_id if google_config else '',
            'REPLAY_OIDC_AUTHORITY': '', 'REPLAY_OIDC_CLIENT_ID': '', 'REPLAY_OIDC_AUDIENCE': ''}


def preview_authenticator(repo, google_config=None):
    if google_config:
        auth = GoogleAuthenticator(repo.engine, google_config, mode='development')
        auth.migrate()
        return auth
    return JWTAuthenticator(AuthConfig(mode='development', dev_token=secrets.token_urlsafe(32),
                                      dev_subject='local-preview-user', dev_tenant_id='local-preview'))


def cancel_preview_jobs(repo):
    """Only call with the launcher's temporary repository, across its identities."""
    with repo.engine.connect() as connection:
        pending = connection.execute(select(jobs.c.tenant_id, jobs.c.id).where(jobs.c.state.in_(NONTERMINAL))).all()
    for tenant_id, job_id in pending:
        repo.cancel(tenant_id, job_id)


def preview_build_files(scratch, api, *, google=False, platform_live=False):
    """Keep Google auth intact; use an exact auth override only in development."""
    config = scratch / 'preview.vite.config.mjs'
    login = '실제 Google 로그인' if google else '개발용 로그인'
    if platform_live:
        message = f'플랫폼 송출 테스트 · {login} · 선택한 플랫폼에 실제 영상이 전송됩니다.'
        help_text = '파일 전용 미리보기와 영상 보관함이 분리되어 있습니다.'
        link = ''
    else:
        message = f'파일 전용 미리보기 · {login} · 외부 플랫폼에는 송출되지 않습니다.'
        help_text = '플랫폼 송출 테스트와 영상 보관함이 분리되어 있습니다.'
        link = '<a href="http://127.0.0.1:13102/" style="color:inherit;font-weight:600;text-decoration:underline;margin-left:12px">플랫폼 송출 테스트 열기</a>'
    banner = ('<aside role="note" style="background:#fef3c7;color:#78350f;padding:10px 20px;text-align:center;font:14px/1.6 system-ui">'
              f'<span>{message}</span><br><span style="font-size:12px">{help_text}</span>{link}</aside>')
    if google:
        config.write_text('''import base from BASE_CONFIG;
const previewBanner = PREVIEW_BANNER;
export default { ...base,
  plugins: [{ name: 'replay-google-preview',
    transformIndexHtml(html) {
      return html.replace('<body>', '<body>' + previewBanner);
    }
  }, ...base.plugins],
  preview: { host: '127.0.0.1', strictPort: true, headers: {
    'Referrer-Policy': 'no-referrer-when-downgrade',
    'Cross-Origin-Opener-Policy': 'same-origin-allow-popups'
  } },
};
'''.replace('BASE_CONFIG', json.dumps(str(ROOT / 'web/vite.vercel.config.ts')))
           .replace('PREVIEW_BANNER', json.dumps(banner, ensure_ascii=False)))
        return config
    adapter = scratch / 'preview-auth.ts'
    adapter.write_text('''export const COMMERCIAL = true;
export const AUTH_PROVIDER = 'development';
const key = 'replay-local-preview-session';
export function authIdentity() { return sessionStorage.getItem(key) || 'signed-out'; }
function stored() {
  try { const value = JSON.parse(sessionStorage.getItem(key) || 'null');
    return value && typeof value.token === 'string' && value.expires_at > Date.now() / 1000 ? value : null;
  } catch { return null; }
}
export async function completeSignIn() { return Boolean(stored()); }
export async function signIn() {
  const response = await fetch(SESSION_URL, { method: 'POST', credentials: 'omit',
    headers: { 'X-Replay-Client': '1' } });
  if (!response.ok) throw new Error('로컬 미리보기 로그인에 실패했습니다. 실행기를 확인하세요.');
  sessionStorage.setItem(key, JSON.stringify(await response.json()));
  window.location.assign('/');
}
export async function signOut() { sessionStorage.removeItem(key); window.location.assign('/'); }
export async function accessToken(forceRefresh = false) {
  const value = stored();
  if (!value || forceRefresh) {
    sessionStorage.removeItem(key);
    throw new Error('로컬 미리보기에서 다시 로그인하세요.');
  }
  return value.token;
}
'''.replace('SESSION_URL', json.dumps(api + '/preview/session')))
    config.write_text('''import fs from 'node:fs';
import base from BASE_CONFIG;
const previewBanner = PREVIEW_BANNER;
const authPath = AUTH_PATH;
let replaced = 0;
export default { ...base,
  plugins: [{ name: 'replay-local-preview-only', enforce: 'pre',
    load(id) {
      if (id.split('?')[0] !== authPath) return null;
      replaced++;
      return fs.readFileSync(ADAPTER_PATH, 'utf8');
    },
    buildEnd(error) { if (!error && replaced !== 1) throw new Error('PREVIEW_AUTH_OVERRIDE_MISSING'); },
    transformIndexHtml(html) {
      return html.replace('<body>', '<body>' + previewBanner);
    }
  }, ...base.plugins],
  preview: { host: '127.0.0.1', strictPort: true },
};
'''.replace('BASE_CONFIG', json.dumps(str(ROOT / 'web/vite.vercel.config.ts')))
           .replace('AUTH_PATH', json.dumps(str(ROOT / 'web/lib/auth.ts')))
           .replace('ADAPTER_PATH', json.dumps(str(adapter)))
           .replace('PREVIEW_BANNER', json.dumps(banner, ensure_ascii=False)))
    return config


def install_preview_boundary(app, auth, *, api_port, web, google=False):
    @app.middleware('http')
    async def preview_boundary(request, call_next):
        if request.headers.get('host') != f'127.0.0.1:{api_port}':
            return JSONResponse({'detail': 'Local preview host required'}, status_code=403)
        if request.headers.get('origin') not in (None, web):
            return JSONResponse({'detail': 'Local preview origin required'}, status_code=403)
        if request.url.path == '/preview/session':
            if google:
                return JSONResponse({'detail': 'Not found'}, status_code=404,
                                    headers={'Cache-Control': 'no-store'})
            if request.headers.get('origin') != web:
                return JSONResponse({'detail': 'Local preview origin required'}, status_code=403)
            cors = {'Access-Control-Allow-Origin': web, 'Access-Control-Allow-Methods': 'POST',
                    'Access-Control-Allow-Headers': 'X-Replay-Client', 'Cache-Control': 'no-store', 'Vary': 'Origin'}
            if request.method == 'OPTIONS':
                return JSONResponse({}, headers=cors)
            if request.method != 'POST' or request.headers.get('x-replay-client') != '1':
                return JSONResponse({'detail': 'Local preview login request required'}, status_code=403, headers=cors)
            token = secrets.token_urlsafe(32)
            auth.config = replace(auth.config, dev_token=token)
            return JSONResponse({'token': token, 'expires_at': time.time() + 3600}, headers=cors)
        if request.method == 'POST' and request.url.path in ('/api/broadcasts', '/api/broadcast-batches'):
            try:
                payload = await request.json()
            except ValueError:
                return JSONResponse({'detail': '요청을 확인하세요.'}, status_code=400)
            choices = payload.get('destinations') if isinstance(payload, dict) and request.url.path.endswith('broadcast-batches') else [payload]
            if not isinstance(choices, list) or not choices or any(not isinstance(item, dict) or item.get('target') != 'local' for item in choices):
                return JSONResponse({'detail': '로컬 미리보기에서는 파일 송출 테스트만 가능합니다.'}, status_code=400,
                                    headers={'Access-Control-Allow-Origin': web})
        return await call_next(request)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-port', type=int, default=18090, help='Unused loopback API port (default: 18090)')
    parser.add_argument('--web-port', type=int, default=13100, help='Unused loopback web port (default: 13100)')
    parser.add_argument('--google', action='store_true', help='Use real Google login; requires a web client ID and account policy')
    parser.add_argument('--google-env-file', help='Optional file with the public Google client ID and server-only account policies listed in .env.google.example; requires --google')
    args = parser.parse_args()
    api = f'http://127.0.0.1:{args.api_port}'
    web = f'http://{"localhost" if args.google else "127.0.0.1"}:{args.web_port}'
    stopping = threading.Event()
    servers, preview_process, worker_thread, repo, scratch_dir = [], None, None, None, None
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    try:
        if args.google_env_file and not args.google:
            raise PreviewConfigurationError('PREVIEW_GOOGLE_MODE_REQUIRED',
                '--google-env-file은 --google 옵션과 함께 사용하세요.')
        google_config = google_configuration(args.google_env_file) if args.google else None
        require_free_ports((args.api_port, args.web_port))
        scratch_dir = tempfile.TemporaryDirectory(prefix='replay-local-preview-')
        scratch = Path(scratch_dir.name)
        cfg = Settings(mode='development', database_url='sqlite:///' + str(scratch / 'preview.sqlite3'),
            public_url=api, origins=(web,), control_token=secrets.token_urlsafe(32), callback_key=secrets.token_urlsafe(32),
            version='local-preview', validation_timeout=45, max_duration=120, max_storage_bytes=20 * 1024**2,
            local_root=str(scratch))
        repo = Repository(cfg.database_url, create_schema=True, max_storage_bytes=cfg.max_storage_bytes,
                          max_output_bytes=cfg.max_output_bytes, validation_duration=cfg.validation_timeout,
                          tenant_concurrency=cfg.tenant_concurrency, global_concurrency=cfg.global_concurrency)
        repo.migrate_watch_links()
        objects = LocalStorage(scratch / 'objects', signing_key=cfg.callback_key, base_url=api, allow_development=True)
        keys = LocalKeyringProvider({'preview': Fernet.generate_key()}, 'preview', mode='development')
        auth = preview_authenticator(repo, google_config)
        app = create_production_app(cfg, repository=repo, storage=objects, keys=keys, authenticator=auth)
        app.state.access_policy.migrate()
        app.state.operations.migrate()
        app.state.stream_connections.migrate()
        logging.getLogger('replay').setLevel(logging.CRITICAL)
        logging.getLogger('replay.requests').disabled = True

        install_preview_boundary(app, auth, api_port=args.api_port, web=web, google=args.google)

        # The preview guard reads the broadcast body before the application's
        # middleware; keep the same actual-byte boundary outside that guard.
        app.add_middleware(BoundaryMiddleware, max_body_bytes=max(cfg.max_upload_bytes, cfg.max_output_bytes),
                           path_limits={'/api/broadcasts': 8192, '/api/broadcast-batches': 65536, '/preview/session': 1024}, body_timeout=120)

        media_id = None
        if not args.google:
            sample = scratch / 'local-preview-15s.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '15', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(sample)], check=True, timeout=45, capture_output=True)
            metadata = validate_media(sample, validation_timeout=45)
            data = sample.read_bytes()
            digest, media_id = hashlib.sha256(data).hexdigest(), uuid.uuid4().hex
            object_key = objects.key('local-preview', media_id)
            objects.put_bytes('local-preview', object_key, data, size=len(data), sha256=digest)
            repo.add_media('local-preview', id=media_id, name='로컬 미리보기 15초.mp4', object_key=object_key,
                           bytes=len(data), sha256=digest, status='ready', **metadata)
            del data

        api_server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.api_port,
                                                    access_log=False, log_level='critical'))
        api_thread = threading.Thread(target=api_server.run, daemon=True)
        api_thread.start()
        servers.append((api_server, api_thread))
        deadline = time.monotonic() + 10
        while not api_server.started and api_thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        if not api_server.started:
            raise RuntimeError('PREVIEW_API_START_FAILED')

        def worker():
            with httpx.Client(timeout=10, trust_env=False) as client:
                while not stopping.is_set():
                    try:
                        response = client.post(api + '/internal/claim', headers={'Authorization': 'Bearer ' + cfg.control_token},
                                               json={'worker_id': 'local-preview', 'version': cfg.version})
                        response.raise_for_status()
                        job = response.json()['job']
                        if job:
                            if job.get('callback_base') != api + '/internal/jobs/' + job['id']:
                                raise RuntimeError('PREVIEW_CALLBACK_ORIGIN_INVALID')
                            if job['mode'] != 'development' or job['target'] not in ('validate', 'local', 'import'):
                                client.post(job['callback_base'] + '/finish',
                                    headers={'Authorization': 'Bearer ' + job['callback_token']},
                                    json={'state': 'failed', 'error_code': 'PREVIEW_EXTERNAL_OUTPUT_FORBIDDEN', 'progress': 0})
                            else:
                                run_job(job, client=client, workdir=scratch)
                    except Exception:
                        # No payloads, URLs or credentials in preview diagnostics.
                        pass
                    stopping.wait(.25)
        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()

        config = preview_build_files(scratch, api, google=args.google)
        node = Path('/opt/homebrew/opt/node@24/bin/node')
        if not node.is_file():
            raise RuntimeError('PREVIEW_NODE24_REQUIRED')
        env = preview_build_environment(api, node, google_config)
        vite = str(ROOT / 'web/node_modules/vite/bin/vite.js')
        subprocess.run([str(node), vite, 'build', '--config', str(config), '--outDir', str(scratch / 'site')],
                       cwd=ROOT / 'web', env=env, check=True, timeout=120, capture_output=True)
        with (scratch / 'preview.log').open('w') as log:
            preview_process = subprocess.Popen([str(node), vite, 'preview', '--config', str(config),
                '--outDir', str(scratch / 'site'), '--host', '127.0.0.1', '--port', str(args.web_port), '--strictPort'],
                cwd=ROOT / 'web', env=env, stdout=log, stderr=log)
            with httpx.Client(timeout=2, trust_env=False) as client:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and preview_process.poll() is None:
                    try:
                        if client.get(web).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('PREVIEW_WEB_START_FAILED')
            print(json.dumps({'ready': True, 'url': web + '/', 'pid': os.getpid(),
                              'sample_preview_path': '/api/media/' + media_id + '/preview' if media_id else None,
                              'authentication': 'google' if args.google else 'development_only',
                              'external_streaming': False}), flush=True)
            while not stopping.wait(.5):
                if preview_process.poll() is not None or not api_thread.is_alive():
                    raise RuntimeError('PREVIEW_SERVER_STOPPED')
        return 0
    except PreviewConfigurationError as error:
        print(json.dumps({'ready': False, 'error': error.code, 'detail': error.detail}, ensure_ascii=False), flush=True)
        return 1
    except Exception as error:
        print(json.dumps({'ready': False, 'error': 'PREVIEW_START_OR_RUNTIME_FAILED', 'type': type(error).__name__}), flush=True)
        return 1
    finally:
        stopping.set()
        if repo:
            try:
                cancel_preview_jobs(repo)
            except Exception:
                pass
        if worker_thread:
            worker_thread.join(timeout=20)
        if preview_process:
            preview_process.terminate()
            try:
                preview_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                preview_process.kill()
                preview_process.wait(timeout=5)
        for server, thread in reversed(servers):
            server.should_exit = True
            thread.join(timeout=5)
        if scratch_dir:
            scratch_dir.cleanup()


if __name__ == '__main__':
    raise SystemExit(main())
