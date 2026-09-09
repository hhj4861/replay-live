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
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .service import Service

BASE = Path(__file__).resolve().parent.parent
MAX_UPLOAD = 500 * 1024 * 1024


class Broadcast(BaseModel):
    media_id: str
    title: str = Field(min_length=1, max_length=120)
    target: Literal['local', 'youtube'] = 'local'
    stream_key: str = Field(default='', max_length=160, repr=False)
    scheduled_at: datetime | None = None


def create_app(data_dir=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(Path(data_dir or os.environ.get('REPLAY_DATA', BASE / 'data')))
        app.state.service.start()
        yield
        await run_in_threadpool(app.state.service.close)

    app = FastAPI(title='Replay Live POC', lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'testserver'])

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            # Header cannot be sent by a cross-origin HTML form; CORS is deliberately disabled.
            if request.headers.get('x-replay-client') != '1' or (origin and origin not in (
                'http://localhost:3000', 'http://127.0.0.1:3000',
                'http://localhost:8080', 'http://127.0.0.1:8080')):
                return JSONResponse({'detail': '로컬 관리 화면에서 요청하세요.'}, status_code=403)
        response = await call_next(request)
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

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')),
                'max_upload_mb': MAX_UPLOAD // 1024 // 1024, 'max_concurrent': 1}

    @app.get('/api/media')
    def media():
        return app.state.service.list_media()

    @app.post('/api/media', status_code=201)
    async def upload(file: UploadFile = File()):
        path = app.state.service.root / 'media' / f'{uuid.uuid4().hex}.mp4'
        try:
            if not file.filename or not file.filename.lower().endswith('.mp4'):
                raise ValueError('MP4 파일을 선택하세요.')
            size = 0
            with path.open('xb') as output:
                os.chmod(path, 0o600)
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, '파일은 최대 500MB까지 업로드할 수 있습니다.')
                    output.write(chunk)
            return await run_in_threadpool(app.state.service.add_media, path, file.filename)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @app.get('/api/media/{media_id}/preview')
    def preview(media_id: str):
        with app.state.service.db() as db:
            row = db.execute('SELECT path FROM media WHERE id=?', (media_id,)).fetchone()
        if not row:
            raise HTTPException(404, '영상이 없습니다.')
        return FileResponse(row['path'], media_type='video/mp4')

    @app.get('/api/broadcasts')
    def broadcasts():
        return app.state.service.list_jobs()

    @app.post('/api/broadcasts', status_code=201)
    def create(payload: Broadcast):
        if payload.scheduled_at is not None and payload.scheduled_at.tzinfo is None:
            raise ValueError('예약 시간에 시간대 정보가 필요합니다.')
        return app.state.service.create_job(payload.media_id, payload.title, payload.target, payload.stream_key,
                                            payload.scheduled_at.timestamp() if payload.scheduled_at else None)

    @app.get('/api/broadcasts/{job_id}')
    def broadcast(job_id: str):
        return app.state.service.get_job(job_id)

    @app.post('/api/broadcasts/{job_id}/stop')
    def stop(job_id: str):
        return app.state.service.stop(job_id)

    @app.get('/api/broadcasts/{job_id}/events')
    def events(job_id: str):
        return app.state.service.events(job_id)

    @app.get('/api/broadcasts/{job_id}/output')
    def output(job_id: str):
        job = app.state.service.get_job(job_id)
        if job['target'] != 'local' or job['state'] != 'completed':
            raise HTTPException(409, '완료된 로컬 송출만 다운로드할 수 있습니다.')
        path = app.state.service.root / 'outputs' / f"{job_id}-{job['attempt']}.flv"
        return FileResponse(path, media_type='video/x-flv', filename=f'replay-{job_id[:8]}.flv')

    return app


app = create_app()
