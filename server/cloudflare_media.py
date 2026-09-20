"""One private Cloudflare container per lease; only the DO can route here."""
import os
import threading
import time

from fastapi import FastAPI, HTTPException, Request

from .request_boundary import BoundaryMiddleware
from .stream_targets import install_host_pin, validate_pinned_destination
from .worker import run_job


def create_media_app(*, runner=run_job, pin=install_host_pin):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()
    started = False

    @app.post('/run', status_code=202)
    async def start(request: Request):
        nonlocal started
        job = await request.json()
        if (not isinstance(job, dict) or job.get('version') != os.environ.get('REPLAY_VERSION')
                or job.get('mode') != 'production' or job.get('target') == 'import'
                or not isinstance(job.get('deadline'), (float, int))
                or not time.time() < job['deadline'] <= time.time() + 1200):
            raise HTTPException(400, 'INVALID_MEDIA_JOB')
        with lock:
            if started:
                raise HTTPException(409, 'ALREADY_STARTED')
            started = True
        # Per-job VM DNS pins keep RTMPS SNI and certificate verification intact.
        destination = job.get('stream_destination')
        if destination:
            checked = validate_pinned_destination(destination)
            pin(checked['hostname'], checked['addresses'])
        thread = threading.Thread(target=runner, args=(job,), daemon=True, name='media-job')
        thread.start()
        return {'started': True}

    app.add_middleware(BoundaryMiddleware, max_body_bytes=49152, body_timeout=10)
    return app
