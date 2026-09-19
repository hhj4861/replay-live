"""User-created download tickets: a PC gets one task, never dispatcher access."""
import hashlib
import json
import time
import uuid

import jwt
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import Column, Float, Index, Integer, MetaData, String, Table, Text, delete, insert, select, update

from .auth import Principal
from .google_auth import GoogleAuthenticator
from .media_sources import normalize_source
from .repository import Conflict, NotFound

metadata = MetaData()
tasks = Table('replay_device_imports', metadata,
    Column('id', String(32), primary_key=True),
    Column('tenant_id', String(200), nullable=False),
    Column('subject', String(200), nullable=False),
    Column('name', String(180), nullable=False),
    Column('source_ciphertext', Text, nullable=False),
    Column('token_hash', String(64), nullable=False),
    Column('state', String(24), nullable=False),
    Column('max_bytes', Integer, nullable=False),
    Column('max_duration', Integer, nullable=False),
    Column('bytes', Integer),
    Column('sha256', String(64)),
    Column('expires_at', Float, nullable=False),
    Column('error_code', String(80)))
Index('ix_replay_device_imports_owner', tasks.c.tenant_id, tasks.c.subject, tasks.c.state)
Index('ix_replay_device_imports_expiry', tasks.c.expires_at)


class DeviceImportIntent(BaseModel):
    model_config = {'extra': 'forbid'}
    provider: str = Field(min_length=1, max_length=30)
    url: str = Field(min_length=1, max_length=4096, repr=False)
    name: str = Field(default='가져온 영상.mp4', min_length=1, max_length=180)


class DeviceUpload(BaseModel):
    model_config = {'extra': 'forbid'}
    bytes: StrictInt = Field(gt=0, le=50 * 1024**2)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class DeviceFailure(BaseModel):
    model_config = {'extra': 'forbid'}
    code: str = Field(pattern=r'^[A-Z][A-Z0-9_]{0,79}$')


