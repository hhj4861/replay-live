#!/usr/bin/env python3
"""Actual Chrome commercial-flow smoke using only temporary local fixtures.

The synthetic HTTPS OIDC issuer enforces PKCE and issues real RS256 JWTs.
SQLite, local object storage and the actual HTTP worker run only in explicit
development mode. This does not verify any live IdP or cloud service. An explicit
--device-import --source-url also exercises actual source download on the PC.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import jwt
import uvicorn
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from server.auth import AuthConfig, JWTAuthenticator
from server.production_app import create_production_app
from server.repository import Repository
from server.secrets import LocalKeyringProvider
from server.settings import Settings
from server.storage import LocalStorage
from server.worker import run_job


def reserve_ports(ports):
    for port in ports:
        with socket.socket() as check:
            check.bind(('127.0.0.1', port))


def certificate(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Replay synthetic local issuer')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),
            x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
    key_path, cert_path = directory / 'synthetic.key', directory / 'synthetic.crt'
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key, key_path, cert_path


def issuer_app(origin, web, signing_key, counters):
    app = FastAPI(docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=[web], allow_methods=['GET', 'POST', 'OPTIONS'],
                       allow_headers=['Content-Type'], allow_credentials=True)
    codes, refresh_tokens = {}, {}
    client_id, audience = 'replay-synthetic-browser', 'replay-synthetic-api'

    @app.get('/.well-known/openid-configuration')
    def metadata():
        return {'issuer': origin, 'authorization_endpoint': origin + '/authorize',
            'token_endpoint': origin + '/token', 'jwks_uri': origin + '/jwks',
            'end_session_endpoint': origin + '/logout', 'revocation_endpoint': origin + '/revoke',
            'response_types_supported': ['code'], 'subject_types_supported': ['public'],
            'id_token_signing_alg_values_supported': ['RS256'], 'token_endpoint_auth_methods_supported': ['none'],
            'code_challenge_methods_supported': ['S256'], 'scopes_supported': ['openid', 'profile', 'offline_access']}

    @app.get('/jwks')
    def jwks():
        counters['jwks_reads'] += 1
        return {'keys': [dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key())),
                              kid='synthetic-rsa', alg='RS256', use='sig')]}

    @app.get('/authorize')
    def authorize(request: Request):
        query = dict(request.query_params)
        if (query.get('client_id') != client_id or query.get('redirect_uri') != web + '/' or
                query.get('response_type') != 'code' or query.get('code_challenge_method') != 'S256' or
                not query.get('code_challenge') or not query.get('state')):
            raise HTTPException(400, 'Synthetic authorization request rejected')
        code = secrets.token_urlsafe(24)
        codes[code] = {**query, 'expires': time.time() + 60}
        counters['authorizations'] += 1
        return RedirectResponse(web + '/?' + urlencode({'code': code, 'state': query['state']}), status_code=302)

    def tokens(nonce, auth_time=None):
        now = int(time.time())
        auth_time = now if auth_time is None else auth_time
        shared = {'iss': origin, 'sub': 'synthetic-browser-operator', 'iat': now, 'exp': now + 600}
        access = jwt.encode({**shared, 'aud': audience, 'jti': secrets.token_urlsafe(16),
            'tenant_id': 'synthetic-browser-tenant', 'roles': ['operator'], 'token_use': 'access'},
            signing_key, algorithm='RS256', headers={'kid': 'synthetic-rsa'})
        identity = jwt.encode({**shared, 'aud': client_id, 'nonce': nonce, 'auth_time': auth_time},
            signing_key, algorithm='RS256', headers={'kid': 'synthetic-rsa'})
        refresh = secrets.token_urlsafe(32)
        refresh_tokens[refresh] = (nonce, auth_time)
        return {'access_token': access, 'id_token': identity, 'refresh_token': refresh,
                'token_type': 'Bearer', 'expires_in': 600, 'scope': 'openid profile offline_access'}

    @app.post('/token')
    async def token(request: Request):
        form = {key: value[0] for key, value in parse_qs((await request.body()).decode()).items()}
        if form.get('client_id') != client_id:
            raise HTTPException(400, 'Synthetic client rejected')
        if form.get('grant_type') == 'authorization_code':
            state = codes.pop(form.get('code', ''), None)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get('code_verifier', '').encode()).digest()).rstrip(b'=').decode()
            if not state or state['expires'] <= time.time() or state['code_challenge'] != challenge or form.get('redirect_uri') != web + '/':
                raise HTTPException(400, 'Synthetic PKCE verification failed')
            counters['pkce_exchanges'] += 1
            return JSONResponse(tokens(state.get('nonce', '')), headers={'Cache-Control': 'no-store'})
        if form.get('grant_type') == 'refresh_token':
            state = refresh_tokens.pop(form.get('refresh_token', ''), None)
            if state is None:
                raise HTTPException(400, 'Synthetic refresh token rejected')
            counters['refresh_exchanges'] += 1
            return JSONResponse(tokens(*state), headers={'Cache-Control': 'no-store'})
        raise HTTPException(400, 'Synthetic grant rejected')

    @app.post('/revoke')
    async def revoke(request: Request):
        form = parse_qs((await request.body()).decode())
        refresh_tokens.pop(form.get('token', [''])[0], None)
        counters['revocations'] += 1
        return {}

    @app.get('/logout')
    def logout(request: Request):
        if request.query_params.get('post_logout_redirect_uri') != web + '/':
            raise HTTPException(400)
        counters['logouts'] += 1
        query = {'state': request.query_params['state']} if request.query_params.get('state') else {}
        return RedirectResponse(web + '/' + ('?' + urlencode(query) if query else ''), status_code=302)
    return app


def start_server(app, port, cert=None, key=None):
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, access_log=False, log_level='critical',
        ssl_certfile=str(cert) if cert else None, ssl_keyfile=str(key) if key else None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.02)
    if not server.started:
        raise RuntimeError('Synthetic server did not start')
    return server, thread


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-port', type=int, default=18090)
    parser.add_argument('--web-port', type=int, default=13100)
    parser.add_argument('--issuer-port', type=int, default=19443)
    parser.add_argument('--device-import', action='store_true', help='Exercise the PC daemon and direct cloud upload')
    parser.add_argument('--source-url', help='With --device-import, verify this actual YouTube source through preview only')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/commercial-browser-evidence-2026-09-10.json')
    args = parser.parse_args()
    if args.source_url and not args.device_import:
        parser.error('--source-url requires --device-import')
    reserve_ports((args.api_port, args.web_port, args.issuer_port))
    if args.device_import:
        reserve_ports((17833,))
    api, web, issuer = f'http://127.0.0.1:{args.api_port}', f'http://127.0.0.1:{args.web_port}', f'https://localhost:{args.issuer_port}'
    counters = {key: 0 for key in ('jwks_reads', 'authorizations', 'pkce_exchanges', 'refresh_exchanges', 'revocations', 'logouts')}
    evidence = {'checked_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
        'scope': 'actual local Chrome, synthetic HTTPS OIDC with real PKCE/RS256, development API/SQLite/local objects/HTTP worker',
        'live_idp_verified': False, 'cloud_verified': False, 'youtube_started': False,
        'tls_exception': 'self-signed temporary issuer certificate; browser ignores TLS errors only in this fixture'}
    servers, process, worker_thread = [], None, None
    temporary_dir, repo = None, None
    stopping = threading.Event()
    worker_results = []
    try:
        temporary_dir = tempfile.TemporaryDirectory(prefix='replay-commercial-browser-')
        scratch = Path(temporary_dir.name)
        signing_key, key_path, cert_path = certificate(scratch)
        servers.append(start_server(issuer_app(issuer, web, signing_key, counters), args.issuer_port, cert_path, key_path))
        cfg = Settings(mode='development', database_url='sqlite:///' + str(scratch / 'state.db'), public_url=api,
            origins=(web,), control_token=secrets.token_urlsafe(32), callback_key=secrets.token_urlsafe(32),
            version='synthetic-browser-release', validation_timeout=45, max_duration=120,
            max_storage_bytes=20 * 1024**2, local_root=str(scratch))
        repo = Repository(cfg.database_url, create_schema=True, validation_duration=cfg.validation_timeout,
                          max_storage_bytes=cfg.max_storage_bytes, max_output_bytes=cfg.max_output_bytes)
        objects = LocalStorage(scratch / 'objects', signing_key=cfg.callback_key, base_url=api, allow_development=True)
        keys = LocalKeyringProvider({'synthetic': Fernet.generate_key()}, 'synthetic', mode='development')
        auth_http = httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(cert_path)), trust_env=False)
        auth = JWTAuthenticator(AuthConfig(issuer=issuer, audience='replay-synthetic-api', jwks_url=issuer + '/jwks'), auth_http)
        app = create_production_app(cfg, repository=repo, storage=objects, keys=keys, authenticator=auth)
        app.state.access_policy.migrate()
        app.state.operations.migrate()
        servers.append(start_server(app, args.api_port))
        logging.getLogger('replay.requests').disabled = True
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)
        def worker():
            with httpx.Client(timeout=10, trust_env=False) as client:
                while not stopping.is_set():
                    try:
                        response = client.post(api + '/internal/claim', headers={'Authorization': 'Bearer ' + cfg.control_token},
                            json={'worker_id': 'synthetic-browser-dispatcher', 'version': cfg.version})
                        response.raise_for_status()
                        job = response.json()['job']
                        if job:
                            if job['mode'] != 'development' or job['target'] not in ('validate', 'local', 'import'):
                                raise RuntimeError('Fixture refuses external output or production jobs')
                            result = run_job(job, client=client, workdir=scratch)
                            worker_results.append({'target': job['target'], 'state': result['state'], 'progress': result.get('progress')})
                    except Exception as error:
                        worker_results.append({'state': 'fixture_error', 'type': type(error).__name__})
                    stopping.wait(.2)
        worker_thread = threading.Thread(target=worker, daemon=True)
        worker_thread.start()
        sample = scratch / 'synthetic-browser.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30',
            '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '15', '-c:v', 'libx264', '-preset', 'ultrafast',
            '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(sample)], check=True, timeout=45, capture_output=True)
        if args.device_import:
            from server.local_import_daemon import LocalImports, create_local_import_app
            def synthetic_source(source, output, **limits):
                if args.source_url:
                    from server.media_sources import download_source, normalize_source
                    if source != normalize_source('youtube', args.source_url):
                        raise ValueError('Unexpected real source')
                    result = download_source(source, output, **limits)
                    info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(output)], timeout=15))
                    evidence['pc_download'] = {'bytes': result['bytes'], 'sha256': result['sha256'],
                        'duration': float(info['format']['duration']), 'codecs': sorted(s['codec_name'] for s in info['streams'])}
                    return result
                if source != {'provider': 'direct', 'url': 'https://media.example/synthetic.mp4'}:
                    raise ValueError('Unexpected browser fixture source')
                limits['check_active']()
                output.write_bytes(sample.read_bytes())
                evidence['pc_source_download_calls'] = evidence.get('pc_source_download_calls', 0) + 1
                return {'bytes': sample.stat().st_size, 'sha256': hashlib.sha256(sample.read_bytes()).hexdigest()}
            local = LocalImports(root=scratch, pairing_code='ABCDEF123456', downloader=synthetic_source,
                                 cloud_url=api, development=True)
            servers.append(start_server(create_local_import_app(origins=(web,), manager=local), 17833))
            logging.getLogger('replay.requests').disabled = True
            evidence['source_transport'] = 'actual YouTube download on local Mac' if args.source_url else 'generated MP4 fixture; original platform not contacted'
        node = '/opt/homebrew/opt/node@24/bin/node'
        if not Path(node).is_file():
            raise RuntimeError('Node 24 is required for this repository build')
        env = {**os.environ, 'PATH': str(Path(node).parent) + os.pathsep + os.environ.get('PATH', ''),
            'REPLAY_COMMERCIAL': '1', 'REPLAY_CLOUD': '0', 'REPLAY_API_URL': api,
            'REPLAY_OIDC_AUTHORITY': issuer, 'REPLAY_OIDC_CLIENT_ID': 'replay-synthetic-browser',
            'REPLAY_OIDC_AUDIENCE': 'replay-synthetic-api', 'REPLAY_OIDC_SCOPE': 'openid profile offline_access'}
        vite = str(ROOT / 'web/node_modules/vite/bin/vite.js')
        subprocess.run([node, vite, 'build', '--config', 'vite.vercel.config.ts', '--outDir', str(scratch / 'site')],
            cwd=ROOT / 'web', env=env, check=True, timeout=120, capture_output=True)
        with (scratch / 'preview.log').open('w') as log:
            process = subprocess.Popen([node, vite, 'preview', '--config', 'vite.vercel.config.ts', '--outDir', str(scratch / 'site'),
                '--host', '127.0.0.1', '--port', str(args.web_port), '--strictPort'], cwd=ROOT / 'web', env=env, stdout=log, stderr=log)
            deadline = time.monotonic() + 20
            with httpx.Client(trust_env=False) as client:
                while time.monotonic() < deadline:
                    try:
                        if client.get(web).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('Vite preview did not become ready')
            browser_config = scratch / 'browser-config.json'
            browser_result = scratch / 'browser-result.json'
            browser_config.write_text(json.dumps({'api': api, 'web': web, 'issuer': issuer, 'sample': str(sample),
                'download': str(scratch / 'download.flv'), 'result': str(browser_result), 'device_import': args.device_import,
                'source_url': args.source_url, 'screenshot': str(args.output.with_suffix('.png'))}))
            driver = 'device-import-browser-smoke.mjs' if args.device_import else 'commercial-browser-smoke.mjs'
            result = subprocess.run([node, str(ROOT / 'scripts' / driver), str(browser_config)],
                cwd=ROOT, timeout=240, capture_output=True, text=True)
            if browser_result.exists():
                evidence.update(json.loads(browser_result.read_text()))
            if result.returncode:
                raise RuntimeError('Browser flow failed at ' + evidence.get('stage', 'unknown'))
            if args.device_import:
                if [item['target'] for item in worker_results] != ['validate']:
                    raise RuntimeError('PC import did not use only the cloud validator')
                source_duration = evidence['pc_download']['duration'] if args.source_url else 15
                if abs(evidence['preview']['duration'] - source_duration) > .1:
                    raise RuntimeError('Source and cloud preview duration differ')
                evidence.update(worker_results=worker_results, issuer_checks=counters, passed=True)
                return 0
            if counters['refresh_exchanges'] < 1 or len([item for item in worker_results if item['target'] == 'local']) != 1:
                raise RuntimeError('Refresh or quota worker verification failed')
            output = scratch / 'download.flv'
            info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(output)], timeout=15))
            codecs = sorted(stream['codec_name'] for stream in info['streams'])
            duration = float(info['format']['duration'])
            if codecs != ['aac', 'h264'] or duration < 14.9 or output.read_bytes()[:3] != b'FLV':
                raise RuntimeError('Downloaded video verification failed')
            evidence.update(download={'bytes': output.stat().st_size, 'duration': duration, 'codecs': codecs,
                'sha256': hashlib.sha256(output.read_bytes()).hexdigest()}, upload_bytes=sample.stat().st_size,
                worker_results=worker_results, issuer_checks=counters, passed=True)
    except Exception as error:
        evidence['passed'] = False
        evidence['failure_type'] = type(error).__name__
        evidence['failure_stage'] = evidence.get('stage', 'fixture_setup')
        evidence['issuer_checks'] = counters
        evidence['worker_results'] = worker_results
    finally:
        stopping.set()
        if repo:
            try:
                for job in repo.list_jobs('synthetic-browser-tenant'):
                    if job['state'] not in ('completed', 'failed', 'stopped'):
                        repo.cancel('synthetic-browser-tenant', job['id'])
            except Exception:
                pass
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if worker_thread:
            worker_thread.join(timeout=20)
        for server, thread in reversed(servers):
            server.should_exit = True
            thread.join(timeout=5)
        if temporary_dir:
            temporary_dir.cleanup()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': evidence['passed'], 'stage': evidence.get('stage'), 'evidence': str(args.output)}, ensure_ascii=False))
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
