"""Loopback-only recording downloader. Cloud/account credentials never enter this process."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, StrictInt

from .media_sources import SourceImportError, download_source, normalize_source
from .request_boundary import BoundaryMiddleware

PORT = 17833
DEFAULT_ORIGINS = ('https://replay-live.pages.dev', 'http://127.0.0.1:13101',
                   'http://127.0.0.1:3100', 'http://localhost:3100')
MAX_BYTES = 50 * 1024**2
MAX_DURATION = 120
SESSION_TTL = 8 * 3600
JOB_TTL = 30 * 60
logger = logging.getLogger('replay.local_import')


def valid_origin(value):
    parsed = urlsplit(value)
    if (value != f'{parsed.scheme}://{parsed.netloc}' or not parsed.hostname
            or parsed.username or parsed.password or parsed.hostname.endswith('.')
            or (parsed.scheme != 'https' and not (parsed.scheme == 'http'
                    and parsed.hostname in {'127.0.0.1', 'localhost'}))):
        raise ValueError('Use an exact HTTPS web origin or an HTTP loopback origin')
    return value


class CloudImport(BaseModel):
    model_config = {'extra': 'forbid'}
    id: str = Field(pattern=r'^[a-f0-9]{32}$')
    token: str = Field(min_length=32, max_length=2048, repr=False)


class Pair(BaseModel):
    model_config = {'extra': 'forbid'}
    code: str = Field(min_length=1, max_length=64, repr=False)
    client_id: str | None = Field(default=None, pattern=r'^[a-f0-9-]{36}$')


class Import(BaseModel):
    model_config = {'extra': 'forbid'}
    request_id: str = Field(pattern=r'^[a-f0-9-]{36}$')
    provider: str = Field(min_length=1, max_length=30)
    url: str = Field(min_length=1, max_length=4096, repr=False)
    max_bytes: StrictInt = Field(gt=0, le=MAX_BYTES)
    max_duration: StrictInt = Field(gt=0, le=MAX_DURATION)


@dataclass
class LocalJob:
    id: str
    owner: str
    request_id: str
    fingerprint: str
    directory: Path
    expires: float
    state: str = 'downloading'
    error_code: str | None = None
    result: dict = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)

    def public(self):
        return {'id': self.id, 'state': self.state, 'error_code': self.error_code,
                **self.result}


class LocalImports:
    def __init__(self, *, downloader=download_source, root=None, pairing_code=None, now=time.time,
                 cloud_url='https://replay-live-api.guswhd1085.workers.dev', cloud_client=None, development=False):
        from .device_import_worker import cloud_origin
        self.cloud_url = cloud_origin(cloud_url, development)
        self.cloud_client, self.development = cloud_client, development
        self.now, self.downloader = now, downloader
        self.root = Path(tempfile.mkdtemp(prefix='replay-local-import-', dir=root))
        self.root.chmod(0o700)
        self.pairing_code = pairing_code or secrets.token_hex(6).upper()
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='replay-import')
        self.sessions, self.jobs, self.browser_sessions = {}, {}, {}
        self.pair_attempts = []
        self.closing = False

    def pair(self, code, origin, client_id=None):
        with self.lock:
            self.sweep()
            self.pair_attempts = [at for at in self.pair_attempts if at > self.now() - 60]
            if len(self.pair_attempts) >= 5:
                raise HTTPException(429, '연결 시도가 많습니다. 1분 뒤 다시 시도하세요.')
            if not hmac.compare_digest(code.upper().encode(), self.pairing_code.encode()):
                self.pair_attempts.append(self.now())
                raise HTTPException(401, '도우미에 표시된 연결 코드를 확인하세요.')
            if client_id and (previous := self.browser_sessions.get((origin, client_id))):
                self.revoke(previous)
            if len(self.sessions) >= 8:
                raise HTTPException(409, '연결된 창이 많습니다. 사용하지 않는 창의 연결을 해제하세요.')
            token = secrets.token_urlsafe(32)
            key = hashlib.sha256(token.encode()).hexdigest()
            self.sessions[key] = (origin, self.now() + SESSION_TTL)
            if client_id: self.browser_sessions[(origin, client_id)] = key
            return {'token': token, 'expires_at': self.sessions[key][1], 'version': 1,
                    'features': ['cloud-direct-upload']}

    def authenticate(self, token, origin):
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            value = self.sessions.get(key)
            if not value or value[0] != origin or value[1] <= self.now():
                raise HTTPException(401, '내 컴퓨터 연결이 만료됐습니다. 다시 연결하세요.')
        return key

    def get(self, id, owner):
        job = self.jobs.get(id)
        if not job or job.owner != owner:
            raise HTTPException(404, '가져오기 작업을 찾을 수 없습니다.')
        return job

    def start(self, payload, owner):
        source = normalize_source(payload.provider, payload.url)
        fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        with self.lock:
            self.sweep()
            if self.closing:
                raise HTTPException(503, '도우미를 종료하고 있습니다.')
            for job in self.jobs.values():
                if job.owner == owner and job.request_id == payload.request_id:
                    if job.fingerprint != fingerprint:
                        raise HTTPException(409, '같은 요청 번호에 다른 영상을 등록할 수 없습니다.')
                    return job.public()
            if any(job.state in {'downloading', 'cancelling'} for job in self.jobs.values()):
                raise HTTPException(409, '다른 영상을 가져오고 있습니다. 완료 후 다시 시도하세요.')
            if len(self.jobs) >= 8 or sum(j.result.get('bytes', 0) for j in self.jobs.values()) + payload.max_bytes > 200 * 1024**2:
                raise HTTPException(409, '도우미 임시 공간이 가득 찼습니다. 완료한 작업을 정리하세요.')
            id = secrets.token_hex(16)
            directory = self.root / id
            directory.mkdir(mode=0o700)
            job = LocalJob(id, owner, payload.request_id, fingerprint, directory, self.now() + JOB_TTL)
            self.jobs[id] = job
            self.pool.submit(self.run, job, source, payload)
            return job.public()

    def start_cloud(self, payload, owner):
        fingerprint = hashlib.sha256(payload.token.encode()).hexdigest()
        with self.lock:
            self.sweep()
            existing = self.jobs.get(payload.id)
            if existing:
                if existing.owner != owner or existing.fingerprint != fingerprint:
                    raise HTTPException(409, '다른 연결의 가져오기 작업입니다.')
                return existing.public()
            if self.closing or len(self.jobs) >= 8 or any(j.state in {'downloading', 'cancelling'} for j in self.jobs.values()):
                raise HTTPException(409, '진행 중인 가져오기가 있습니다. 잠시 후 다시 시도하세요.')
            directory = self.root / payload.id
            directory.mkdir(mode=0o700)
            job = LocalJob(payload.id, owner, payload.id, fingerprint, directory, self.now() + JOB_TTL)
            self.jobs[job.id] = job
            self.pool.submit(self.run_cloud, job, payload.token)
            return job.public()

    def run_cloud(self, job, token):
        from .device_import_worker import run_device_import
        def active():
            if job.cancel.is_set() or self.now() >= job.expires:
                raise SourceImportError('SOURCE_CANCELLED')
        def phase(value):
            with self.lock:
                job.result['phase'] = value
        try:
            result = run_device_import(job.id, token, api=self.cloud_url,
                output=job.directory / 'recording.mp4', downloader=self.downloader,
                check_active=active, on_phase=phase, client=self.cloud_client, development=self.development)
            with self.lock:
                active()
                job.result = result
                job.state = 'ready'
        except Exception as error:
            with self.lock:
                job.state = 'cancelled' if job.cancel.is_set() else 'failed'
                job.error_code = error.code if isinstance(error, SourceImportError) else 'SOURCE_UNAVAILABLE'
        finally:
            shutil.rmtree(job.directory, ignore_errors=True)
            with self.lock:
                if job.cancel.is_set():
                    self.jobs.pop(job.id, None)

    def run(self, job, source, payload):
        logger.info(json.dumps({'event': 'local_import_started', 'job_id': job.id,
                               'provider': source['provider']}))
        def active():
            if job.cancel.is_set():
                raise SourceImportError('SOURCE_CANCELLED')
        try:
            result = self.downloader(source, job.directory / 'recording.mp4',
                max_bytes=payload.max_bytes, max_duration=payload.max_duration,
                timeout=120, check_active=active)
            active()
            with self.lock:
                active()
                job.result = {'bytes': result['bytes'], 'sha256': result['sha256']}
                job.state = 'ready'
        except Exception as error:
            with self.lock:
                job.state = 'cancelled' if job.cancel.is_set() else 'failed'
                job.error_code = error.code if isinstance(error, SourceImportError) else 'SOURCE_UNAVAILABLE'
        finally:
            with self.lock:
                logger.info(json.dumps({'event': 'local_import_finished', 'job_id': job.id,
                    'state': job.state, 'error_code': job.error_code, **job.result}))
                if job.cancel.is_set() or job.state != 'ready':
                    shutil.rmtree(job.directory, ignore_errors=True)
                if job.cancel.is_set():
                    self.jobs.pop(job.id, None)

    def remove(self, id, owner):
        with self.lock:
            job = self.get(id, owner)
            job.cancel.set()
            if job.state in {'downloading', 'cancelling'}:
                job.state = 'cancelling'
            else:
                shutil.rmtree(job.directory, ignore_errors=True)
                self.jobs.pop(id, None)

    def revoke(self, owner):
        with self.lock:
            self.sessions.pop(owner, None)
            for browser, key in list(self.browser_sessions.items()):
                if key == owner: self.browser_sessions.pop(browser, None)
            for job in list(self.jobs.values()):
                if job.owner == owner:
                    self.remove(job.id, owner)

    def sweep(self):
        with self.lock:
            for owner, (_, expires) in list(self.sessions.items()):
                if expires <= self.now():
                    self.revoke(owner)
            for job in list(self.jobs.values()):
                if job.expires <= self.now() or job.owner not in self.sessions:
                    self.remove(job.id, job.owner)

    def close(self):
        with self.lock:
            self.closing = True
            for job in self.jobs.values():
                job.cancel.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(self.root, ignore_errors=True)


def create_local_import_app(*, origins=DEFAULT_ORIGINS, manager=None, port=PORT):
    allowed = {valid_origin(origin) for origin in origins}
    service = manager or LocalImports()

    @asynccontextmanager
    async def lifespan(app):
        async def cleanup():
            while True:
                await asyncio.sleep(10)
                await asyncio.to_thread(service.sweep)
        task = asyncio.create_task(cleanup())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await asyncio.to_thread(service.close)

    app = FastAPI(title='Replay Live local import', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.imports = service

    @app.middleware('http')
    async def boundary(request, call_next):
        # Explicit Host and Origin checks stop DNS rebinding and ambient websites.
        origin = request.headers.get('origin', '')
        if request.headers.get('host') != f'127.0.0.1:{port}' or origin not in allowed:
            return JSONResponse({'detail': '허용되지 않은 웹 연결입니다.'}, status_code=403)
        if request.method == 'OPTIONS':
            requested = {v.strip().lower() for v in request.headers.get('access-control-request-headers', '').split(',') if v.strip()}
            if request.headers.get('access-control-request-method') not in {'GET', 'POST', 'DELETE'} or not requested <= {'content-type', 'x-replay-local', 'authorization'}:
                return Response(status_code=403)
            response = Response(status_code=204, headers={
                'Access-Control-Allow-Methods': 'GET, POST, DELETE',
                'Access-Control-Allow-Headers': 'Content-Type, X-Replay-Local, Authorization',
                'Access-Control-Allow-Private-Network': 'true', 'Access-Control-Max-Age': '600'})
        elif request.headers.get('x-replay-local') != '1':
            response = JSONResponse({'detail': '도우미 연결을 확인하세요.'}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update({'Access-Control-Allow-Origin': origin, 'Vary': 'Origin',
                                 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        return response

    app.add_middleware(BoundaryMiddleware, max_body_bytes=8192)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({'detail': '가져오기 요청 형식을 확인하세요.'}, status_code=422)

    @app.exception_handler(SourceImportError)
    async def source_error(request, error):
        return JSONResponse({'detail': error.code}, status_code=400)

    def owner(request: Request):
        authorization = request.headers.get('authorization', '')
        if not authorization.startswith('Bearer ') or len(authorization) > 128:
            raise HTTPException(401, '내 컴퓨터를 먼저 연결하세요.')
        return service.authenticate(authorization[7:], request.headers.get('origin'))

    @app.get('/pairing-code')
    def pairing_code():
        # The boundary above requires an exact trusted Origin, loopback Host,
        # and a non-simple header. This is intentionally available before pairing
        # so the trusted web UI can fill the current PC's code without persistence.
        return {'code': service.pairing_code, 'version': 1, 'features': ['tab-reconnect']}

    @app.post('/pair')
    def pair(payload: Pair, request: Request):
        return service.pair(payload.code, request.headers['origin'], payload.client_id)

    @app.get('/session')
    def session(user=Depends(owner)):
        return {'version': 1, 'max_bytes': MAX_BYTES, 'max_duration': MAX_DURATION}

    @app.delete('/session', status_code=204)
    def disconnect(user=Depends(owner)):
        service.revoke(user)
        return Response(status_code=204)

    @app.post('/imports', status_code=202)
    def start(payload: Import, user=Depends(owner)):
        return service.start(payload, user)

    @app.post('/cloud-imports', status_code=202)
    def start_cloud(payload: CloudImport, user=Depends(owner)):
        return service.start_cloud(payload, user)

    @app.get('/imports/{id}')
    def status(id: str, user=Depends(owner)):
        with service.lock:
            service.sweep()
            return service.get(id, user).public()

    @app.delete('/imports/{id}', status_code=204)
    def remove(id: str, user=Depends(owner)):
        service.remove(id, user)
        return Response(status_code=204)

    @app.get('/imports/{id}/file')
    def file(id: str, user=Depends(owner)):
        with service.lock:
            service.sweep()
            job = service.get(id, user)
            if job.result.get('media_id'):
                raise HTTPException(409, '보관함에 직접 업로드한 영상입니다.')
            if job.state != 'ready':
                raise HTTPException(409, '영상 다운로드가 끝나지 않았습니다.')
            return FileResponse(job.directory / 'recording.mp4', media_type='video/mp4',
                                filename='recording.mp4')

    return app