def install_device_imports(app, cfg, repo, objects, keys, policy, writer, public_media,
                           upload_complete, notify_dispatch, auth):
    # Production uses 010_device_imports.sql; never create tables at runtime there.
    if cfg.mode == 'development':
        metadata.create_all(repo.engine)

    def read_task(connection, id):
        row = connection.execute(select(tasks).where(tasks.c.id == id)).mappings().first()
        if row is None:
            raise HTTPException(404, '이 컴퓨터에 전달된 가져오기 작업이 없습니다.')
        return dict(row)

    def public_task(row):
        return {key: row[key] for key in ('id', 'state', 'expires_at', 'error_code')}

    def capability(id: str, request: Request):
        token = request.headers.get('authorization', '').removeprefix('Bearer ')
        try:
            claims = jwt.decode(token, cfg.callback_key, algorithms=['HS256'], audience='replay-device-import',
                options={'require': ['exp', 'sub', 'tenant_id', 'task_id', 'token_id', 'user_expires']})
            if claims['task_id'] != id:
                raise ValueError()
            user = Principal(claims['sub'], claims['tenant_id'], ('operator',),
                             claims['token_id'], claims['user_expires'])
            if isinstance(auth, GoogleAuthenticator):
                # Recheck the original live session, including family logout,
                # administrative revocation and role changes. No login token
                # is shared with the daemon or persisted in a download ticket.
                with auth._transaction() as conn:
                    session, roles = auth._session(conn, user.token_id, auth._now(conn))
                    if (user.subject != 'google:' + session['subject']
                            or user.tenant_id != session['tenant_id']
                            or not set(roles).intersection(('operator', 'admin', 'site_admin'))):
                        raise HTTPException(403, '이 가져오기 작업의 사용자 권한이 변경됐습니다.')
            policy.check(user)
            with repo._transaction() as conn:
                repo._gate(conn)
                repo._check_admission(conn, user.tenant_id)
                row = read_task(conn, id)
                if (row['tenant_id'] != user.tenant_id or row['subject'] != user.subject
                        or row['token_hash'] != hashlib.sha256(token.encode()).hexdigest()
                        or row['expires_at'] <= time.time()):
                    raise ValueError()
                if row['state'] in ('cancelled', 'failed', 'expired'):
                    raise HTTPException(409, '종료된 가져오기 작업입니다.')
            return row, user
        except (jwt.PyJWTError, ValueError, KeyError, TypeError):
            raise HTTPException(401, '이 가져오기 작업의 권한이 만료되었거나 일치하지 않습니다.') from None

    def clean_media(id, tenant_id):
        try:
            repo.delete_media(tenant_id, id)
            notify_dispatch()
        except (NotFound, Conflict):
            # An already admitted validation/broadcast keeps its media.
            pass

    def expire(before=None):
        now = time.time()
        with repo._transaction() as conn:
            repo._gate(conn)
            rows = conn.execute(select(tasks).where(tasks.c.expires_at <= now,
                tasks.c.state.in_(['queued', 'downloading', 'uploading', 'completing']))
                .order_by(tasks.c.expires_at).limit(100)).mappings().all()
            if rows:
                conn.execute(update(tasks).where(tasks.c.id.in_([row['id'] for row in rows]))
                    .values(state='expired', source_ciphertext=''))
            if before is not None:
                conn.execute(delete(tasks).where(tasks.c.expires_at < before,
                    tasks.c.state.in_(['completed', 'cancelled', 'failed', 'expired'])))
        for row in rows:
            # Completion may have admitted validation before losing its reply.
            if row['state'] != 'completing':
                clean_media(row['id'], row['tenant_id'])
        return len(rows)

    @app.post('/api/device-imports', status_code=201)
    def create(payload: DeviceImportIntent, user=Depends(writer)):
        policy.check(user, action='upload')
        if cfg.draining:
            raise HTTPException(503, '현재 점검 중입니다.')
        source = normalize_source(payload.provider, payload.url)
        now = time.time()
        expiry = min(now + 600, user.expires_at or now)
        if expiry < now + 60:
            raise HTTPException(401, '로그인을 갱신한 뒤 다시 가져오세요.')
        limit = min(50 * 1024**2, cfg.max_upload_bytes, objects.max_put_bytes,
                    repo.usage(user.tenant_id)['storage_available_bytes'])
        if limit < 1:
            raise HTTPException(409, '보관함의 저장 공간이 부족합니다.')
        id = uuid.uuid4().hex
        token = jwt.encode({'aud': 'replay-device-import', 'sub': user.subject, 'tenant_id': user.tenant_id,
            'task_id': id, 'token_id': user.token_id, 'user_expires': user.expires_at,
            'iat': now, 'exp': expiry}, cfg.callback_key, algorithm='HS256')
        name = payload.name if payload.name.lower().endswith('.mp4') else payload.name[:176] + '.mp4'
        row = dict(id=id, tenant_id=user.tenant_id, subject=user.subject, name=name,
            source_ciphertext=keys.encrypt(json.dumps(source), context={'tenant_id': user.tenant_id}),
            token_hash=hashlib.sha256(token.encode()).hexdigest(), state='queued', max_bytes=int(limit),
            max_duration=min(120, cfg.max_duration), expires_at=expiry, error_code=None)
        with repo._transaction() as conn:
            repo._gate(conn)
            repo._check_admission(conn, user.tenant_id)
            active = (tasks.c.tenant_id == user.tenant_id) & (tasks.c.subject == user.subject) & tasks.c.state.in_(['queued', 'downloading', 'uploading', 'completing'])
            expired = conn.execute(select(tasks.c.id, tasks.c.state).where(active, tasks.c.expires_at <= now)).all()
            conn.execute(update(tasks).where(active, tasks.c.expires_at <= now).values(state='expired', source_ciphertext=''))
            if conn.execute(select(tasks.c.id).where(active, tasks.c.expires_at > now)).first():
                raise HTTPException(409, '진행 중인 내 컴퓨터 가져오기를 먼저 완료하거나 취소하세요.')
            conn.execute(insert(tasks).values(**row))
        for old in expired:
            if old.state != 'completing':
                clean_media(old.id, user.tenant_id)
        return {**public_task(row), 'token': token}

    @app.get('/api/device-imports/{id}')
    def status(id: str, user=Depends(writer)):
        with repo._read() as conn:
            row = read_task(conn, id)
        if row['tenant_id'] != user.tenant_id or row['subject'] != user.subject:
            raise HTTPException(404, '가져오기 작업을 찾을 수 없습니다.')
        result = public_task(row)
        if row['state'] == 'completed':
            result['media'] = public_media(repo.get_media(user.tenant_id, id))
        return result

    @app.delete('/api/device-imports/{id}', status_code=204)
    def cancel(id: str, user=Depends(writer)):
        with repo._transaction() as conn:
            repo._gate(conn)
            row = read_task(conn, id)
            if row['tenant_id'] != user.tenant_id or row['subject'] != user.subject:
                raise HTTPException(404, '가져오기 작업을 찾을 수 없습니다.')
            if row['state'] == 'completed':
                return
            if row['state'] == 'completing':
                raise HTTPException(409, '업로드 완료를 확인 중입니다. 잠시 후 보관함을 확인하세요.')
            conn.execute(update(tasks).where(tasks.c.id == id).values(state='cancelled', source_ciphertext=''))
        clean_media(id, user.tenant_id)

    @app.get('/api/device-imports/{id}/task')
    def task(id: str, grant=Depends(capability)):
        row, _ = grant
        if row['state'] == 'completed':
            return {'id': id, 'state': 'completed', 'media_id': id}
        source = json.loads(keys.decrypt(row['source_ciphertext'], context={'tenant_id': row['tenant_id']}))
        with repo._transaction() as conn:
            conn.execute(update(tasks).where(tasks.c.id == id, tasks.c.state == 'queued').values(state='downloading'))
        return {'id': id, 'state': row['state'], 'source': source, 'max_bytes': row['max_bytes'],
                'max_duration': row['max_duration'], 'expires_at': row['expires_at']}

    @app.post('/api/device-imports/{id}/upload')
    def upload(id: str, payload: DeviceUpload, grant=Depends(capability)):
        row, user = grant
        if payload.bytes > row['max_bytes']:
            raise HTTPException(413, '허용된 가져오기 크기를 초과했습니다.')
        with repo._transaction() as conn:
            repo._gate(conn)
            current = read_task(conn, id)
            if current['state'] not in ('queued', 'downloading', 'uploading'):
                raise HTTPException(409, '종료된 가져오기 작업입니다.')
            if current['sha256'] and (current['sha256'] != payload.sha256 or current['bytes'] != payload.bytes):
                raise HTTPException(409, '한 작업의 영상 파일은 변경할 수 없습니다.')
            conn.execute(update(tasks).where(tasks.c.id == id).values(state='uploading', bytes=payload.bytes, sha256=payload.sha256))
        key = objects.key(user.tenant_id, id)
        # No storage or API master credential is returned to the PC.
        signed = objects.presign_upload(user.tenant_id, key, size=payload.bytes, sha256=payload.sha256,
                                        expires=max(1, min(300, int(row['expires_at'] - time.time()))))
        try:
            item = repo.get_media(user.tenant_id, id)
        except NotFound:
            try:
                item = repo.add_media(user.tenant_id, id=id, name=row['name'], object_key=key,
                    bytes=payload.bytes, sha256=payload.sha256, status='uploading')
            except Conflict:
                item = repo.get_media(user.tenant_id, id)
        if item['status'] != 'uploading' or item['sha256'] != payload.sha256 or item['bytes'] != payload.bytes:
            raise HTTPException(409, '영상 업로드 상태가 변경됐습니다.')
        # A cancellation racing with signing/admission must not orphan quota.
        with repo._read() as conn:
            current = read_task(conn, id)
        if current['state'] != 'uploading':
            clean_media(id, user.tenant_id)
            raise HTTPException(409, '취소된 가져오기 작업입니다.')
        return signed

    @app.post('/api/device-imports/{id}/complete')
    def complete(id: str, grant=Depends(capability)):
        row, user = grant
        if row['state'] not in ('uploading', 'completing', 'completed'):
            raise HTTPException(409, '영상 업로드가 끝나지 않았습니다.')
        with repo._transaction() as conn:
            repo._gate(conn)
            current = read_task(conn, id)
            if current['state'] not in ('uploading', 'completing', 'completed'):
                raise HTTPException(409, '가져오기 작업이 취소됐습니다.')
            if current['state'] != 'completed':
                conn.execute(update(tasks).where(tasks.c.id == id).values(state='completing'))
        try:
            result = upload_complete(id, user)
        except Exception:
            # Retry verification, but never make an admitted validation job cancellable
            # as an unfinished upload after a lost wakeup/completion response.
            if repo.get_media(user.tenant_id, id)['status'] == 'uploading':
                with repo._transaction() as conn:
                    conn.execute(update(tasks).where(tasks.c.id == id, tasks.c.state == 'completing').values(state='uploading'))
            raise
        with repo._transaction() as conn:
            conn.execute(update(tasks).where(tasks.c.id == id).values(state='completed', source_ciphertext=''))
        return {'media': result}

    @app.post('/api/device-imports/{id}/failure')
    def failed(id: str, payload: DeviceFailure, grant=Depends(capability)):
        row, user = grant
        with repo._transaction() as conn:
            repo._gate(conn)
            current = read_task(conn, id)
            if current['state'] in ('completed', 'completing', 'cancelled', 'expired'):
                return public_task(current)
            conn.execute(update(tasks).where(tasks.c.id == id).values(state='failed', error_code=payload.code, source_ciphertext=''))
        clean_media(id, user.tenant_id)
        return {'id': id, 'state': 'failed', 'error_code': payload.code}

    return expire
