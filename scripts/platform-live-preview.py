#!/usr/bin/env python3
"""Persistent, explicitly enabled platform tests inside a dedicated Linux VM.

API/worker only: the web preview runs separately on the Mac. Defaults permit
file tests only. This launcher never changes the regular commercial preview.
"""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from server.media_runtime import MediaError, validate_media
from server.repository import NotFound, Repository
from server.request_boundary import BoundaryMiddleware
from server.secrets import LocalKeyringProvider
from server.settings import Settings
from server.storage import LocalStorage
from server.stream_targets import LIVE_TARGETS, STREAM_TARGETS, install_host_pin, validate_pinned_destination
from server.worker import run_job

SPEC = importlib.util.spec_from_file_location('platform_test_commercial_preview', ROOT / 'scripts/commercial-preview.py')
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)

VERSION = 'platform-live-test-v1'
TENANT = 'platform-live-test'
VM_MARKER = Path('/etc/replay-platform-test-vm')
VM_MARKER_CONTENT = 'replay-platform-live-test-v1\n'
PIN_JOURNAL = Path('/run/replay-platform-live-host-pin.json')
STORAGE_LIMIT = 1024**3
MAX_DURATION = 120


class LivePreviewError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require_test_vm():
    """The provisioning coordinator creates this root-owned explicit VM marker."""
    try:
        info = VM_MARKER.lstat()
        if (sys.platform != 'linux' or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022 or info.st_size != len(VM_MARKER_CONTENT)
                or VM_MARKER.read_text() != VM_MARKER_CONTENT):
            raise ValueError()
    except (OSError, ValueError):
        raise LivePreviewError('PLATFORM_TEST_VM_REQUIRED') from None


def allowed_targets(values):
    if (any(value not in STREAM_TARGETS | {'none', 'all'} for value in values)
            or (set(values) & {'none', 'all'} and len(values) > 1)):
        raise LivePreviewError('PLATFORM_TEST_ALLOWLIST_INVALID')
    if values == ['all']:
        return STREAM_TARGETS
    return frozenset({'local', *(value for value in values if value != 'none')})


def private_directory(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise LivePreviewError('PLATFORM_TEST_DATA_PATH_INVALID')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise LivePreviewError('PLATFORM_TEST_DATA_PERMISSIONS')
    return path


def checked_private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or info.st_nlink != 1):
        raise LivePreviewError('PLATFORM_TEST_FILE_PERMISSIONS')
    return info


