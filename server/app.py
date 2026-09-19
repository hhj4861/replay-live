import os
from pathlib import Path
import shutil
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.middleware.cors import CORSMiddleware
from .public_access import PublicAccess, PUBLIC_UPLOAD, PUBLIC_DURATION
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .service import Service
from .request_boundary import BoundaryMiddleware
from .telemetry import configure_telemetry

BASE = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 500 * 1024 * 1024


class Broadcast(BaseModel):
    media_id: str
    title: str = Field(min_length=1, max_length=120)
    target: Literal['local', 'youtube'] = 'local'
    stream_key: str = Field(default='', max_length=160, repr=False)
    scheduled_at: datetime | None = None


class TestLogin(BaseModel):
    code: str = Field(min_length=1, max_length=200, repr=False)
    resume_token: str | None = Field(default=None, max_length=200, repr=False)


def create_app(data_dir=None, public_origins=None, invite_code=None, allowed_hosts=None, deadline_file=None):
    configure_telemetry()
    public = public_origins is not None
    max_upload = PUBLIC_UPLOAD if public else MAX_UPLOAD
    origins = public_origins if public else ['http://localhost:3000', 'http://127.0.0.1:3000', 'http://localhost:8080', 'http://127.0.0.1:8080']

    def service(request):
        return request.state.service if public else app.state.service

    @asynccontextmanager
    async def lifespan(app):
        if public:
            if not data_dir or Path(data_dir).resolve() == (BASE / 'data').resolve():
                raise RuntimeError('외부 테스트에는 별도의 데이터 폴더를 지정하세요.')
            app.state.access = PublicAccess(Path(data_dir), invite_code)
        else:
            app.state.service = Service(Path(data_dir or os.environ.get('REPLAY_DATA', BASE / 'data')))
            app.state.service.start()
        yield
        await run_in_threadpool(app.state.access.close if public else app.state.service.close)

    app = FastAPI(title='Replay Live POC', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or (['localhost', '127.0.0.1', 'testserver', '*.trycloudflare.com'] if public else ['localhost', '127.0.0.1', 'testserver']))

    def cloud_deadline():
        if not deadline_file:
            return None
        try:
            return float(Path(deadline_file).read_text().strip())
        except (OSError, ValueError):
            raise HTTPException(503, '테스트 서버의 실행 시간을 확인할 수 없습니다. 다시 접속하세요.') from None

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        upload_slot = False
        if request.url.path.startswith('/api') and request.method != 'OPTIONS':
            origin = request.headers.get('origin')
            if origin and origin not in origins:
                return JSONResponse({'detail': '허용되지 않은 요청 출처입니다.'}, status_code=403)
            if request.method not in ('GET', 'HEAD') and request.headers.get('x-replay-client') != '1':
                return JSONResponse({'detail': '관리 화면에서 요청하세요.'}, status_code=403)
            if public and request.url.path != '/api/session':
                try:
                    request.state.service = app.state.access.authenticate(request.headers.get('authorization', ''))
                except HTTPException as exc:
                    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
            if public and request.method == 'POST':
                maximum = max_upload + 1024 * 1024 if request.url.path == '/api/media' else 4096
                try:
                    length = int(request.headers.get('content-length', '0' if request.url.path.endswith('/stop') else '-1'))
                except ValueError:
                    length = -1
                if not 0 <= length <= maximum:
                    return JSONResponse({'detail': '요청 크기가 허용 범위를 초과했거나 Content-Length가 없습니다.'}, status_code=413)
                if request.url.path == '/api/media':
                    upload_slot = app.state.access.uploads.acquire(blocking=False)
                    if not upload_slot:
                        return JSONResponse({'detail': '다른 업로드가 진행 중입니다. 잠시 후 다시 시도하세요.'}, status_code=429)
        try:
            response = await call_next(request)
        finally:
            if upload_slot:
                app.state.access.uploads.release()
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    # Pydantic's default response includes the submitted input; strip it to avoid echoing keys.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail': '입력 형식이 올바르지 않습니다. 필수 값과 예약 시간을 확인하세요.'}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({'detail': '항목을 찾을 수 없습니다.'}, status_code=404)

    @app.post('/api/session')
    def login(payload: TestLogin, request: Request):
        if not public:
            raise HTTPException(404, '로컬 모드에서는 접속 코드가 필요하지 않습니다.')
        return app.state.access.login(payload.code, client_key=request.client.host if request.client else 'unknown', resume_token=payload.resume_token)

    @app.get('/api/health')
    def health(request: Request):
        readiness = service(request).readiness()
        return {**readiness, 'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')),
                'max_upload_mb': max_upload // 1024 // 1024, 'max_concurrent': 1, 'public_test': public, 'max_duration_seconds': PUBLIC_DURATION if public else 14400,
                'server_expires_at': cloud_deadline()}

    @app.get('/api/media')
    def media(request: Request):
        return service(request).list_media()

    @app.post('/api/media', status_code=201)
    async def upload(request: Request, file: UploadFile = File()):
        path = service(request).root / 'media' / f'{uuid.uuid4().hex}.mp4'
        try:
            if not file.filename or not file.filename.lower().endswith('.mp4'):
                raise ValueError('MP4 파일을 선택하세요.')
            size = 0
            with path.open('xb') as output:
                os.chmod(path, 0o600)
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_upload:
                        raise HTTPException(413, f'파일은 최대 {max_upload // 1024 // 1024}MB까지 업로드할 수 있습니다.')
                    output.write(chunk)
            return await run_in_threadpool(service(request).add_media, path, file.filename)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.get('/api/media/{media_id}/preview')
    def preview(media_id: str, request: Request):
        with service(request).db() as db:
            row = db.execute('SELECT path FROM media WHERE id=?', (media_id,)).fetchone()
        if not row:
            raise HTTPException(404, '영상이 없습니다.')
        return FileResponse(row['path'], media_type='video/mp4')

    @app.get('/api/broadcasts')
    def broadcasts(request: Request):
        return service(request).list_jobs()

    @app.post('/api/broadcasts', status_code=201)
    def create(payload: Broadcast, request: Request):
        deadline = cloud_deadline()
        if deadline is not None:
            import time
            start_at = payload.scheduled_at.timestamp() if payload.scheduled_at else time.time()
            if start_at + PUBLIC_DURATION + 60 > deadline:
                raise ValueError('테스트 서버 종료 3분 전까지만 방송을 시작할 수 있습니다. 서버 종료 후 다시 접속하세요.')
        if public and payload.scheduled_at is not None:
            import time
            if payload.scheduled_at.timestamp() > time.time() + 3600:
                raise ValueError('외부 테스트 예약은 1시간 이내로 선택하세요.')
        if payload.scheduled_at is not None and payload.scheduled_at.tzinfo is None:
            raise ValueError('예약 시간에 시간대 정보가 필요합니다.')
        return service(request).create_job(payload.media_id, payload.title, payload.target, payload.stream_key,
                                            payload.scheduled_at.timestamp() if payload.scheduled_at else None,
                                            idempotency_key=request.headers.get('idempotency-key'), deadline=deadline)

    @app.get('/api/broadcasts/{job_id}')
    def broadcast(job_id: str, request: Request):
        return service(request).get_job(job_id)

    @app.post('/api/broadcasts/{job_id}/stop')
    def stop(job_id: str, request: Request):
        return service(request).stop(job_id)

    @app.get('/api/broadcasts/{job_id}/events')
    def events(job_id: str, request: Request):
        return service(request).events(job_id)

    @app.get('/api/broadcasts/{job_id}/output')
    def output(job_id: str, request: Request):
        job = service(request).get_job(job_id)
        if job['target'] != 'local' or job['state'] != 'completed':
            raise HTTPException(409, '완료된 로컬 송출만 다운로드할 수 있습니다.')
        path = service(request).root / 'outputs' / f"{job_id}-{job['attempt']}.flv"
        return FileResponse(path, media_type='video/x-flv', filename=f'replay-{job_id[:8]}.flv')

    app.add_middleware(BoundaryMiddleware, max_body_bytes=4096,
                       path_limits={'/api/media': max_upload + 1024 * 1024}, body_timeout=120)
    if public:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=['GET', 'POST', 'HEAD', 'OPTIONS'], allow_headers=['Authorization', 'Content-Type', 'X-Replay-Client', 'Range', 'Idempotency-Key'], expose_headers=['Content-Range', 'Accept-Ranges', 'Content-Length'])
    return app


app = create_app()
