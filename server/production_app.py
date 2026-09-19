"""Stateless commercial control plane; no scheduler, decoder or secrets on API disk."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, StrictBool
import jwt

from starlette.concurrency import run_in_threadpool
from .access_policy import AccessPolicy
from .operations import Operations
from .auth import AuthConfig, JWTAuthenticator, Principal
from .google_auth import GoogleAuthConfig, GoogleAuthenticator
from .member_management import MemberManagement
from .aws_identity import VercelAWSCredentials, VercelOIDCMiddleware, AWSIdentityError
from .output_policy import estimate_output_bytes
from .request_boundary import BoundaryMiddleware
from .repository import Repository, RepositoryError, NotFound, Conflict, PROCESSING_TARGETS
from .media_sources import SourceImportError, normalize_source, source_platforms
from .secrets import AWSKMSKeyProvider, LocalKeyringProvider, EnvironmentAESGCMKeyProvider
from .blob_storage import VercelBlobStorage
from .dispatch_wakeup import DispatchWakeup
from .settings import Settings
from .telemetry import configure_telemetry, event
from .storage import S3Storage, LocalStorage
from .stream_targets import StreamTargetError, platform_metadata, validate_destination, pin_destination
from .stream_connections import StreamConnections
from .watch_links import WatchLinkError, normalize_watch_links
from .device_imports import install_device_imports


class UploadIntent(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class MediaImport(BaseModel):
    provider: str = Field(min_length=1, max_length=30)
    url: str = Field(min_length=1, max_length=4096, repr=False)
    name: str | None = Field(default=None, min_length=1, max_length=180)


class GoogleExchange(BaseModel):
    credential: str = Field(min_length=1, max_length=12000, repr=False)
    challenge_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,128}$', repr=False)
    challenge_secret: str = Field(pattern=r'^[A-Za-z0-9_-]{16,128}$', repr=False)


class Destination(BaseModel):
    target: Literal['local', 'youtube', 'twitch', 'facebook', 'instagram', 'tiktok',
                    'naver', 'chzzk', 'kick', 'custom'] = 'local'
    server_url: str = Field(default='', max_length=2048, repr=False)
    stream_key: str = Field(default='', max_length=1024, repr=False)
    channel_url: str = Field(default='', max_length=2048, repr=False)
    broadcast_url: str = Field(default='', max_length=2048, repr=False)


class Broadcast(Destination):
    media_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    scheduled_at: datetime | None = None
    max_attempts: int = Field(default=3, ge=1, le=3)


class StreamConnectionIntent(BaseModel):
    model_config = {'extra': 'forbid'}
    server_url: str = Field(default='', max_length=2048, repr=False)
    stream_key: str = Field(min_length=1, max_length=1024, repr=False)
    channel_url: str = Field(default='', max_length=2048, repr=False)


class WatchLinksIntent(BaseModel):
    model_config = {'extra': 'forbid'}
    channel_url: str = Field(default='', max_length=2048, repr=False)
    broadcast_url: str = Field(default='', max_length=2048, repr=False)


class MemberStatusIntent(BaseModel):
    model_config = {'extra': 'forbid'}
    enabled: StrictBool


class BroadcastBatch(BaseModel):
    media_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    scheduled_at: datetime | None = None
    max_attempts: int = Field(default=3, ge=1, le=3)
    destinations: list[Destination] = Field(min_length=1, max_length=10, repr=False)


class Heartbeat(BaseModel):
    progress: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class Finish(Heartbeat):
    state: Literal['completed', 'failed', 'stopped', 'retry_wait']
    error_code: str | None = Field(default=None, pattern=r'^[A-Z][A-Z0-9_]{0,79}$')
    metadata: dict | None = None
    output_bytes: int | None = Field(default=None, gt=0)
    output_sha256: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')


class OutputIntent(BaseModel):
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class Claim(BaseModel):
    worker_id: str = Field(pattern=r'^[a-zA-Z0-9._-]{1,120}$')
    version: str = Field(max_length=100)


def create_production_app(settings=None, *, repository=None, storage=None, keys=None, authenticator=None):
    configure_telemetry()
    cfg = settings or Settings.from_env()
    wakeup = DispatchWakeup(cfg)
    workload = VercelAWSCredentials(cfg.aws_role_arn, cfg.region, issuer=cfg.aws_oidc_issuer,
                 audience=cfg.aws_oidc_audience, subject=cfg.aws_oidc_subject) if cfg.aws_auth_mode == 'vercel_oidc' else None
    repo = repository or Repository(cfg.database_url, tenant_concurrency=cfg.tenant_concurrency,
            global_concurrency=cfg.global_concurrency, max_storage_bytes=cfg.max_storage_bytes,
            max_output_bytes=cfg.max_output_bytes,
            max_pending_jobs=cfg.max_pending_jobs, validation_duration=cfg.validation_timeout,
            reservation_grace=cfg.reservation_grace, create_schema=False)
    if storage is not None:
        objects = storage
    elif cfg.mode == 'development':
        objects = LocalStorage(Path(cfg.local_root) / 'objects', signing_key=cfg.callback_key,
                               base_url=cfg.public_url, allow_development=True)
    elif cfg.storage_provider == 'vercel-blob':
        objects = VercelBlobStorage(control_url=cfg.blob_control_url, control_token=cfg.control_token)
    else:
        objects = S3Storage(cfg.bucket, kms_key_id=cfg.kms_key_id, region_name=cfg.region,
                            client=workload.client('s3') if workload else None)
    if cfg.max_output_bytes > objects.max_put_bytes:
        raise ValueError('Output ceiling exceeds the configured object storage upload capability')
    if keys is not None:
        secrets = keys
    elif cfg.mode == 'development':
        secrets = LocalKeyringProvider(json.loads(os.environ['REPLAY_DEV_KEYRING']), os.environ['REPLAY_DEV_ACTIVE_KEY'], mode='development')
    elif cfg.secret_provider == 'env-aesgcm':
        secrets = EnvironmentAESGCMKeyProvider.from_env()
    else:
        secrets = AWSKMSKeyProvider(cfg.kms_key_id, region_name=cfg.region,
                                    client=workload.client('kms') if workload else None)
    login_provider = os.getenv('REPLAY_LOGIN_PROVIDER', 'oidc')
    if authenticator is None and login_provider not in ('oidc', 'google'):
        raise ValueError('REPLAY_LOGIN_PROVIDER must be oidc or google')
    auth = authenticator or (GoogleAuthenticator(repo.engine, GoogleAuthConfig.from_env(), mode=cfg.mode)
                            if login_provider == 'google' else JWTAuthenticator(AuthConfig.from_env()))
    if cfg.mode == 'production' and auth.config.mode != 'production':
        raise ValueError('Production cannot use development authentication')

    @asynccontextmanager
    async def lifespan(app):
        yield
        await auth.close()
        repo.close()
        if workload:
            workload.close()
        if isinstance(objects, VercelBlobStorage):
            objects.close()
        wakeup.close()

    app = FastAPI(title='Replay Live', version=cfg.version, lifespan=lifespan,
                  docs_url=None if cfg.mode == 'production' else '/docs', redoc_url=None)
    policy = AccessPolicy(repo.engine, mode=cfg.mode)
    operations = Operations(repo.engine)
    app.state.repository, app.state.storage, app.state.settings = repo, objects, cfg
    app.state.access_policy = policy
    app.state.operations = operations
    stream_connections = StreamConnections(repo.engine, secrets, mode=cfg.mode)
    app.state.stream_connections = stream_connections
    members = MemberManagement(auth, repo) if isinstance(auth, GoogleAuthenticator) else None
    app.state.members = members
    if members is not None:
        repo.admission_guard = members.check_admission

    def notify_dispatch(*, required=False, health=False):
        if not wakeup.notify(health=health):
            event('dispatch_wakeup_deferred', version=cfg.version, code='QUEUE_UNAVAILABLE')
            if required:
                # The committed job remains durable. The same idempotency key
                # retries notification without creating a second broadcast.
                raise HTTPException(503, '작업은 저장됐지만 실행 연결이 지연되고 있습니다. 잠시 후 다시 시도하세요.')

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, exc):
        return JSONResponse({'detail': '입력 형식이 올바르지 않습니다.'}, status_code=422)

    @app.exception_handler(RepositoryError)
    async def repository_error(request, exc):
        status = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 400
        code = getattr(exc, 'code', type(exc).__name__)
        messages = {
            'OUTPUT_LIMIT_EXCEEDED': '예상 결과 파일이 허용 크기를 초과합니다. 더 짧은 영상으로 다시 시도하세요.',
            'OUTPUT_STORAGE_QUOTA_EXCEEDED': '결과 파일을 저장할 공간이 부족합니다. 보관 파일을 정리하거나 저장 한도를 늘려주세요.',
        }
        return JSONResponse({'detail': messages.get(code, str(exc)), 'code': code}, status_code=status)

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({'detail': '요청 값 또는 저장된 자료를 확인할 수 없습니다.'}, status_code=400)

    @app.exception_handler(StreamTargetError)
    async def invalid_stream_target(request, exc):
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=400)

    @app.exception_handler(WatchLinkError)
    async def invalid_watch_link(request, exc):
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=400)

    @app.exception_handler(SourceImportError)
    async def invalid_media_source(request, exc):
        return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=400)

    @app.exception_handler(AWSIdentityError)
    async def identity_unavailable(request, exc):
        return JSONResponse({'detail': '저장소 접근 권한을 갱신하지 못했습니다. 잠시 후 다시 시도하세요.'}, status_code=503)

    @app.middleware('http')
    async def headers(request, call_next):
        origin = request.headers.get('origin')
        if origin and origin not in cfg.origins:
            return JSONResponse({'detail': '허용되지 않은 요청 출처입니다.'}, status_code=403)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer', 'X-Replay-Version': cfg.version})
        return response

    def google_request(request: Request):
        if not isinstance(auth, GoogleAuthenticator):
            raise HTTPException(404, 'Google 로그인이 설정되지 않았습니다.')
        if request.headers.get('origin') not in cfg.origins or request.headers.get('x-replay-client') != '1':
            raise HTTPException(403, '허용된 로그인 화면에서 다시 시도하세요.')
        if request.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json':
            raise HTTPException(415, '로그인 요청 형식을 확인하세요.')

    @app.post('/api/auth/google/challenge')
    async def google_challenge(request: Request):
        google_request(request)
        return await run_in_threadpool(auth.challenge, request.client.host if request.client else 'unknown')

    @app.post('/api/auth/google/exchange')
    async def google_exchange(payload: GoogleExchange, request: Request):
        google_request(request)
        return await auth.exchange(payload.credential, payload.challenge_id, payload.challenge_secret)

    @app.post('/api/auth/google/refresh')
    async def google_refresh(request: Request):
        google_request(request)
        return await run_in_threadpool(auth.refresh, request.headers.get('authorization', ''))

    async def principal(request: Request):
        user = await auth.authenticate(request.headers.get('authorization', ''), request.headers.get('x-replay-tenant'))
        await run_in_threadpool(policy.check, user)
        request.state.principal = user
        return user

    def writer(user: Principal = Depends(principal)):
        if not user.has_role('operator'):
            raise HTTPException(403, '방송 운영 권한이 필요합니다.')
        return user

    def site_administrator(user: Principal = Depends(principal)):
        if members is None:
            raise HTTPException(404, '회원 관리가 설정되지 않았습니다.')
        if 'site_admin' not in user.roles:
            raise HTTPException(403, '사이트 관리자 권한이 필요합니다.')
        return user

    @app.get('/api/admin/members')
    def member_list(q: str = Query(default='', max_length=254), offset: int = Query(default=0, ge=0),
                    limit: int = Query(default=25, ge=1, le=100), user=Depends(site_administrator)):
        return members.list(user, q=q, offset=offset, limit=limit)

    @app.get('/api/admin/members/{member_id}')
    def member_detail(member_id: str, user=Depends(site_administrator)):
        return members.detail(user, member_id)

    @app.put('/api/admin/members/{member_id}/status')
    def member_status(member_id: str, payload: MemberStatusIntent, user=Depends(site_administrator)):
        result = members.set_enabled(user, member_id, payload.enabled)
        if not payload.enabled:
            notify_dispatch()
        return result

    @app.post('/api/admin/members/{member_id}/revoke-sessions')
    def member_revoke_sessions(member_id: str, user=Depends(site_administrator)):
        return members.revoke_sessions(user, member_id)

    @app.delete('/api/admin/members/{member_id}/stream-connections/{target}', status_code=204)
    def member_delete_connection(member_id: str, target: str, user=Depends(site_administrator)):
        members.delete_connection(user, member_id, target)
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    @app.get('/api/stream-connections')
    def saved_stream_connections(user=Depends(writer)):
        return stream_connections.list(user)

    @app.put('/api/stream-connections/{target}')
    def save_stream_connection(target: str, payload: StreamConnectionIntent, user=Depends(writer)):
        return stream_connections.save(user, target, server_url=payload.server_url, stream_key=payload.stream_key,
                                       channel_url=payload.channel_url)

    @app.post('/api/stream-connections/{target}/use')
    def use_stream_connection(target: str, user=Depends(writer)):
        return JSONResponse(stream_connections.use(user, target), headers={'Cache-Control': 'no-store'})

    @app.delete('/api/stream-connections/{target}', status_code=204)
    def delete_stream_connection(target: str, user=Depends(writer)):
        stream_connections.delete(user, target)
        return Response(status_code=204, headers={'Cache-Control': 'no-store'})

    def control(request: Request):
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + cfg.control_token):
            raise HTTPException(401, '실행 조정자 인증이 필요합니다.')

    def callback(job_id: str, request: Request):
        try:
            token = request.headers.get('authorization', '').removeprefix('Bearer ')
            claims = jwt.decode(token, cfg.callback_key, algorithms=['HS256'], audience='replay-worker',
                                options={'require': ['exp', 'job_id', 'tenant_id', 'lease_token']})
            if claims['job_id'] != job_id:
                raise ValueError()
            return claims
        except (jwt.PyJWTError, ValueError):
            raise HTTPException(401, '작업 실행 권한이 만료되었거나 일치하지 않습니다.') from None

    def public_media(item):
        return {key: value for key, value in item.items() if key not in ('object_key', 'sha256', 'etag', 'tenant_id')}

    def public_job(item):
        hidden = {'secret_ciphertext', 'lease_token', 'payload_hash', 'idempotency_key', 'worker_id', 'output_key', 'tenant_id'}
        return {key: value for key, value in item.items() if key not in hidden}

    @app.get('/api/live')
    def live():
        return {'status': 'alive', 'version': cfg.version}

    @app.get('/api/health')
    def health(user=Depends(principal)):
        try:
            repo.ping()
            objects.health()
            workers = repo.worker_health(max_age=150)
            if not workers.get('alive'):
                notify_dispatch(health=True)
            ready = bool(workers.get('alive')) and not cfg.draining
        except Exception:
            return JSONResponse({'ready': False, 'status': 'database_unavailable', 'version': cfg.version}, status_code=503)
        return JSONResponse({'ready': ready, 'status': 'ready' if ready else 'worker_unavailable', 'version': cfg.version,
                'workers': workers, 'max_upload_mb': cfg.max_upload_bytes // 1024**2,
                'max_duration_seconds': cfg.max_duration, 'max_output_bytes': cfg.max_output_bytes,
                'max_concurrent': min(repo.tenant_concurrency, repo.global_concurrency),
                'retention_days': cfg.retention_days, 'commercial': True}, status_code=200 if ready else 503)

    @app.get('/api/me')
    def me(user=Depends(principal)):
        return {'subject': user.subject, 'tenant_id': user.tenant_id, 'roles': user.roles,
                'profile': members.profile(user) if members is not None else None,
                'permissions': {'manage_members': members is not None and 'site_admin' in user.roles}}

    @app.post('/api/logout')
    async def logout(request: Request):
        if isinstance(auth, GoogleAuthenticator):
            google_request(request)
            await run_in_threadpool(auth.logout, request.headers.get('authorization', ''))
            return {'status': 'signed_out'}
        user = await auth.authenticate(request.headers.get('authorization', ''))
        await run_in_threadpool(policy.revoke, user)
        return {'status': 'signed_out'}

    @app.get('/api/usage')
    def usage(user=Depends(principal)):
        return repo.usage(user.tenant_id)

    @app.get('/api/media')
    def media_list(user=Depends(principal)):
        return [public_media(item) for item in repo.list_media(user.tenant_id)]

    @app.get('/api/media-sources')
    def media_sources(user=Depends(principal)):
        return {'sources': source_platforms()}

    @app.post('/api/media/imports', status_code=202)
    def import_media(payload: MediaImport, request: Request, user=Depends(writer)):
        if cfg.mode == 'production':
            raise HTTPException(409, '영상 링크는 내 컴퓨터의 도우미로 가져옵니다. 화면을 새로고침하고 도우미를 연결해 주세요.')
        policy.check(user, action='upload')
        if cfg.draining:
            raise HTTPException(503, '현재 점검 중입니다. 잠시 후 다시 시도하세요.')
        idem = request.headers.get('idempotency-key', '')
        if not re.fullmatch(r'[a-zA-Z0-9._:-]{8,200}', idem):
            raise HTTPException(400, '중복 등록 방지 키가 필요합니다.')
        source = normalize_source(payload.provider, payload.url)
        name = payload.name.strip() if payload.name is not None else '가져온 녹화영상.mp4'
        encoded = json.dumps(source, sort_keys=True, separators=(',', ':'))
        fingerprint = hmac.new(cfg.callback_key.encode(), json.dumps(
            {'operation': 'import', 'source': source, 'name': name}, sort_keys=True,
            separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
        ciphertext = secrets.encrypt(encoded, context={'tenant_id': user.tenant_id})
        identifier = uuid.uuid4().hex
        result = repo.create_import(user.tenant_id, id=identifier, name=name,
            object_key=objects.key(user.tenant_id, identifier), secret_ciphertext=ciphertext,
            idempotency_key=idem, request_fingerprint=fingerprint,
            max_bytes=min(cfg.max_upload_bytes, objects.max_put_bytes))
        notify_dispatch(required=True)
        return {'media': public_media(result['media']), 'job': public_job(result['job'])}

    @app.post('/api/uploads', status_code=201)
    def upload_intent(payload: UploadIntent, user=Depends(writer)):
        policy.check(user, action='upload')
        if cfg.draining:
            raise HTTPException(503, '현재 점검 중입니다. 잠시 후 다시 시도하세요.')
        if payload.bytes > cfg.max_upload_bytes or not payload.name.lower().endswith('.mp4'):
            raise HTTPException(413, '허용된 크기의 MP4 파일을 선택하세요.')
        identifier = uuid.uuid4().hex
        key = objects.key(user.tenant_id, identifier)
        # No URL is returned until quota reservation commits. Signing failures must
        # not leave invisible uploading records or consume the customer's quota.
        signed = objects.presign_upload(user.tenant_id, key, size=payload.bytes, sha256=payload.sha256)
        row = repo.add_media(user.tenant_id, id=identifier, name=payload.name, object_key=key,
                             bytes=payload.bytes, sha256=payload.sha256, status='uploading')
        return {'media': public_media(row), 'upload': signed}

    @app.post('/api/uploads/{media_id}/complete', status_code=202)
    def upload_complete(media_id: str, user=Depends(writer)):
        item = repo.get_media(user.tenant_id, media_id)
        if item['status'] == 'ready':
            return public_media(item)
        objects.verify_upload(user.tenant_id, item['object_key'], size=item['bytes'], sha256=item['sha256'])
        repo.create_job(user.tenant_id, media_id=media_id, title='영상 검사', target='validate',
                        idempotency_key='validate:' + media_id, max_attempts=1,
                        request_fingerprint=hashlib.sha256(('validate:' + media_id).encode()).hexdigest())
        notify_dispatch(required=True)
        return public_media(repo.get_media(user.tenant_id, media_id))

    @app.delete('/api/media/{media_id}', status_code=202)
    def delete_media(media_id: str, user=Depends(writer)):
        repo.delete_media(user.tenant_id, media_id)
        notify_dispatch()
        return {'status': 'deletion_queued'}

    @app.get('/api/media/{media_id}/preview')
    def preview(media_id: str, user=Depends(principal)):
        item = repo.get_media(user.tenant_id, media_id)
        if item['status'] != 'ready':
            raise HTTPException(409, '영상 검사가 완료되지 않았습니다.')
        return objects.presign_download(user.tenant_id, item['object_key'], expires=300)

    @app.get('/api/media/{media_id}/output-estimate')
    def output_estimate(media_id: str, user=Depends(principal)):
        item = repo.get_media(user.tenant_id, media_id)
        if item['status'] != 'ready':
            raise HTTPException(409, '영상 검사가 완료되지 않았습니다.')
        required = estimate_output_bytes(item['duration'])
        usage = repo.usage(user.tenant_id)
        available = max(0, usage['storage_limit_bytes'] - usage['storage_bytes'])
        reason = ('OUTPUT_LIMIT_EXCEEDED' if required > repo.max_output_bytes else
                  'OUTPUT_STORAGE_QUOTA_EXCEEDED' if required > available else None)
        # An informational quote only: create_job owns the atomic reservation.
        return {'media_id': media_id, 'estimated_output_bytes': required,
                'max_output_bytes': repo.max_output_bytes, 'storage_available_bytes': available,
                'can_create': reason is None, 'reason': reason}

    @app.get('/api/broadcasts')
    def broadcasts(user=Depends(principal)):
        return [public_job(item) for item in repo.list_jobs(user.tenant_id) if item['target'] not in PROCESSING_TARGETS]

    @app.get('/api/stream-targets')
    def stream_targets(user=Depends(principal)):
        return {'targets': platform_metadata(),
                'max_destinations': min(repo.tenant_concurrency, repo.global_concurrency, 10)}

    def broadcast_request(payload, request, user):
        policy.check(user, action='broadcast')
        if cfg.draining:
            raise HTTPException(503, '현재 점검 중입니다. 잠시 후 다시 시도하세요.')
        idem = request.headers.get('idempotency-key', '')
        if not re.fullmatch(r'[a-zA-Z0-9._:-]{8,200}', idem):
            raise HTTPException(400, '중복 등록 방지 키가 필요합니다.')
        if payload.scheduled_at and payload.scheduled_at.tzinfo is None:
            raise HTTPException(400, '예약 시간에 시간대 정보가 필요합니다.')
        return idem

    def encrypted_destination(payload, user):
        normalized = validate_destination(payload.target, payload.server_url, payload.stream_key)
        links = normalize_watch_links(payload.target, channel_url=payload.channel_url, broadcast_url=payload.broadcast_url)
        encoded = json.dumps(normalized, sort_keys=True, separators=(',', ':')) if normalized else ''
        if len(encoded.encode()) > 4096:
            raise HTTPException(400, '송출 서버 주소와 키가 너무 깁니다.')
        ciphertext = secrets.encrypt(encoded, context={'tenant_id': user.tenant_id}) if encoded else None
        return {'target': payload.target, 'secret_ciphertext': ciphertext, **links}, normalized

    def broadcast_fingerprint(payload):
        # Keep pre-links idempotency fingerprints valid when optional links are
        # absent/empty, including retries after an application upgrade.
        def empty_links(destination):
            return {name for name in ('channel_url', 'broadcast_url') if not getattr(destination, name)}
        excluded = ({'destinations': {index: empty_links(destination)
                     for index, destination in enumerate(payload.destinations)}} if isinstance(payload, BroadcastBatch)
                    else empty_links(payload))
        return hmac.new(cfg.callback_key.encode(), payload.model_dump_json(exclude=excluded).encode(), hashlib.sha256).hexdigest()

    @app.post('/api/broadcasts', status_code=201)
    def create_broadcast(payload: Broadcast, request: Request, user=Depends(writer)):
        idem = broadcast_request(payload, request, user)
        destination, _ = encrypted_destination(payload, user)
        row = repo.create_job(user.tenant_id, media_id=payload.media_id, title=payload.title, target=payload.target,
               scheduled=payload.scheduled_at.timestamp() if payload.scheduled_at else None,
               idempotency_key=idem, secret_ciphertext=destination['secret_ciphertext'], max_attempts=payload.max_attempts,
               channel_url=destination['channel_url'], broadcast_url=destination['broadcast_url'],
               request_fingerprint=broadcast_fingerprint(payload))
        event('broadcast_created', request_id=request.state.request_id, job_id=row['id'], tenant_id=user.tenant_id, version=cfg.version, state=row['state'])
        notify_dispatch(required=True)
        return public_job(row)

    @app.post('/api/broadcast-batches', status_code=201)
    def create_broadcast_batch(payload: BroadcastBatch, request: Request, user=Depends(writer)):
        idem = broadcast_request(payload, request, user)
        if len({destination.target for destination in payload.destinations}) != len(payload.destinations):
            raise HTTPException(400, '같은 플랫폼은 한 번만 선택할 수 있습니다.')
        effective = set()
        for destination in payload.destinations:
            normalized = validate_destination(destination.target, destination.server_url, destination.stream_key)
            if normalized:
                endpoint = normalized['server_url'].rstrip('/') + '/' + normalized['stream_key']
                if endpoint in effective:
                    raise HTTPException(400, '동일한 송출 서버와 키를 중복 등록할 수 없습니다.')
                effective.add(endpoint)
        destinations = [encrypted_destination(destination, user)[0] for destination in payload.destinations]
        result = repo.create_jobs_batch(user.tenant_id, media_id=payload.media_id, title=payload.title,
            destinations=destinations, idempotency_key=idem, max_attempts=payload.max_attempts,
            scheduled=payload.scheduled_at.timestamp() if payload.scheduled_at else None,
            request_fingerprint=broadcast_fingerprint(payload))
        for row in result['jobs']:
            event('broadcast_created', request_id=request.state.request_id, job_id=row['id'], tenant_id=user.tenant_id,
                  version=cfg.version, state=row['state'])
        notify_dispatch(required=True)
        return {'jobs': [public_job(row) for row in result['jobs']], 'replayed': result['replayed']}

    @app.get('/api/broadcasts/{job_id}')
    def broadcast(job_id: str, user=Depends(principal)):
        return public_job(repo.get_job(user.tenant_id, job_id))

    @app.put('/api/broadcasts/{job_id}/watch-links')
    def update_watch_links(job_id: str, payload: WatchLinksIntent, user=Depends(writer)):
        return public_job(repo.update_watch_links(user.tenant_id, job_id,
            channel_url=payload.channel_url, broadcast_url=payload.broadcast_url))

    @app.post('/api/broadcasts/{job_id}/stop')
    def cancel(job_id: str, user=Depends(writer)):
        return public_job(repo.cancel(user.tenant_id, job_id))

    @app.get('/api/broadcasts/{job_id}/events')
    def events(job_id: str, user=Depends(principal)):
        return repo.events(user.tenant_id, job_id)

    @app.get('/api/broadcasts/{job_id}/output')
    def output(job_id: str, user=Depends(principal)):
        item = repo.get_job(user.tenant_id, job_id)
        if item['state'] != 'completed' or item['target'] != 'local' or not item.get('output_key'):
            raise HTTPException(409, '완료된 테스트 결과가 없습니다.')
        return objects.presign_download(user.tenant_id, item['output_key'], expires=300, filename=f'replay-{job_id}.flv')

    @app.post('/internal/claim', dependencies=[Depends(control)])
    def claim(payload: Claim):
        if payload.version != cfg.version:
            raise HTTPException(409, '실행 조정자와 API의 배포 버전이 일치하지 않습니다.')
        repo.heartbeat_worker(payload.worker_id, {'version': payload.version, 'role': 'dispatcher', 'draining': cfg.draining})
        repo.recover_expired()
        if cfg.draining:
            return {'job': None, 'draining': True}
        row = repo.claim(payload.worker_id, lease_seconds=cfg.lease_seconds)
        if not row:
            return {'job': None}
        try:
            token = jwt.encode({'aud': 'replay-worker', 'job_id': row['id'], 'tenant_id': row['tenant_id'],
                   'lease_token': row['lease_token'], 'exp': row['deadline'] + 60}, cfg.callback_key, algorithm='HS256')
            source = row['media']
            destination = None
            import_source = None
            if row.get('secret_ciphertext'):
                plaintext = secrets.decrypt(row['secret_ciphertext'], context={'tenant_id': row['tenant_id']})
                try:
                    decoded = json.loads(plaintext)
                except (ValueError, TypeError):
                    if row['target'] != 'youtube':
                        raise ValueError('Invalid stored stream destination') from None
                    decoded = {'target': 'youtube', 'server_url': '', 'stream_key': plaintext}
                if row['target'] == 'import':
                    if not isinstance(decoded, dict):
                        raise ValueError('Invalid stored media source')
                    import_source = normalize_source(decoded.get('provider'), decoded.get('url'))
                elif row['target'] == 'youtube' and not isinstance(decoded, dict) and not plaintext.startswith('{'):
                    decoded = {'target': 'youtube', 'server_url': '', 'stream_key': plaintext}
                if row['target'] != 'import':
                    if not isinstance(decoded, dict) or decoded.get('target') != row['target']:
                        raise ValueError('Stored stream destination does not match its job')
                    destination = pin_destination(validate_destination(row['target'], decoded.get('server_url', ''), decoded.get('stream_key', '')))
            if row['target'] == 'import' and import_source is None:
                raise ValueError('Stored media source is missing')
            event('worker_claimed', job_id=row['id'], tenant_id=row['tenant_id'], version=cfg.version)
            return {'job': {'id': row['id'], 'tenant_id': row['tenant_id'], 'lease_version': row['lease_version'],
                'target': row['target'], 'duration': source['duration'], 'progress': row['progress'], 'bytes': source['bytes'], 'sha256': source['sha256'],
                'input': None if row['target'] == 'import' else objects.presign_download(row['tenant_id'], source['object_key'], expires=900),
                'source': import_source,
                'stream_destination': destination,
                'stream_key': destination['stream_key'] if destination else '',
                'callback_base': cfg.public_url + '/internal/jobs/' + row['id'], 'callback_token': token,
                'deadline': min(row['deadline'], row['reserved_until'], time.time() + cfg.worker_max_seconds), 'lease_seconds': cfg.lease_seconds,
                'validation_timeout': cfg.validation_timeout, 'max_duration': cfg.max_duration,
                'output_budget_bytes': row['output_budget_bytes'],
                'version': cfg.version, 'mode': cfg.mode}}
        except Exception:
            repo.finish(row['id'], row['lease_token'], state='failed', error_code='WORKER_SETUP_FAILED')
            raise HTTPException(503, '작업 실행 준비에 실패했습니다.') from None

    @app.post('/internal/jobs/{job_id}/heartbeat')
    def heartbeat(job_id: str, payload: Heartbeat, claims=Depends(callback)):
        return repo.heartbeat(job_id, claims['lease_token'], lease_seconds=cfg.lease_seconds, progress=payload.progress)

    @app.post('/internal/jobs/{job_id}/output')
    def output_intent(job_id: str, payload: OutputIntent, claims=Depends(callback)):
        repo.heartbeat(job_id, claims['lease_token'], lease_seconds=cfg.lease_seconds)
        row = repo.get_job(claims['tenant_id'], job_id)
        if row['target'] not in ('local', 'import'):
            raise HTTPException(400, '파일 결과를 생성하는 작업이 아닙니다.')
        if payload.bytes > row['output_budget_bytes']:
            raise HTTPException(409, '결과 파일이 예약한 저장 용량을 초과했습니다.')
        importing = row['target'] == 'import'
        key = objects.key(claims['tenant_id'], (row['media_id'] if importing else job_id) + '-' + str(row['lease_version']),
                          kind='media' if importing else 'outputs', extension='mp4' if importing else 'flv')
        signed = objects.presign_upload(claims['tenant_id'], key, size=payload.bytes, sha256=payload.sha256,
                                        content_type='video/mp4' if importing else 'video/x-flv')
        # Reserve after signing so cleanup cannot precede the issued PUT expiry.
        # The signed capability is never returned unless this transaction succeeds.
        repo.reserve_output(job_id, claims['lease_token'], object_key=key, bytes=payload.bytes, sha256=payload.sha256)
        return signed

    @app.post('/internal/jobs/{job_id}/finish')
    def finish(job_id: str, payload: Finish, background_tasks: BackgroundTasks, claims=Depends(callback)):
        repo.heartbeat(job_id, claims['lease_token'], lease_seconds=cfg.lease_seconds)
        row = repo.get_job(claims['tenant_id'], job_id)
        output_key = None
        if payload.state == 'completed' and row['target'] in ('local', 'import'):
            if not payload.output_bytes or not payload.output_sha256:
                raise HTTPException(400, '검증된 출력 파일이 필요합니다.')
            importing = row['target'] == 'import'
            output_key = objects.key(claims['tenant_id'], (row['media_id'] if importing else job_id) + '-' + str(row['lease_version']),
                                     kind='media' if importing else 'outputs', extension='mp4' if importing else 'flv')
            objects.verify_upload(claims['tenant_id'], output_key, size=payload.output_bytes, sha256=payload.output_sha256,
                                  content_type='video/mp4' if importing else 'video/x-flv')
        if row['target'] in PROCESSING_TARGETS and payload.state == 'completed':
            info = payload.metadata or {}
            duration = info.get('duration')
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 < duration <= cfg.max_duration:
                raise HTTPException(400, '허용된 영상 길이가 아닙니다.')
        completed = repo.finish(job_id, claims['lease_token'], state=payload.state, error_code=payload.error_code,
                           progress=payload.progress, output_key=output_key, output_bytes=payload.output_bytes or 0,
                           validation_metadata=payload.metadata)
        event('worker_finished', job_id=job_id, tenant_id=claims['tenant_id'], version=cfg.version, state=completed['state'], code=completed.get('error_code'))
        background_tasks.add_task(notify_dispatch)
        return public_job(completed)

    @app.post('/internal/monitor', dependencies=[Depends(control)])
    def monitor():
        return operations.collect()

    @app.post('/internal/alerts', dependencies=[Depends(control)])
    def alerts():
        return {'alerts': operations.pending_alerts(limit=10)}

    @app.post('/internal/alerts/{alert_id}/ack', dependencies=[Depends(control)])
    def acknowledge_alert(alert_id: str):
        return {'acknowledged': operations.acknowledge(alert_id)}

    @app.post('/internal/runtimes', dependencies=[Depends(control)])
    def runtimes():
        repo.recover_expired()
        return {'runtimes': repo.runtimes(limit=200)}

    @app.post('/internal/next-wakeup', dependencies=[Depends(control)])
    def next_wakeup():
        return {'at': repo.next_wakeup()}

    class CleanedRuntime(BaseModel):
        job_id: str = Field(pattern=r'^[a-f0-9]{32}$')
        lease_version: int = Field(ge=1)

    @app.post('/internal/runtime-cleaned', dependencies=[Depends(control)])
    def cleaned_runtime(payload: CleanedRuntime):
        repo.mark_runtime_cleaned(payload.job_id, payload.lease_version)
        return {'status': 'cleaned'}

    @app.post('/internal/maintenance', dependencies=[Depends(control)])
    def maintenance():
        policy.purge_expired()
        if isinstance(auth, GoogleAuthenticator):
            auth.purge_expired()
        expired_imports = expire_device_imports(time.time() - cfg.retention_days * 86400)
        result = repo.cleanup(time.time() - cfg.retention_days * 86400)
        deleted, failed = 0, 0
        deletion_deadline = time.monotonic() + 8
        for item in repo.pending_deletions(limit=100):
            if time.monotonic() >= deletion_deadline:
                break
            try:
                objects.delete(item['tenant_id'], item['object_key'])
                repo.confirm_deletion(item['id'])
                deleted += 1
            except Exception:
                repo.deletion_failed(item['id'])
                failed += 1
        return {**result, 'expired_device_imports': expired_imports, 'objects_deleted': deleted, 'objects_failed': failed}

    if cfg.mode == 'development':
        @app.api_route('/api/storage/local', methods=['GET', 'PUT'])
        async def local_object(request: Request):
            query = request.query_params
            tenant_id, key = query.get('tenant', ''), query.get('key', '')
            constraints = ({'size': int(query.get('size', '0')), 'sha256': query.get('sha256', ''),
                            'content_type': query.get('content_type', '')} if request.method == 'PUT' else {})
            path = objects.verify_local_signature(request.method, tenant_id, key,
                    int(query.get('expires', '0')), query.get('signature', ''), **constraints)
            if request.method == 'GET':
                return FileResponse(path)
            await objects.put_stream(tenant_id, key, request.stream(), **constraints)
            return {'status': 'uploaded'}

    expire_device_imports = install_device_imports(app, cfg, repo, objects, secrets, policy, writer, public_media,
                           upload_complete, notify_dispatch, auth)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(cfg.public_url).hostname, 'testserver'] if cfg.mode == 'development' else [urlsplit(cfg.public_url).hostname])
    app.add_middleware(BoundaryMiddleware, max_body_bytes=max(cfg.max_upload_bytes, cfg.max_output_bytes) if cfg.mode == 'development' else 16384,
                       path_limits={'/api/uploads': 4096, '/api/media/imports': 8192, '/api/broadcasts': 8192, '/api/broadcast-batches': 49152,
                                    **{f'/api/stream-connections/{target}': 8192 for target in ('youtube', 'twitch', 'facebook', 'instagram', 'tiktok', 'naver', 'chzzk', 'kick', 'custom')},
                                    **{f'/api/stream-connections/{target}/use': 1024 for target in ('youtube', 'twitch', 'facebook', 'instagram', 'tiktok', 'naver', 'chzzk', 'kick', 'custom')},
                                    '/api/auth/google/challenge': 1024, '/api/auth/google/exchange': 16384,
                                    '/api/auth/google/refresh': 1024, '/api/logout': 1024},
                       body_timeout=120 if cfg.mode == 'development' else 15)
    app.add_middleware(CORSMiddleware, allow_origins=list(cfg.origins), allow_methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
                       allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key', 'X-Replay-Client', 'X-Replay-Tenant'],
                       expose_headers=['X-Request-ID', 'X-Replay-Version'])
    if workload:
        app.add_middleware(VercelOIDCMiddleware)
    return app