def create_private_file(path, contents=b''):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def exclusive_runtime(data_dir, *, vm_lock_path=None):
    path = data_dir / 'runtime.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    vm_fd = None
    try:
        checked_private_file(path)
        try:
            if vm_lock_path is not None:
                # One coordinator per VM, even if another launch uses a new
                # port/data directory. flock does not alter the root-owned marker.
                vm_fd = os.open(vm_lock_path, os.O_RDONLY | os.O_NOFOLLOW)
                fcntl.flock(vm_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LivePreviewError('PLATFORM_TEST_ALREADY_RUNNING') from None
        yield
    finally:
        os.close(fd)
        if vm_fd is not None:
            os.close(vm_fd)


def persistent_keys(data_dir):
    path = data_dir / 'keys.json'
    if not path.exists() and not path.is_symlink():
        if (data_dir / 'platform-test.sqlite3').exists():
            raise LivePreviewError('PLATFORM_TEST_KEYS_MISSING')
        create_private_file(path, json.dumps({'version': VERSION, 'control_token': secrets.token_urlsafe(32),
            'callback_key': secrets.token_urlsafe(32), 'keyring': Fernet.generate_key().decode()}).encode())
    try:
        if checked_private_file(path).st_size > 4096:
            raise ValueError()
        values = json.loads(path.read_text())
        if (set(values) != {'version', 'control_token', 'callback_key', 'keyring'} or values['version'] != VERSION
                or any(not isinstance(values[key], str) or len(values[key]) < 32 for key in ('control_token', 'callback_key'))
                or values['control_token'] == values['callback_key']):
            raise ValueError()
        Fernet(values['keyring'])
        return values
    except (OSError, ValueError, TypeError, KeyError):
        raise LivePreviewError('PLATFORM_TEST_KEYS_INVALID') from None


def _without_host(contents, hostname):
    return [line for line in contents.splitlines() if hostname not in line.split('#', 1)[0].split()[1:]]


def edit_host_pin(action, *, hostname=None, addresses=None, hosts_path=Path('/etc/hosts'), journal_path=PIN_JOURNAL):
    """Privileged helper: recover only this tool's change, never overwrite others."""
    hosts_path, journal_path = Path(hosts_path), Path(journal_path)
    lock_fd = os.open(journal_path.with_suffix('.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if action == 'restore':
            if not journal_path.exists() and not journal_path.is_symlink():
                return
            try:
                if checked_private_file(journal_path).st_size > 131072:
                    raise ValueError()
                saved = json.loads(journal_path.read_text())
                before, hostname = saved['before'], saved['hostname']
                if not isinstance(before, str) or not isinstance(hostname, str):
                    raise ValueError()
                current = hosts_path.read_text()
                if _without_host(current, hostname) != _without_host(before, hostname):
                    raise ValueError()
                hosts_path.write_text(before)
                journal_path.unlink()
            except (OSError, ValueError, KeyError, TypeError):
                raise LivePreviewError('PLATFORM_TEST_HOST_RESTORE_REQUIRED') from None
            return
        if action != 'install' or journal_path.exists() or journal_path.is_symlink():
            raise LivePreviewError('PLATFORM_TEST_HOST_RESTORE_REQUIRED')
        pinned = validate_pinned_destination({'target': 'custom', 'server_url': f'rtmp://{hostname}:1935/app',
            'stream_key': 'platform-pin-check', 'hostname': hostname, 'port': 1935, 'protocol': 'rtmp', 'addresses': addresses})
        before = hosts_path.read_text()
        if len(before.encode()) > 65536:
            raise LivePreviewError('PLATFORM_TEST_HOSTS_INVALID')
        create_private_file(journal_path, json.dumps({'before': before, 'hostname': pinned['hostname']}).encode())
        install_host_pin(pinned['hostname'], pinned['addresses'], hosts_path=hosts_path)
    finally:
        os.close(lock_fd)


def manage_host_pin(action, destination=None):
    require_test_vm()
    values = {}
    if action == 'install':
        destination = validate_pinned_destination(destination)
        # Literal public addresses require no hosts mutation.
        if destination['hostname'] in destination['addresses']:
            return
        values = {'hostname': destination['hostname'], 'addresses': destination['addresses']}
    if os.geteuid() == 0:
        return edit_host_pin(action, **values)
    command = ['sudo', '-n', sys.executable, str(Path(__file__).resolve()), '--hosts-action', action]
    if values:
        command.extend(['--pin-host', values['hostname']])
        for address in values['addresses']:
            command.extend(['--pin-address', address])
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=10, check=False)
        if result.returncode != 0:
            raise ValueError()
    except (OSError, subprocess.SubprocessError, ValueError):
        raise LivePreviewError('PLATFORM_TEST_HOST_SETUP_FAILED') from None


def install_boundary(app, auth, *, api_port, web, allowed):
    # Reuse the existing session issuance, rotation, exact-origin and CSRF
    # contract without mounting its file-only broadcast boundary on the API.
    sessions = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    preview.install_preview_boundary(sessions, auth, api_port=api_port, web=web)

    @sessions.get('/status')
    def status():
        return {'allowed_targets': sorted(allowed), 'external_streaming': bool(allowed & LIVE_TARGETS),
                'sequential': True, 'storage_limit_bytes': STORAGE_LIMIT, 'max_duration_seconds': MAX_DURATION}

    app.mount('/preview', sessions)

    @app.middleware('http')
    async def boundary(request, call_next):
        if request.headers.get('host') != f'127.0.0.1:{api_port}':
            return JSONResponse({'detail': 'Local platform test host required'}, status_code=403)
        if request.headers.get('origin') not in (None, web):
            return JSONResponse({'detail': 'Local platform test origin required'}, status_code=403)
        if request.method == 'POST' and request.url.path in ('/api/broadcasts', '/api/broadcast-batches'):
            try:
                payload = await request.json()
            except ValueError:
                return JSONResponse({'detail': '요청을 확인하세요.'}, status_code=400)
            choices = payload.get('destinations') if isinstance(payload, dict) and request.url.path.endswith('broadcast-batches') else [payload]
            if (not isinstance(choices, list) or not choices or any(not isinstance(choice, dict)
                    or not isinstance(choice.get('target'), str) or choice['target'] not in allowed for choice in choices)):
                return JSONResponse({'detail': '이번 시험에서 허용하지 않은 플랫폼입니다.', 'code': 'PLATFORM_TEST_TARGET_FORBIDDEN'},
                    status_code=400, headers={'Access-Control-Allow-Origin': web})
        return await call_next(request)

    app.add_middleware(BoundaryMiddleware, max_body_bytes=STORAGE_LIMIT, body_timeout=120,
        path_limits={'/api/broadcasts': 8192, '/api/broadcast-batches': 65536, '/preview/session': 1024})


def build_application(data_dir, *, api_port=18092, web_port=13102, allowed=frozenset({'local'})):
    data_dir = private_directory(data_dir)
    values = persistent_keys(data_dir)
    database = data_dir / 'platform-test.sqlite3'
    if not database.exists() and not database.is_symlink():
        create_private_file(database)
    checked_private_file(database)
    cfg = Settings(mode='development', database_url='sqlite:///' + str(database),
        public_url=f'http://127.0.0.1:{api_port}', origins=(f'http://127.0.0.1:{web_port}',),
        control_token=values['control_token'], callback_key=values['callback_key'], version=VERSION,
        local_root=str(data_dir), max_upload_bytes=STORAGE_LIMIT, max_output_bytes=STORAGE_LIMIT,
        max_storage_bytes=STORAGE_LIMIT, max_duration=MAX_DURATION, validation_timeout=120,
        tenant_concurrency=1, global_concurrency=1, worker_max_seconds=900)
    repo = Repository(cfg.database_url, create_schema=True, max_storage_bytes=cfg.max_storage_bytes,
        max_output_bytes=cfg.max_output_bytes, validation_duration=cfg.validation_timeout,
        tenant_concurrency=1, global_concurrency=1, reservation_grace=cfg.reservation_grace)
    repo.migrate_watch_links()
    # Restart never replays queued/scheduled tests from a previous session.
    # Original media and all terminal history remain in this private repository.
    preview.cancel_preview_jobs(repo)
    objects = LocalStorage(data_dir / 'objects', signing_key=cfg.callback_key, base_url=cfg.public_url, allow_development=True)
    keyring = LocalKeyringProvider({'platform-test': values['keyring']}, 'platform-test', mode='development')
    auth = preview.preview_authenticator(repo)
    auth.config = replace(auth.config, dev_subject='platform-test-user', dev_tenant_id=TENANT)
    app = preview.create_production_app(cfg, repository=repo, storage=objects, keys=keyring, authenticator=auth)
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    if hasattr(app.state, 'stream_connections'):
        app.state.stream_connections.migrate()
    install_boundary(app, auth, api_port=api_port, web=cfg.origins[0], allowed=allowed)
    return SimpleNamespace(app=app, cfg=cfg, repo=repo, objects=objects, auth=auth, allowed=allowed, data_dir=data_dir)


def seed_sample(runtime, source):
    source = Path(source)
    if not source.is_file() or not 1 <= source.stat().st_size <= runtime.cfg.max_upload_bytes:
        raise LivePreviewError('PLATFORM_TEST_SAMPLE_INVALID')
    with tempfile.TemporaryDirectory(prefix='sample-check-', dir=runtime.data_dir) as temporary:
        snapshot = Path(temporary) / 'sample.mp4'
        digest, size = hashlib.sha256(), 0
        with source.open('rb') as handle, snapshot.open('xb') as output:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                if size > runtime.cfg.max_upload_bytes:
                    raise LivePreviewError('PLATFORM_TEST_SAMPLE_INVALID')
                digest.update(chunk)
                output.write(chunk)
        checksum = digest.hexdigest()
        media_id = 'sample-' + checksum[:32]
        try:
            runtime.repo.get_media(TENANT, media_id)
            return media_id
        except NotFound:
            pass
        if size > runtime.repo.usage(TENANT)['storage_available_bytes']:
            raise LivePreviewError('PLATFORM_TEST_SAMPLE_STORAGE_FULL')
        metadata = validate_media(snapshot, validation_timeout=120, allow_portrait=True)
        if metadata['duration'] > MAX_DURATION:
            raise LivePreviewError('PLATFORM_TEST_SAMPLE_TOO_LONG')
        object_key = runtime.objects.key(TENANT, media_id)
        runtime.objects.upload(TENANT, object_key, snapshot)
        runtime.repo.add_media(TENANT, id=media_id, name=source.name[:180], object_key=object_key,
            bytes=size, sha256=checksum, status='ready', **metadata)
        return media_id


def process_job(runtime, job, client):
    """No host mutation before allowlist/callback validation; always undo pins."""
    pin_attempted = False
    try:
        if (job.get('mode') != 'development' or job.get('version') != VERSION
                or job.get('callback_base') != runtime.cfg.public_url + '/internal/jobs/' + job.get('id', '')):
            raise LivePreviewError('PLATFORM_TEST_JOB_INVALID')
        if job['target'] not in runtime.allowed | {'validate', 'import'}:
            client.post(job['callback_base'] + '/finish', headers={'Authorization': 'Bearer ' + job['callback_token']},
                json={'state': 'failed', 'error_code': 'PLATFORM_TEST_TARGET_FORBIDDEN', 'progress': 0}).raise_for_status()
            return
        if job['target'] in LIVE_TARGETS:
            pin_attempted = True
            manage_host_pin('install', job.get('stream_destination'))
        # run_job retains all production DNS-pin, TLS, lease, hash and watchdog checks.
        result = run_job(job, client=client, workdir=runtime.data_dir)
        print(json.dumps({'event': 'platform_test_job_finished', 'job_id': job['id'], 'target': job['target'],
            **{key: result.get(key) for key in ('state', 'error_code', 'progress')}}), flush=True)
        return result
    finally:
        try:
            if pin_attempted:
                manage_host_pin('restore')
        finally:
            job['callback_token'] = ''
            job['stream_key'] = ''
            for key in ('source', 'stream_destination'):
                if isinstance(job.get(key), dict):
                    job[key].clear()


def worker_loop(runtime, stopping):
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        while not stopping.is_set():
            try:
                response = client.post(runtime.cfg.public_url + '/internal/claim',
                    headers={'Authorization': 'Bearer ' + runtime.cfg.control_token},
                    json={'worker_id': 'platform-live-test', 'version': VERSION})
                response.raise_for_status()
                job = response.json()['job']
                if job:
                    process_job(runtime, job, client)
            except LivePreviewError as error:
                print(json.dumps({'event': 'platform_test_worker_blocked', 'code': error.code}), flush=True)
                # Host restore failure must prevent another DNS claim or stream.
                stopping.set()
            except Exception:
                # HTTP errors can contain signed URLs or tokens; never print them.
                print(json.dumps({'event': 'platform_test_worker_error', 'code': 'PLATFORM_TEST_WORKER_IO'}), flush=True)
            stopping.wait(.5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--allow-target', action='append', default=[], choices=sorted(STREAM_TARGETS | {'none', 'all'}),
                        help='Enable a platform, or all platforms with all; defaults to file tests only')
    parser.add_argument('--sample', type=Path)
    parser.add_argument('--api-port', type=int, default=18092)
    parser.add_argument('--web-port', type=int, default=13102)
    parser.add_argument('--hosts-action', choices=('install', 'restore'), help=argparse.SUPPRESS)
    parser.add_argument('--pin-host', help=argparse.SUPPRESS)
    parser.add_argument('--pin-address', action='append', default=[], help=argparse.SUPPRESS)
    args = parser.parse_args()
    runtime = api_server = api_thread = worker_thread = runtime_lock = None
    stopping = threading.Event()
    previous_umask = os.umask(0o077)
    try:
        require_test_vm()
        if args.hosts_action:
            if os.geteuid() != 0:
                raise LivePreviewError('PLATFORM_TEST_HOST_ROOT_REQUIRED')
            edit_host_pin(args.hosts_action, hostname=args.pin_host, addresses=args.pin_address)
            return 0
        if args.data_dir is None:
            raise LivePreviewError('PLATFORM_TEST_DATA_DIR_REQUIRED')
        allowed = allowed_targets(args.allow_target)
        if not 1024 <= args.web_port <= 65535 or args.web_port == args.api_port:
            raise LivePreviewError('PLATFORM_TEST_PORT_INVALID')
        preview.require_free_ports((args.api_port,))
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stopping.set())
        data_dir = private_directory(args.data_dir)
        acquired_lock = exclusive_runtime(data_dir, vm_lock_path=VM_MARKER)
        acquired_lock.__enter__()
        runtime_lock = acquired_lock
        # Recover a crash's hosts mutation before any API claim can resolve DNS.
        manage_host_pin('restore')
        runtime = build_application(data_dir, api_port=args.api_port, web_port=args.web_port, allowed=allowed)
        if args.sample:
            seed_sample(runtime, args.sample)
        logging.getLogger('replay').setLevel(logging.CRITICAL)
        logging.getLogger('replay.requests').disabled = True
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)
        api_server = uvicorn.Server(uvicorn.Config(runtime.app, host='127.0.0.1', port=args.api_port,
            access_log=False, log_level='critical'))
        api_thread = threading.Thread(target=api_server.run, daemon=True)
        api_thread.start()
        deadline = time.monotonic() + 15
        while not api_server.started and api_thread.is_alive() and time.monotonic() < deadline:
            stopping.wait(.05)
        if not api_server.started:
            raise LivePreviewError('PLATFORM_TEST_API_START_FAILED')
        worker_thread = threading.Thread(target=worker_loop, args=(runtime, stopping), daemon=True)
        worker_thread.start()
        print(json.dumps({'ready': True, 'api': runtime.cfg.public_url, 'web_origin': runtime.cfg.origins[0],
            'allowed_targets': sorted(allowed), 'external_streaming': bool(allowed & LIVE_TARGETS),
            'sequential': True, 'storage_limit_bytes': STORAGE_LIMIT, 'max_duration_seconds': MAX_DURATION,
            'pid': os.getpid(), 'persistent': True}), flush=True)
        while not stopping.wait(.5):
            if not api_thread.is_alive() or not worker_thread.is_alive():
                raise LivePreviewError('PLATFORM_TEST_SERVER_STOPPED')
        return 0
    except (LivePreviewError, MediaError) as error:
        print(json.dumps({'ready': False, 'error': error.code}), flush=True)
        return 1
    except Exception:
        print(json.dumps({'ready': False, 'error': 'PLATFORM_TEST_START_OR_RUNTIME_FAILED'}), flush=True)
        return 1
    finally:
        stopping.set()
        if runtime:
            try:
                preview.cancel_preview_jobs(runtime.repo)
            except Exception:
                pass
        if worker_thread and worker_thread.is_alive():
            worker_thread.join(timeout=30)
        # The original worker stops on cancellation/lease loss and has a bounded
        # child-process watchdog. Keep its API alive while that stop is confirmed.
        if runtime_lock and (not worker_thread or not worker_thread.is_alive()):
            try:
                manage_host_pin('restore')
            except LivePreviewError as error:
                print(json.dumps({'event': 'platform_test_cleanup_blocked', 'code': error.code}), flush=True)
        if api_server:
            api_server.should_exit = True
        if api_thread and api_thread.is_alive():
            api_thread.join(timeout=10)
        if runtime:
            runtime.repo.close()
        if runtime_lock:
            runtime_lock.__exit__(None, None, None)
        os.umask(previous_umask)


if __name__ == '__main__':
    raise SystemExit(main())
