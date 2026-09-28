"""Durable recurring broadcasts using saved destinations and existing job quotas."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from sqlalchemy import Boolean, Column, Float, MetaData, String, Table, Text, insert, select, update

from .auth import Principal
from .repository import NotFound, RepositoryError, QuotaExceeded, LeaseLost
from .stream_connections import StreamConnections
from .youtube_channel import ChannelError


metadata = MetaData()
schedules = Table('replay_automations', metadata,
    Column('id', String(32), primary_key=True), Column('tenant_id', String(200), nullable=False),
    Column('subject', String(4096), nullable=False), Column('subject_hash', String(64), nullable=False),
    Column('name', String(180), nullable=False), Column('config_json', Text, nullable=False),
    Column('enabled', Boolean, nullable=False), Column('deleted', Boolean, nullable=False),
    Column('next_run', Float, nullable=False), Column('active_run', String(32)),
    Column('last_video_id', String(64)), Column('lease_token', String(32)),
    Column('lease_until', Float, nullable=False, default=0), Column('created', Float, nullable=False))
runs = Table('replay_automation_runs', metadata,
    Column('id', String(32), primary_key=True), Column('schedule_id', String(32), nullable=False, index=True),
    Column('scheduled_for', Float, nullable=False), Column('state', String(24), nullable=False),
    Column('video_id', String(64)), Column('media_id', String(64)), Column('import_job_id', String(64)),
    Column('jobs_json', Text, nullable=False), Column('error_code', String(80)),
    Column('created', Float, nullable=False), Column('updated', Float, nullable=False))
TERMINAL = {'completed', 'skipped', 'failed', 'cancelled'}


def next_occurrence(config, after):
    zone = ZoneInfo(config['timezone'])
    hour, minute = map(int, config['time'].split(':'))
    local = datetime.fromtimestamp(after, timezone.utc).astimezone(zone)
    for day in range(15):
        date = local.date() + timedelta(days=day)
        if date.weekday() not in config['weekdays']:
            continue
        candidate = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone, fold=0)
        instant = candidate.timestamp()
        # Skip nonexistent DST wall times; ambiguous times run once at fold=0.
        back = datetime.fromtimestamp(instant, zone)
        if instant > after and (back.hour, back.minute, back.date()) == (hour, minute, date):
            return instant
    raise ValueError('Invalid recurrence')


def validate_config(payload):
    name = str(payload.get('name', '')).strip()
    source = payload.get('source')
    targets, days = payload.get('targets'), payload.get('weekdays')
    if (not name or len(name) > 180 or source not in ('media', 'youtube_latest')
            or not isinstance(targets, list) or not 1 <= len(targets) <= 10
            or any(not isinstance(t, str) for t in targets) or len(set(targets)) != len(targets)
            or not isinstance(days, list) or not days
            or any(type(d) is not int or not 0 <= d <= 6 for d in days)
            or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', str(payload.get('time', '')))):
        raise ValueError('반복 일정과 송출 플랫폼을 확인해 주세요.')
    try:
        ZoneInfo(payload.get('timezone', ''))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('올바른 시간대를 선택해 주세요.') from None
    media_id = payload.get('media_id')
    if source == 'media' and (not isinstance(media_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', media_id)):
        raise ValueError('반복 송출할 보관함 영상을 선택해 주세요.')
    return name, {'source': source, 'media_id': media_id if source == 'media' else None,
                  'targets': targets, 'time': payload['time'], 'timezone': payload['timezone'],
                  'weekdays': sorted(set(days))}


class Automations:
    def __init__(self, repo, keys, connections, channel, objects, cfg, *, clock=time.time):
        self.repo, self.keys, self.connections, self.channel = repo, keys, connections, channel
        self.objects, self.cfg, self.clock = objects, cfg, clock

    def migrate(self):
        metadata.create_all(self.repo.engine)
        from .youtube_channel import metadata as youtube_metadata
        youtube_metadata.create_all(self.repo.engine)

    @staticmethod
    def scope(user):
        owner = StreamConnections._owner(user)
        return ((schedules.c.tenant_id == owner['tenant_id']) &
                (schedules.c.subject_hash == owner['subject_hash']) & schedules.c.deleted.is_(False))

    def list(self, user):
        with self.repo.engine.connect() as connection:
            rows = connection.execute(select(schedules).where(self.scope(user)).order_by(schedules.c.created.desc())).mappings().all()
            result = []
            for row in rows:
                history = connection.execute(select(runs).where(runs.c.schedule_id == row['id'])
                    .order_by(runs.c.created.desc()).limit(10)).mappings().all()
                result.append({'id': row['id'], 'name': row['name'], **json.loads(row['config_json']),
                    'enabled': row['enabled'], 'next_run': row['next_run'], 'active': bool(row['active_run']),
                    'history': [{k: r[k] for k in ('id', 'state', 'scheduled_for', 'video_id', 'media_id', 'error_code', 'updated')} for r in history]})
        return result

    def destinations(self, user, targets):
        if len(targets) > min(self.repo.tenant_concurrency, self.repo.global_concurrency):
            raise ValueError('동시 송출할 수 있는 플랫폼 수를 초과했습니다.')
        destinations = [self.connections.use(user, target) for target in targets]
        endpoints = [d['server_url'].rstrip('/') + '/' + d['stream_key'] for d in destinations]
        if len(set(endpoints)) != len(endpoints):
            raise ValueError('같은 송출 연결을 중복 선택할 수 없습니다.')
        return destinations

    def create(self, user, payload):
        name, config = validate_config(payload)
        self.destinations(user, config['targets'])
        if config['source'] == 'media':
            if self.repo.get_media(user.tenant_id, config['media_id'])['status'] != 'ready':
                raise ValueError('준비된 보관함 영상을 선택해 주세요.')
        elif not self.channel.status(user)['connected']:
            raise ChannelError('YOUTUBE_RECONNECT_REQUIRED')
        owner = StreamConnections._owner(user)
        identifier, now = uuid.uuid4().hex, self.clock()
        with self.repo._transaction() as connection:
            self.repo._gate(connection)
            self.repo._check_admission(connection, user.tenant_id)
            existing = connection.execute(select(schedules.c.id).where(self.scope(user))).all()
            if len(existing) >= 20:
                raise ValueError('자동 송출 일정은 최대 20개까지 만들 수 있습니다.')
            connection.execute(insert(schedules).values(**owner, subject=user.subject, id=identifier, name=name,
                config_json=json.dumps(config), enabled=True, deleted=False, next_run=next_occurrence(config, now),
                lease_until=0, created=now))
        return next(row for row in self.list(user) if row['id'] == identifier)

    def change(self, user, identifier, *, enabled=None, remove=False):
        with self.repo._transaction() as connection:
            self.repo._gate(connection)
            row = connection.execute(select(schedules).where(self.scope(user), schedules.c.id == identifier)
                                     .with_for_update()).mappings().first()
            if not row:
                raise NotFound('자동 송출 일정을 찾을 수 없습니다.')
            if remove and row['active_run']:
                raise ValueError('진행 중인 회차가 끝난 뒤 삭제해 주세요. 먼저 일정을 일시중지할 수 있습니다.')
            values = {'deleted': True, 'enabled': False} if remove else {'enabled': enabled}
            if enabled is True and not row['enabled']:
                values['next_run'] = next_occurrence(json.loads(row['config_json']), self.clock())
            connection.execute(update(schedules).where(schedules.c.id == identifier).values(**values))

    def claim(self):
        with self.repo._transaction() as connection:
            self.repo._gate(connection)
            now = self.clock()
            row = connection.execute(select(schedules).where(schedules.c.deleted.is_(False),
                schedules.c.lease_until <= now,
                ((schedules.c.enabled.is_(True) & (schedules.c.next_run <= now)) | schedules.c.active_run.is_not(None)))
                .order_by(schedules.c.lease_until, schedules.c.next_run).limit(1).with_for_update(skip_locked=True)).mappings().first()
            if not row:
                return None
            row = dict(row)
            row['lease_token'] = uuid.uuid4().hex
            if not row['active_run']:
                row['active_run'] = uuid.uuid4().hex
                connection.execute(insert(runs).values(id=row['active_run'], schedule_id=row['id'],
                    scheduled_for=row['next_run'], state='checking', jobs_json='[]', created=now, updated=now))
            connection.execute(update(schedules).where(schedules.c.id == row['id']).values(
                active_run=row['active_run'], lease_token=row['lease_token'], lease_until=now+120))
            return row

    def record(self, rule, **values):
        with self.repo._transaction() as connection:
            self.repo._gate(connection)
            current = connection.execute(select(schedules).where(schedules.c.id == rule['id'])
                                         .with_for_update()).mappings().one()
            if current['lease_token'] != rule['lease_token'] or current['lease_until'] <= self.clock():
                raise LeaseLost('AUTOMATION_LEASE_LOST')
            connection.execute(update(runs).where(runs.c.id == rule['active_run']).values(**values, updated=self.clock()))
            if values.get('state') == 'broadcasting':
                video_id = connection.execute(select(runs.c.video_id).where(runs.c.id == rule['active_run'])).scalar_one()
                if video_id:
                    connection.execute(update(schedules).where(schedules.c.id == rule['id']).values(last_video_id=video_id))
            if values.get('state') in TERMINAL:
                connection.execute(update(schedules).where(schedules.c.id == rule['id']).values(
                    active_run=None, next_run=next_occurrence(json.loads(rule['config_json']), self.clock())))

    def step(self, rule):
        user = Principal(rule['subject'], rule['tenant_id'], ('operator',))
        config = json.loads(rule['config_json'])
        with self.repo._transaction() as connection:
            self.repo._check_admission(connection, user.tenant_id)
            run = dict(connection.execute(select(runs).where(runs.c.id == rule['active_run'])).mappings().one())
        if run['state'] == 'checking':
            if config['source'] == 'youtube_latest':
                if self.cfg.mode == 'production' and not self.cfg.server_imports:
                    raise ChannelError('SERVER_IMPORTS_DISABLED')
                video = self.channel.latest(user)
                with self.repo.engine.connect() as connection:
                    previously_queued = bool(video and connection.execute(select(runs.c.id).where(
                        runs.c.schedule_id == rule['id'], runs.c.video_id == video['id'], runs.c.jobs_json != '[]')
                        .limit(1)).first())
                if not video or video['id'] == rule['last_video_id'] or previously_queued:
                    self.record(rule, state='skipped', error_code='NO_NEW_VIDEO')
                    return
                # Persist the selected video before admission so retries keep the same source.
                self.record(rule, state='selected', video_id=video['id'])
            else:
                self.record(rule, state='ready', media_id=config['media_id'])
            return
        if run['state'] == 'selected':
            source = {'provider': 'youtube', 'url': 'https://www.youtube.com/watch?v=' + run['video_id'],
                      'use_original_title': True}
            identity = 'automation-import-' + run['id']
            result = self.repo.create_import(user.tenant_id, name='가져온 녹화영상.mp4',
                object_key=self.objects.key(user.tenant_id, run['id']), id=run['id'],
                secret_ciphertext=self.keys.encrypt(json.dumps(source), {'tenant_id': user.tenant_id}),
                idempotency_key=identity, request_fingerprint=hashlib.sha256(identity.encode()).hexdigest(),
                max_bytes=min(self.cfg.max_upload_bytes or self.cfg.max_storage_bytes, self.objects.max_put_bytes))
            self.record(rule, state='importing', media_id=result['media']['id'], import_job_id=result['job']['id'])
            return
        if run['state'] == 'importing':
            media = self.repo.get_media(user.tenant_id, run['media_id'])
            if media['status'] == 'ready':
                self.record(rule, state='ready')
            elif media['status'] in ('failed', 'deleting', 'deleted'):
                self.record(rule, state='failed', error_code=media.get('error_code') or 'IMPORT_FAILED')
            return
        if run['state'] == 'ready':
            destinations = []
            for value in self.destinations(user, config['targets']):
                payload = {key: value[key] for key in ('target', 'server_url', 'stream_key')}
                destinations.append({'target': value['target'],
                    'secret_ciphertext': self.keys.encrypt(json.dumps(payload), {'tenant_id': user.tenant_id}),
                    'channel_url': value.get('channel_url', ''), 'broadcast_url': ''})
            media = self.repo.get_media(user.tenant_id, run['media_id'])
            identity = 'automation-broadcast-' + run['id']
            result = self.repo.create_jobs_batch(user.tenant_id, media_id=run['media_id'],
                title=media['name'][:120], destinations=destinations, idempotency_key=identity,
                request_fingerprint=hashlib.sha256(identity.encode()).hexdigest(), max_attempts=1)
            self.record(rule, state='broadcasting', jobs_json=json.dumps([j['id'] for j in result['jobs']]))
            return
        if run['state'] == 'broadcasting':
            jobs = [self.repo.get_job(user.tenant_id, job) for job in json.loads(run['jobs_json'])]
            if all(job['state'] in ('completed', 'failed', 'stopped') for job in jobs):
                success = all(job['state'] == 'completed' for job in jobs)
                self.record(rule, state='completed' if success else 'failed',
                            error_code=None if success else 'BROADCAST_FAILED')

    def tick(self, limit=10):
        if self.cfg.draining:
            return {'processed': 0}
        processed = 0
        for _ in range(limit):
            rule = self.claim()
            if not rule:
                break
            try:
                self.step(rule)
            except LeaseLost:
                pass  # Another coordinator owns recovery; never overwrite its state.
            except (ChannelError, RepositoryError, HTTPException, ValueError) as error:
                code = error.code if isinstance(error, ChannelError) else (
                    'CONNECTION_REQUIRED' if isinstance(error, HTTPException) and error.status_code == 404
                    else 'STORAGE_OR_JOB_QUOTA' if isinstance(error, QuotaExceeded) else 'AUTOMATION_FAILED')
                try:
                    self.record(rule, state='failed', error_code=code)
                except LeaseLost:
                    pass
            finally:
                with self.repo._transaction() as connection:
                    connection.execute(update(schedules).where(schedules.c.id == rule['id'],
                        schedules.c.lease_token == rule['lease_token']).values(lease_until=self.clock()+1, lease_token=None))
            processed += 1
        return {'processed': processed}


def install_automations(app, cfg, repo, keys, connections, objects, writer, control, policy, notify):
    """Routes are inert until explicitly enabled and migrated by the operator."""
    import os
    from fastapi import Depends, Query
    from fastapi.responses import HTMLResponse, JSONResponse
    from pydantic import BaseModel, StrictBool
    from .youtube_channel import YouTubeChannel

    enabled = os.getenv('REPLAY_AUTOMATIONS_ENABLED') == '1'
    channel = YouTubeChannel(repo, keys, cfg.public_url.rstrip('/') + '/api/youtube-channel/callback')
    service = Automations(repo, keys, connections, channel, objects, cfg)
    app.state.automations = service

    def require_enabled():
        if not enabled:
            raise HTTPException(404, '자동 송출이 활성화되지 않았습니다.')

    messages = {
        'YOUTUBE_NOT_CONFIGURED': 'YouTube 채널 연결 설정이 아직 준비되지 않았습니다.',
        'YOUTUBE_RECONNECT_REQUIRED': 'YouTube 채널을 다시 연결해 주세요.',
        'YOUTUBE_CHANNEL_REQUIRED': '영상을 보유한 YouTube 채널을 선택해 주세요.',
        'YOUTUBE_INVALID_STATE': '연결 요청이 만료되었습니다. 다시 연결해 주세요.',
        'YOUTUBE_ACCESS_DENIED': 'YouTube 채널 조회 권한을 확인해 주세요.',
        'YOUTUBE_UNAVAILABLE': 'YouTube에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.',
    }

    @app.exception_handler(ChannelError)
    async def channel_error(request, exc):
        return JSONResponse({'detail': messages.get(exc.code, messages['YOUTUBE_UNAVAILABLE']),
                             'code': exc.code}, status_code=400)

    @app.get('/api/automations')
    def listing(user=Depends(writer)):
        if not enabled:
            return {'enabled': False, 'items': []}
        return {'enabled': True, 'items': service.list(user), 'youtube': channel.status(user),
                'connections': connections.list(user)}

    @app.post('/api/automations', status_code=201, dependencies=[Depends(require_enabled)])
    def create(payload: dict, user=Depends(writer)):
        policy.check(user, action='broadcast')
        try:
            return service.create(user, payload)
        except (ChannelError, RepositoryError, HTTPException):
            raise
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    class Status(BaseModel):
        model_config = {'extra': 'forbid'}
        enabled: StrictBool

    @app.put('/api/automations/{identifier}', dependencies=[Depends(require_enabled)])
    def change(identifier: str, payload: Status, user=Depends(writer)):
        service.change(user, identifier, enabled=payload.enabled)
        return {'ok': True}

    @app.delete('/api/automations/{identifier}', dependencies=[Depends(require_enabled)])
    def remove(identifier: str, user=Depends(writer)):
        try:
            service.change(user, identifier, remove=True)
        except NotFound:
            raise
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return {'ok': True}

    @app.post('/api/youtube-channel/connect', dependencies=[Depends(require_enabled)])
    def connect(user=Depends(writer)):
        return channel.begin(user)

    @app.delete('/api/youtube-channel', dependencies=[Depends(require_enabled)])
    def disconnect(user=Depends(writer)):
        channel.disconnect(user)
        return {'ok': True}

    @app.get('/api/youtube-channel/callback', dependencies=[Depends(require_enabled)])
    def callback(state: str = Query('', max_length=100), code: str = Query('', max_length=4096),
                 error: str = Query('', max_length=200)):
        result = '연결했습니다. 이 창을 닫아 주세요.'
        ok = True
        try:
            channel.finish(state, '' if error else code)
        except ChannelError as exc:
            ok = False
            result = messages.get(exc.code, messages['YOUTUBE_UNAVAILABLE'])
        # Fixed trusted destination. No token, state or provider error is reflected.
        target = json.dumps(cfg.origins[0]).replace('<', '\\u003c')
        message = json.dumps({'type': 'replay-youtube-connected', 'ok': ok, 'message': result}).replace('<', '\\u003c')
        return HTMLResponse('<!doctype html><html lang="ko"><meta charset="utf-8">'
            '<meta name="referrer" content="no-referrer"><title>YouTube 채널 연결</title><p>' + result + '</p>'
            '<script>if(window.opener){window.opener.postMessage(' + message + ','
            + target + ');window.close();}</script></html>', headers={'Cache-Control': 'no-store',
                'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; frame-ancestors 'none'"})

    @app.post('/internal/automations/tick', dependencies=[Depends(control), Depends(require_enabled)])
    def tick():
        result = service.tick()
        if result['processed']:
            notify()
        return result

    return channel.http.close
