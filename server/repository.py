"""Tenant-scoped durable job repository.

PostgreSQL is the production backend. SQLite is supported for development and
deterministic tests, using BEGIN IMMEDIATE instead of PostgreSQL row locks.
All timestamps are UTC Unix seconds. Only ``claim`` returns worker credentials;
public methods deliberately omit encrypted secrets and lease ownership.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import re
import threading
import time
import uuid

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Float, ForeignKeyConstraint,
                        Index, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, and_, case, create_engine, delete, event,
                        func, insert, inspect, or_, select, update)
from sqlalchemy.pool import StaticPool

from server.output_policy import DEFAULT_MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES, estimate_output_bytes
from server.watch_links import normalize_watch_links


TERMINAL = frozenset({'completed', 'failed', 'stopped'})
QUEUED = frozenset({'scheduled', 'retry_wait'})
ACTIVE = frozenset({'starting', 'streaming', 'stopping'})
NONTERMINAL = QUEUED | ACTIVE
BROADCAST_TARGETS = frozenset({'local', 'youtube', 'twitch', 'facebook', 'instagram',
    'tiktok', 'naver', 'chzzk', 'kick', 'custom'})
PROCESSING_TARGETS = frozenset({'validate', 'import'})


class RepositoryError(ValueError):
    """Safe application error; contains no database details or credentials."""


class NotFound(RepositoryError):
    pass


class Conflict(RepositoryError):
    pass


class QuotaExceeded(Conflict):
    pass


class OutputCapacityExceeded(QuotaExceeded):
    def __init__(self, code='OUTPUT_STORAGE_QUOTA_EXCEEDED'):
        messages = {
            'OUTPUT_LIMIT_EXCEEDED': '예상 출력 크기가 파일당 출력 한도를 초과합니다.',
            'OUTPUT_STORAGE_QUOTA_EXCEEDED': '예상 출력 파일을 예약할 저장 공간이 부족합니다.',
        }
        if code not in messages:
            raise ValueError('Invalid output capacity error code')
        self.code = code
        super().__init__(messages[code])


class LeaseLost(Conflict):
    pass


metadata = MetaData()
coordination = Table('replay_coordination', metadata,
                     Column('id', Integer, primary_key=True))
media = Table('replay_media', metadata,
    Column('id', String(64), primary_key=True),
    Column('tenant_id', String(200), nullable=False),
    Column('name', String(180), nullable=False),
    Column('object_key', String(1024), nullable=False, unique=True),
    Column('bytes', BigInteger, nullable=False),
    Column('duration', Float, nullable=False),
    Column('width', Integer, nullable=False),
    Column('height', Integer, nullable=False),
    Column('fps', Float, nullable=False),
    Column('sha256', String(64), nullable=False),
    Column('etag', String(200), nullable=False),
    Column('status', String(24), nullable=False),
    Column('error_code', String(80)),
    Column('created', Float, nullable=False),
    Column('updated', Float, nullable=False),
    UniqueConstraint('tenant_id', 'id', name='uq_replay_media_tenant_id'))
jobs = Table('replay_jobs', metadata,
    Column('id', String(64), primary_key=True),
    Column('tenant_id', String(200), nullable=False),
    Column('media_id', String(64), nullable=False),
    Column('title', String(120), nullable=False),
    Column('target', String(24), nullable=False),
    Column('channel_url', String(2048), nullable=False, default='', server_default=''),
    Column('broadcast_url', String(2048), nullable=False, default='', server_default=''),
    Column('secret_ciphertext', Text),
    Column('idempotency_key', String(200), nullable=False),
    Column('payload_hash', String(64), nullable=False),
    Column('scheduled', Float, nullable=False),
    Column('reserved_until', Float, nullable=False),
    Column('deadline', Float, nullable=False),
    Column('state', String(24), nullable=False),
    Column('progress', Float, nullable=False, default=0),
    Column('attempt', Integer, nullable=False, default=0),
    Column('max_attempts', Integer, nullable=False),
    Column('next_run', Float, nullable=False),
    Column('lease_token', String(64)),
    Column('lease_version', Integer, nullable=False, default=0),
    Column('lease_expires', Float),
    Column('worker_id', String(200)),
    Column('cancel_requested', Boolean, nullable=False, default=False),
    Column('output_key', String(1024)),
    Column('output_bytes', BigInteger, nullable=False, default=0),
    Column('output_budget_bytes', BigInteger, nullable=False, default=0, server_default='0'),
    Column('output_reserved_bytes', BigInteger, nullable=False, default=0, server_default='0'),
    Column('created', Float, nullable=False),
    Column('updated', Float, nullable=False),
    Column('error_code', String(80)),
    UniqueConstraint('tenant_id', 'idempotency_key', name='uq_replay_jobs_idempotency'),
    UniqueConstraint('tenant_id', 'id', name='uq_replay_jobs_tenant_id'),
    CheckConstraint('output_budget_bytes >= 0', name='ck_replay_jobs_output_budget'),
    CheckConstraint('output_reserved_bytes >= 0 AND output_reserved_bytes <= output_budget_bytes',
                    name='ck_replay_jobs_output_reserved'),
    ForeignKeyConstraint(['tenant_id', 'media_id'], ['replay_media.tenant_id', 'replay_media.id']))
job_events = Table('replay_events', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('tenant_id', String(200), nullable=False),
    Column('job_id', String(64), nullable=False),
    Column('at', Float, nullable=False),
    Column('code', String(80), nullable=False),
    ForeignKeyConstraint(['tenant_id', 'job_id'], ['replay_jobs.tenant_id', 'replay_jobs.id'], ondelete='CASCADE'))
daily_usage = Table('replay_daily_usage', metadata,
    Column('tenant_id', String(200), primary_key=True),
    Column('day', Integer, primary_key=True),
    Column('jobs', Integer, nullable=False),
    Column('reserved_seconds', Float, nullable=False))
object_deletions = Table('replay_object_deletions', metadata,
    Column('id', String(64), primary_key=True),
    Column('tenant_id', String(200), nullable=False),
    Column('object_key', String(1024), nullable=False, unique=True),
    Column('created', Float, nullable=False),
    Column('not_before', Float, nullable=False),
    Column('attempt', Integer, nullable=False, default=0),
    Column('error_code', String(80)))
idempotency_records = Table('replay_idempotency', metadata,
    Column('tenant_id', String(200), primary_key=True),
    Column('idempotency_key', String(200), primary_key=True),
    Column('payload_hash', String(64), nullable=False),
    Column('job_id', String(64), nullable=False),
    Column('created', Float, nullable=False))
broadcast_batches = Table('replay_broadcast_batches', metadata,
    Column('tenant_id', String(200), primary_key=True),
    Column('idempotency_key', String(200), primary_key=True),
    Column('payload_hash', String(64), nullable=False),
    Column('job_ids_json', Text, nullable=False),
    Column('created', Float, nullable=False))
upload_reservations = Table('replay_output_reservations', metadata,
    Column('object_key', String(1024), primary_key=True),
    Column('tenant_id', String(200), nullable=False),
    Column('job_id', String(64), nullable=False),
    Column('lease_version', Integer, nullable=False),
    Column('bytes', BigInteger, nullable=False),
    Column('sha256', String(64), nullable=False),
    Column('expires_at', Float, nullable=False),
    Column('status', String(24), nullable=False))
runtime_records = Table('replay_runtimes', metadata,
    Column('job_id', String(64), primary_key=True),
    Column('lease_version', Integer, primary_key=True),
    Column('state', String(24), nullable=False),
    Column('deadline', Float, nullable=False),
    Column('updated', Float, nullable=False),
    Column('cleaned', Boolean, nullable=False))
workers = Table('replay_workers', metadata,
    Column('id', String(200), primary_key=True),
    Column('last_seen', Float, nullable=False),
    Column('metadata_json', Text, nullable=False))
Index('ix_replay_media_tenant_created', media.c.tenant_id, media.c.created)
Index('ix_replay_jobs_due', jobs.c.state, jobs.c.next_run)
Index('ix_replay_jobs_tenant_created', jobs.c.tenant_id, jobs.c.created)
Index('ix_replay_jobs_lease', jobs.c.state, jobs.c.lease_expires)
Index('ix_replay_events_job_at', job_events.c.job_id, job_events.c.at)


def _identity(value, label='identifier', limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise RepositoryError(f'Invalid {label}')
    return value


def _number(value, label, minimum=0, maximum=1e15):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise RepositoryError(f'Invalid {label}')
    return value


def _code(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_:-]{1,80}', value):
        raise RepositoryError('Invalid error code')
    return value


def _object_key(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/')) or '\\' in value or any(ord(c) < 32 for c in value):
        raise RepositoryError('Invalid object key')
    return value


class Repository:
    def __init__(self, database_url, *, tenant_concurrency=1, global_concurrency=4,
                 max_storage_bytes=10 * 1024**3, max_pending_jobs=50, max_media=100,
                 max_output_bytes=DEFAULT_MAX_OUTPUT_BYTES,
                 max_daily_runtime_seconds=86400, reservation_grace=60,
                 retry_delay=5, max_events=100, validation_duration=600,
                 runtime_allowance=120, clock=time.time, create_schema=False):
        self.clock = clock
        self._memory_lock = threading.RLock()
        # The Google application supplies its account admission check. It runs
        # under the same coordination lock as suspension and job cancellation.
        # Other authentication modes and offline worker repositories keep None.
        self.admission_guard = None
        for name, value in [('tenant_concurrency', tenant_concurrency), ('global_concurrency', global_concurrency),
                            ('max_storage_bytes', max_storage_bytes), ('max_pending_jobs', max_pending_jobs),
                            ('max_media', max_media), ('max_daily_runtime_seconds', max_daily_runtime_seconds),
                            ('max_events', max_events)]:
            _number(value, name, 1)
            setattr(self, name, int(value))
        self.reservation_grace = _number(reservation_grace, 'reservation_grace', 0, 3600)
        self.max_output_bytes = int(_number(max_output_bytes, 'max_output_bytes', 1, MAX_OUTPUT_BYTES))
        self.retry_delay = _number(retry_delay, 'retry_delay', 1, 300)
        self.validation_duration = _number(validation_duration, 'validation duration', 1, 14400)
        self.runtime_allowance = _number(runtime_allowance, 'runtime allowance', 0, 3600)
        opts = {'pool_pre_ping': True}
        if database_url.startswith('sqlite'):
            opts['connect_args'] = {'check_same_thread': False, 'timeout': 30}
            if database_url.endswith(':memory:') or database_url in ('sqlite://', 'sqlite+pysqlite://'):
                opts['poolclass'] = StaticPool
        self.engine = create_engine(database_url, **opts)
        self.sqlite = self.engine.dialect.name == 'sqlite'
        if self.engine.dialect.name not in ('sqlite', 'postgresql'):
            self.engine.dispose()
            raise RepositoryError('Only PostgreSQL and development SQLite are supported')
        if self.sqlite:
            @event.listens_for(self.engine, 'connect')
            def enable_foreign_keys(dbapi_connection, _):
                dbapi_connection.execute('PRAGMA foreign_keys=ON')
                dbapi_connection.execute('PRAGMA busy_timeout=30000')
        if create_schema:
            self.create_schema()

    def close(self):
        self.engine.dispose()

    def create_schema(self):
        """For local tests/bootstrap only; production deploys versioned SQL migrations."""
        metadata.create_all(self.engine)
        with self._transaction() as conn:
            if conn.execute(select(coordination.c.id).where(coordination.c.id == 1)).first() is None:
                conn.execute(insert(coordination).values(id=1))

    def migrate_watch_links(self):
        """Explicit pre-start upgrade for persistent development SQLite only."""
        if not self.sqlite:
            raise RepositoryError('Production requires versioned SQL migrations')
        with self._transaction() as conn:
            columns = {column['name'] for column in inspect(conn).get_columns('replay_jobs')}
            for name in ('channel_url', 'broadcast_url'):
                if name not in columns:
                    conn.exec_driver_sql(f"ALTER TABLE replay_jobs ADD COLUMN {name} VARCHAR(2048) NOT NULL DEFAULT ''")

    @contextmanager
    def _transaction(self):
        # StaticPool shares one in-memory SQLite connection between threads.
        # File-backed SQLite and PostgreSQL use the database's own transaction locks.
        from contextlib import nullcontext
        lock = self._memory_lock if isinstance(self.engine.pool, StaticPool) else nullcontext()
        with lock, self.engine.connect() as conn:
            if self.sqlite:
                conn.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                conn.begin()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _gate(self, conn):
        # Capacity/admission and lease transitions share a short transaction lock.
        # This prevents write skew across otherwise independent tenant job rows.
        row = conn.execute(select(coordination.c.id).where(coordination.c.id == 1).with_for_update()).first()
        if row is None:
            raise RepositoryError('Database migration is required')

    def _now(self, conn):
        # Default production leases use one authoritative database clock across
        # control-plane instances. An explicit injected clock remains available
        # for deterministic expiry tests.
        if not self.sqlite and self.clock is time.time:
            return float(conn.execute(select(func.extract('epoch', func.clock_timestamp()))).scalar_one())
        return self.clock()

    def _check_admission(self, conn, tenant_id):
        if self.admission_guard is not None:
            self.admission_guard(conn, tenant_id)

    @contextmanager
    def _read(self):
        from contextlib import nullcontext
        lock = self._memory_lock if isinstance(self.engine.pool, StaticPool) else nullcontext()
        with lock, self.engine.connect() as conn:
            yield conn

    @staticmethod
    def _public(row):
        result = dict(row)
        for name in ('secret_ciphertext', 'payload_hash', 'lease_token', 'worker_id', 'idempotency_key'):
            result.pop(name, None)
        return result

    def _event(self, conn, row, code, now):
        conn.execute(insert(job_events).values(tenant_id=row['tenant_id'], job_id=row['id'], at=now, code=_code(code)))
        stale = select(job_events.c.id).where(job_events.c.job_id == row['id']).order_by(job_events.c.id.desc()).offset(self.max_events)
        conn.execute(delete(job_events).where(job_events.c.id.in_(stale)))

    def _media(self, conn, tenant_id, media_id, *, include_deleted=False):
        stmt = select(media).where(media.c.tenant_id == tenant_id, media.c.id == media_id)
        if not include_deleted:
            stmt = stmt.where(media.c.status.not_in(['deleted', 'deleting']))
        row = conn.execute(stmt).mappings().first()
        if row is None:
            raise NotFound('Media not found')
        return dict(row)

    def _job(self, conn, tenant_id, job_id):
        row = conn.execute(select(jobs, media.c.name.label('media_name'), media.c.duration).join(
            media, and_(jobs.c.media_id == media.c.id, jobs.c.tenant_id == media.c.tenant_id)).where(
            jobs.c.tenant_id == tenant_id, jobs.c.id == job_id)).mappings().first()
        if row is None:
            raise NotFound('Job not found')
        return dict(row)

    def _storage_totals(self, conn, tenant_id):
        originals, incoming = conn.execute(select(
            func.coalesce(func.sum(media.c.bytes), 0),
            func.coalesce(func.sum(case((media.c.status.in_(['uploading', 'pending', 'validating']), media.c.bytes), else_=0)), 0)
        ).where(media.c.tenant_id == tenant_id, media.c.status != 'deleted')).one()
        outputs, held = conn.execute(select(
            func.coalesce(func.sum(jobs.c.output_bytes), 0),
            func.coalesce(func.sum(jobs.c.output_reserved_bytes), 0)
        ).where(jobs.c.tenant_id == tenant_id)).one()
        pending = conn.execute(select(func.coalesce(func.sum(upload_reservations.c.bytes), 0)).where(
            upload_reservations.c.tenant_id == tenant_id, upload_reservations.c.status == 'pending')).scalar_one()
        return int(originals + outputs + held + pending), int(incoming + held + pending)

    def _storage_used(self, conn, tenant_id):
        return self._storage_totals(conn, tenant_id)[0]

    def _output_budget(self, duration):
        budget = estimate_output_bytes(duration)
        if budget > self.max_output_bytes:
            raise OutputCapacityExceeded('OUTPUT_LIMIT_EXCEEDED')
        return budget

    @staticmethod
    def _media_metadata(duration, width, height, fps):
        _number(duration, 'duration', 1, 4 * 3600)
        _number(width, 'width', 1, 1920)
        _number(height, 'height', 1, 1920)
        _number(fps, 'fps', 0.01, 60)
        if int(width) != width or int(height) != height or min(width, height) > 1080 or int(width) % 2 or int(height) % 2:
            raise RepositoryError('Invalid video dimensions')

    def add_media(self, tenant_id, *, name, object_key, bytes, duration=0, width=0,
                  height=0, fps=0, sha256='', etag='', status='ready', id=None):
        _identity(tenant_id, 'tenant')
        name = _identity(name, 'media name', 180)
        _object_key(object_key)
        _number(bytes, 'media bytes', 1, self.max_storage_bytes)
        if int(bytes) != bytes or status not in ('uploading', 'pending', 'ready'):
            raise RepositoryError('Invalid media metadata')
        if sha256 and not re.fullmatch(r'[a-f0-9]{64}', sha256):
            raise RepositoryError('Invalid SHA256')
        if len(etag) > 200:
            raise RepositoryError('Invalid ETag')
        if status == 'ready':
            self._media_metadata(duration, width, height, fps)
        elif (duration, width, height, fps) != (0, 0, 0, 0):
            self._media_metadata(duration, width, height, fps)
        now = self.clock()
        item = dict(id=_identity(id or uuid.uuid4().hex, 'media ID', 64), tenant_id=tenant_id,
                    name=name, object_key=object_key, bytes=int(bytes), duration=duration,
                    width=width, height=height, fps=fps, sha256=sha256, etag=etag,
                    status=status, created=now, updated=now)
        with self._transaction() as conn:
            self._gate(conn)
            self._check_admission(conn, tenant_id)
            count = conn.execute(select(func.count()).select_from(media).where(
                media.c.tenant_id == tenant_id, media.c.status != 'deleted')).scalar_one()
            if count >= self.max_media or self._storage_used(conn, tenant_id) + bytes > self.max_storage_bytes:
                raise QuotaExceeded('Media storage quota exceeded')
            if conn.execute(select(media.c.id).where(or_(media.c.id == item['id'], media.c.object_key == object_key))).first():
                raise Conflict('Media already exists')
            conn.execute(insert(media).values(**item))
        return item

    def update_media(self, tenant_id, media_id, *, status, duration=None, width=None,
                     height=None, fps=None, error_code=None, etag=None):
        if status not in ('pending', 'validating', 'ready', 'failed'):
            raise RepositoryError('Invalid media status')
        with self._transaction() as conn:
            self._gate(conn)
            row = self._media(conn, tenant_id, media_id)
            if row['status'] == 'ready' and status != 'ready':
                raise Conflict('Validated media is immutable')
            values = {'status': status, 'error_code': _code(error_code), 'updated': self.clock()}
            for key, value in [('duration', duration), ('width', width), ('height', height), ('fps', fps)]:
                if value is not None:
                    values[key] = value
            proposed = {**row, **values}
            if status == 'ready':
                self._media_metadata(proposed['duration'], proposed['width'], proposed['height'], proposed['fps'])
                if row['status'] == 'ready' and any(proposed[k] != row[k] for k in ('duration', 'width', 'height', 'fps')):
                    raise Conflict('Validated media is immutable')
            if etag is not None:
                if not isinstance(etag, str) or len(etag) > 200:
                    raise RepositoryError('Invalid ETag')
                values['etag'] = etag
            conn.execute(update(media).where(media.c.id == media_id, media.c.tenant_id == tenant_id).values(**values))
            return {**row, **values}

    def get_media(self, tenant_id, media_id):
        with self._read() as conn:
            return self._media(conn, tenant_id, media_id)

    def list_media(self, tenant_id, *, limit=100):
        with self._read() as conn:
            return [dict(row) for row in conn.execute(select(media).where(media.c.tenant_id == tenant_id,
                media.c.status.not_in(['deleted', 'deleting'])).order_by(media.c.created.desc()).limit(min(max(int(limit), 1), 500))).mappings()]

    def _capacity(self, conn, tenant_id, start, end):
        rows = conn.execute(select(jobs.c.tenant_id, jobs.c.scheduled, jobs.c.reserved_until).where(
            jobs.c.state.in_(NONTERMINAL), jobs.c.scheduled < end, jobs.c.reserved_until > start)).mappings().all()
        # Half-open interval sweep: a release at t happens before an admission at t.
        for tenant, cap in [(None, self.global_concurrency), (tenant_id, self.tenant_concurrency)]:
            points = [(start, 1), (end, -1)]
            for row in rows:
                if tenant is None or row['tenant_id'] == tenant:
                    points.extend([(max(start, row['scheduled']), 1), (min(end, row['reserved_until']), -1)])
            count = 0
            for _, delta in sorted(points):
                count += delta
                if count > cap:
                    raise QuotaExceeded('The requested broadcast time has no available capacity')

    def create_job(self, tenant_id, *, media_id, title, target, idempotency_key,
                   scheduled=None, secret_ciphertext=None, deadline=None,
                   max_attempts=3, request_fingerprint=None, channel_url='', broadcast_url=''):
        with self._transaction() as conn:
            self._gate(conn)
            return self._create_job(conn, self._now(conn), tenant_id, media_id=media_id, title=title,
                target=target, idempotency_key=idempotency_key, scheduled=scheduled,
                secret_ciphertext=secret_ciphertext, deadline=deadline,
                max_attempts=max_attempts, request_fingerprint=request_fingerprint,
                channel_url=channel_url, broadcast_url=broadcast_url)

    def create_import(self, tenant_id, *, name, object_key, secret_ciphertext,
                      idempotency_key, request_fingerprint, max_bytes, id=None):
        """Atomically admit an encrypted source import and its storage budget."""
        _identity(tenant_id, 'tenant')
        _identity(idempotency_key, 'idempotency key')
        name = _identity(name, 'media name', 180)
        _object_key(object_key)
        _number(max_bytes, 'import bytes', 1)
        if int(max_bytes) != max_bytes or not isinstance(request_fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', request_fingerprint):
            raise RepositoryError('Invalid import request')
        with self._transaction() as conn:
            self._gate(conn)
            self._check_admission(conn, tenant_id)
            now = self._now(conn)
            previous = conn.execute(select(idempotency_records).where(
                idempotency_records.c.tenant_id == tenant_id,
                idempotency_records.c.idempotency_key == idempotency_key)).mappings().first()
            if previous:
                if previous['payload_hash'] != request_fingerprint:
                    raise Conflict('Idempotency key was used with a different request')
                existing = conn.execute(select(jobs).where(jobs.c.id == previous['job_id'],
                    jobs.c.tenant_id == tenant_id)).mappings().first()
                if not existing or existing['target'] != 'import':
                    raise Conflict('The original import has expired; use a new idempotency key')
                return {'media': self._media(conn, tenant_id, existing['media_id']),
                        'job': {**self._public(self._job(conn, tenant_id, existing['id'])), 'replayed': True}}
            count = conn.execute(select(func.count()).select_from(media).where(
                media.c.tenant_id == tenant_id, media.c.status != 'deleted')).scalar_one()
            budget = min(int(max_bytes), self.max_storage_bytes - self._storage_used(conn, tenant_id))
            if count >= self.max_media or budget <= 0:
                raise QuotaExceeded('Media storage quota exceeded')
            item = dict(id=_identity(id or uuid.uuid4().hex, 'media ID', 64), tenant_id=tenant_id,
                name=name, object_key=object_key, bytes=0, duration=0, width=0, height=0,
                fps=0, sha256='', etag='', status='importing', created=now, updated=now)
            if conn.execute(select(media.c.id).where(or_(media.c.id == item['id'], media.c.object_key == object_key))).first():
                raise Conflict('Media already exists')
            conn.execute(insert(media).values(**item))
            job = self._create_job(conn, now, tenant_id, media_id=item['id'], title='링크 영상 가져오기',
                target='import', idempotency_key=idempotency_key, secret_ciphertext=secret_ciphertext,
                max_attempts=1, request_fingerprint=request_fingerprint, import_budget_bytes=budget)
            return {'media': item, 'job': job}

    def _create_job(self, conn, now, tenant_id, *, media_id, title, target, idempotency_key,
                   scheduled=None, secret_ciphertext=None, deadline=None,
                   max_attempts=3, request_fingerprint=None, import_budget_bytes=None,
                   channel_url='', broadcast_url=''):
        _identity(tenant_id, 'tenant')
        self._check_admission(conn, tenant_id)
        _identity(idempotency_key, 'idempotency key')
        title = _identity(title.strip(), 'job title', 120)
        if not isinstance(target, str) or target not in BROADCAST_TARGETS | PROCESSING_TARGETS:
            raise RepositoryError('Unsupported target')
        if target in PROCESSING_TARGETS and (channel_url or broadcast_url):
            raise RepositoryError('Processing jobs do not have viewing links')
        links = (normalize_watch_links(target, channel_url=channel_url, broadcast_url=broadcast_url)
                 if target in BROADCAST_TARGETS else {'channel_url': '', 'broadcast_url': ''})
        if target not in ('local', 'validate') and (not isinstance(secret_ciphertext, str) or not secret_ciphertext or len(secret_ciphertext) > 20000):
            raise RepositoryError('Encrypted stream key is required')
        if target in ('local', 'validate') and secret_ciphertext:
            raise RepositoryError('Unexpected stream key')
        if target == 'import':
            _number(import_budget_bytes, 'import budget', 1, self.max_storage_bytes)
        elif import_budget_bytes is not None:
            raise RepositoryError('Unexpected import budget')
        _number(max_attempts, 'max attempts', 1, 5)
        if int(max_attempts) != max_attempts:
            raise RepositoryError('Invalid max attempts')
        payload = dict(media_id=media_id, title=title, target=target, scheduled=scheduled,
                       secret_ciphertext=secret_ciphertext, deadline=deadline, max_attempts=max_attempts)
        payload.update({key: value for key, value in links.items() if value})
        payload_hash = request_fingerprint or hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if not re.fullmatch(r'[a-f0-9]{64}', payload_hash):
            raise RepositoryError('Invalid request fingerprint')
        if conn.execute(select(broadcast_batches.c.tenant_id).where(broadcast_batches.c.tenant_id == tenant_id,
            broadcast_batches.c.idempotency_key == idempotency_key)).first():
            raise Conflict('Idempotency key was used for a broadcast batch')
        previous = conn.execute(select(idempotency_records).where(idempotency_records.c.tenant_id == tenant_id,
            idempotency_records.c.idempotency_key == idempotency_key)).mappings().first()
        if previous is not None:
            if previous['payload_hash'] != payload_hash:
                raise Conflict('Idempotency key was used with a different request')
            if conn.execute(select(jobs.c.id).where(jobs.c.id == previous['job_id'])).first() is None:
                raise Conflict('The original job has expired; use a new idempotency key')
            return {**self._public(self._job(conn, tenant_id, previous['job_id'])), 'replayed': True}
        item = self._media(conn, tenant_id, media_id)
        if target not in PROCESSING_TARGETS and item['status'] != 'ready':
            raise Conflict('Media has not passed validation')
        if target == 'validate' and item['status'] not in ('uploading', 'pending', 'validating', 'failed'):
            raise Conflict('Media does not require validation')
        if target == 'import' and item['status'] != 'importing':
            raise Conflict('Media does not require importing')
        start = now if scheduled is None else _number(scheduled, 'scheduled time', now - 5, now + 30 * 86400)
        duration = self.validation_duration if target in PROCESSING_TARGETS else item['duration']
        envelope = (duration + self.runtime_allowance) * max_attempts + sum(self.retry_delay * 2**i for i in range(max_attempts - 1)) + self.reservation_grace
        end = start + envelope
        deadline = end if deadline is None else _number(deadline, 'deadline', end, now + 31 * 86400)
        pending = conn.execute(select(func.count()).select_from(jobs).where(jobs.c.tenant_id == tenant_id,
            jobs.c.state.in_(NONTERMINAL))).scalar_one()
        if pending >= self.max_pending_jobs:
            raise QuotaExceeded('Pending job quota exceeded')
        output_budget = (self._output_budget(duration) if target == 'local' else
                         int(import_budget_bytes) if target == 'import' else 0)
        if output_budget and self._storage_used(conn, tenant_id) + output_budget > self.max_storage_bytes:
            raise OutputCapacityExceeded()
        self._capacity(conn, tenant_id, start, end)
        day = int(start // 86400)
        usage_row = conn.execute(select(daily_usage).where(daily_usage.c.tenant_id == tenant_id, daily_usage.c.day == day)).mappings().first()
        reserved = duration * max_attempts
        if (usage_row['reserved_seconds'] if usage_row else 0) + reserved > self.max_daily_runtime_seconds:
            raise QuotaExceeded('Daily broadcast quota exceeded')
        if usage_row:
            conn.execute(update(daily_usage).where(daily_usage.c.tenant_id == tenant_id, daily_usage.c.day == day).values(
                jobs=daily_usage.c.jobs + 1, reserved_seconds=daily_usage.c.reserved_seconds + reserved))
        else:
            conn.execute(insert(daily_usage).values(tenant_id=tenant_id, day=day, jobs=1, reserved_seconds=reserved))
        row = dict(id=uuid.uuid4().hex, tenant_id=tenant_id, media_id=media_id, title=title, target=target,
                   **links,
                   secret_ciphertext=secret_ciphertext, idempotency_key=idempotency_key, payload_hash=payload_hash,
                   scheduled=start, reserved_until=end, deadline=deadline, state='scheduled', progress=0,
                   attempt=0, max_attempts=max_attempts, next_run=start, lease_version=0,
                   cancel_requested=False, output_bytes=0, output_budget_bytes=output_budget,
                   output_reserved_bytes=output_budget, created=now, updated=now)
        conn.execute(insert(jobs).values(**row))
        if target == 'validate':
            conn.execute(update(media).where(media.c.id == media_id, media.c.tenant_id == tenant_id).values(
                status='validating', error_code=None, updated=now))
        conn.execute(insert(idempotency_records).values(tenant_id=tenant_id, idempotency_key=idempotency_key,
            payload_hash=payload_hash, job_id=row['id'], created=now))
        self._event(conn, row, 'job_scheduled', now)
        return {**self._public(self._job(conn, tenant_id, row['id'])), 'replayed': False}

    def create_jobs_batch(self, tenant_id, *, media_id, title, destinations, idempotency_key,
                          scheduled=None, deadline=None, max_attempts=3, request_fingerprint=None):
        """Admit every destination in one transaction, retaining group replay history."""
        _identity(tenant_id, 'tenant')
        _identity(idempotency_key, 'idempotency key')
        if not isinstance(destinations, list) or not 1 <= len(destinations) <= 10:
            raise RepositoryError('A broadcast batch must contain one to ten destinations')
        targets = []
        normalized_destinations = []
        for destination in destinations:
            if not isinstance(destination, dict) or set(destination) - {'target', 'secret_ciphertext', 'channel_url', 'broadcast_url'}:
                raise RepositoryError('Invalid broadcast destination')
            target = destination.get('target')
            if not isinstance(target, str) or target not in BROADCAST_TARGETS or target in targets:
                raise RepositoryError('Broadcast destinations must use distinct supported platforms')
            targets.append(target)
            links = normalize_watch_links(target, channel_url=destination.get('channel_url', ''),
                                          broadcast_url=destination.get('broadcast_url', ''))
            normalized_destinations.append({**{key: value for key, value in destination.items()
                                               if key not in ('channel_url', 'broadcast_url')},
                                            **{key: value for key, value in links.items() if value}})
        destinations = normalized_destinations
        payload = dict(media_id=media_id, title=title, destinations=destinations,
                       scheduled=scheduled, deadline=deadline, max_attempts=max_attempts)
        fingerprint = request_fingerprint or hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
            raise RepositoryError('Invalid request fingerprint')
        with self._transaction() as conn:
            self._gate(conn)
            self._check_admission(conn, tenant_id)
            now = self._now(conn)
            previous = conn.execute(select(broadcast_batches).where(broadcast_batches.c.tenant_id == tenant_id,
                broadcast_batches.c.idempotency_key == idempotency_key)).mappings().first()
            if previous:
                if previous['payload_hash'] != fingerprint:
                    raise Conflict('Idempotency key was used with a different request')
                identifiers = json.loads(previous['job_ids_json'])
                if len(identifiers) != len(destinations) or conn.execute(select(func.count()).select_from(jobs).where(
                    jobs.c.tenant_id == tenant_id, jobs.c.id.in_(identifiers))).scalar_one() != len(identifiers):
                    raise Conflict('The original batch has expired; use a new idempotency key')
                return {'jobs': [self._public(self._job(conn, tenant_id, identifier)) for identifier in identifiers], 'replayed': True}
            if conn.execute(select(idempotency_records.c.job_id).where(idempotency_records.c.tenant_id == tenant_id,
                idempotency_records.c.idempotency_key == idempotency_key)).first():
                raise Conflict('Idempotency key was used for a single broadcast')
            if len(destinations) > min(self.tenant_concurrency, self.global_concurrency, 10):
                raise QuotaExceeded('Broadcast batch exceeds concurrent destination capacity')
            # One instant for all immediate jobs also makes the shared admission
            # envelope exact. A failed later destination rolls back every hold,
            # usage counter, event and child idempotency record created here.
            start = now if scheduled is None else scheduled
            group = uuid.uuid4().hex
            rows = []
            for index, destination in enumerate(destinations):
                row = self._create_job(conn, now, tenant_id, media_id=media_id, title=title,
                    target=destination['target'], secret_ciphertext=destination.get('secret_ciphertext'),
                    channel_url=destination.get('channel_url', ''), broadcast_url=destination.get('broadcast_url', ''),
                    idempotency_key=f'batch-child:{group}:{index}', scheduled=start, deadline=deadline,
                    max_attempts=max_attempts, request_fingerprint=hashlib.sha256(f'{fingerprint}:{index}'.encode()).hexdigest())
                rows.append({key: value for key, value in row.items() if key != 'replayed'})
            conn.execute(insert(broadcast_batches).values(tenant_id=tenant_id, idempotency_key=idempotency_key,
                payload_hash=fingerprint, job_ids_json=json.dumps([row['id'] for row in rows]), created=now))
            return {'jobs': rows, 'replayed': False}

    def get_job(self, tenant_id, job_id):
        with self._read() as conn:
            return self._public(self._job(conn, tenant_id, job_id))

    def update_watch_links(self, tenant_id, job_id, *, channel_url='', broadcast_url=''):
        """Edit viewing metadata only; source, idempotency and runtime stay fixed."""
        with self._transaction() as conn:
            self._gate(conn)
            row = self._job(conn, tenant_id, job_id)
            if row['target'] not in BROADCAST_TARGETS:
                raise RepositoryError('Processing jobs do not have viewing links')
            links = normalize_watch_links(row['target'], channel_url=channel_url, broadcast_url=broadcast_url)
            conn.execute(update(jobs).where(jobs.c.tenant_id == tenant_id, jobs.c.id == job_id).values(**links))
            self._event(conn, row, 'watch_links_updated', self._now(conn))
            return self._public(self._job(conn, tenant_id, job_id))

    def list_jobs(self, tenant_id, *, limit=100):
        with self._read() as conn:
            ids = conn.execute(select(jobs.c.id).where(jobs.c.tenant_id == tenant_id).order_by(jobs.c.created.desc()).limit(min(max(int(limit), 1), 500))).scalars()
            return [self._public(self._job(conn, tenant_id, job_id)) for job_id in ids]

    def events(self, tenant_id, job_id):
        with self._read() as conn:
            self._job(conn, tenant_id, job_id)
            return [dict(row) for row in conn.execute(select(job_events.c.id, job_events.c.at, job_events.c.code).where(
                job_events.c.tenant_id == tenant_id, job_events.c.job_id == job_id).order_by(job_events.c.id.desc()).limit(self.max_events)).mappings()]

    def _recover(self, conn, now):
        rows = conn.execute(select(jobs).where(or_(and_(jobs.c.state.in_(ACTIVE), jobs.c.lease_expires <= now),
            and_(jobs.c.state.in_(QUEUED), jobs.c.deadline <= now))).with_for_update(skip_locked=True)).mappings().all()
        for row in rows:
            # A lost worker may still be publishing to RTMP. Fencing protects the
            # DB but cannot revoke already-open network sockets: never auto-retry
            # an expired active lease until an operator has stopped that worker.
            state = 'stopped' if row['cancel_requested'] else 'failed'
            code = 'lease_expired' if row['state'] in ACTIVE else 'deadline_exceeded'
            conn.execute(update(jobs).where(jobs.c.id == row['id']).values(state=state, updated=now,
                error_code=code, secret_ciphertext=None, lease_token=None, lease_expires=None, worker_id=None))
            self._event(conn, row, code, now)
            self._finish_side_effects(conn, row, state, code, now)
        return len(rows)

    def recover_expired(self):
        with self._transaction() as conn:
            self._gate(conn)
            return self._recover(conn, self._now(conn))

    def claim(self, worker_id, lease_seconds=60):
        _identity(worker_id, 'worker')
        _number(lease_seconds, 'lease seconds', 5, 900)
        now = self.clock()
        with self._transaction() as conn:
            self._gate(conn)
            now = self._now(conn)
            self._recover(conn, now)
            if conn.execute(select(func.count()).select_from(jobs).where(jobs.c.state.in_(ACTIVE))).scalar_one() >= self.global_concurrency:
                return None
            candidates = conn.execute(select(jobs).where(jobs.c.state.in_(QUEUED), jobs.c.next_run <= now,
                jobs.c.cancel_requested.is_(False)).order_by(jobs.c.next_run, jobs.c.created).limit(100).with_for_update(skip_locked=True)).mappings().all()
            for candidate in candidates:
                row = dict(candidate)
                running = conn.execute(select(func.count()).select_from(jobs).where(jobs.c.tenant_id == row['tenant_id'], jobs.c.state.in_(ACTIVE))).scalar_one()
                if running >= self.tenant_concurrency:
                    continue
                item = self._media(conn, row['tenant_id'], row['media_id'])
                duration = (self.validation_duration if row['target'] in PROCESSING_TARGETS else
                            item['duration'] if row['target'] == 'local' else max(0, item['duration'] - row['progress']))
                # reservation_grace is admission-to-dispatch jitter. Consuming
                # it a second time here would reject every one-attempt job as
                # soon as even one millisecond elapsed after admission.
                if now + duration + self.runtime_allowance > min(row['deadline'], row['reserved_until']) or row['attempt'] >= row['max_attempts']:
                    conn.execute(update(jobs).where(jobs.c.id == row['id']).values(state='failed', error_code='deadline_exceeded', secret_ciphertext=None, updated=now))
                    self._event(conn, row, 'deadline_exceeded', now)
                    self._finish_side_effects(conn, row, 'failed', 'deadline_exceeded', now)
                    continue
                if row['target'] in ('local', 'import'):
                    # Migration leaves old jobs at zero. Never start a legacy
                    # job or another attempt until its full budget is held.
                    if not row['output_budget_bytes']:
                        try:
                            row['output_budget_bytes'] = self._output_budget(item['duration'])
                        except OutputCapacityExceeded as exc:
                            conn.execute(update(jobs).where(jobs.c.id == row['id']).values(
                                state='failed', error_code=exc.code, updated=now))
                            self._event(conn, row, exc.code, now)
                            self._finish_side_effects(conn, row, 'failed', exc.code, now)
                            continue
                        conn.execute(update(jobs).where(jobs.c.id == row['id']).values(
                            output_budget_bytes=row['output_budget_bytes']))
                    missing = row['output_budget_bytes'] - row['output_reserved_bytes']
                    if missing and self._storage_used(conn, row['tenant_id']) + missing > self.max_storage_bytes:
                        if row['error_code'] != 'OUTPUT_STORAGE_QUOTA_EXCEEDED':
                            conn.execute(update(jobs).where(jobs.c.id == row['id']).values(
                                error_code='OUTPUT_STORAGE_QUOTA_EXCEEDED', updated=now))
                            self._event(conn, row, 'OUTPUT_STORAGE_QUOTA_EXCEEDED', now)
                        # Previous PUT URLs remain charged until expiry and
                        # deletion acknowledgement, even across retries. A
                        # blocked tenant must not prevent other tenants running.
                        continue
                    if missing:
                        row['output_reserved_bytes'] = row['output_budget_bytes']
                        conn.execute(update(jobs).where(jobs.c.id == row['id']).values(
                            output_reserved_bytes=row['output_reserved_bytes']))
                values = dict(state='starting', attempt=row['attempt'] + 1, lease_token=uuid.uuid4().hex,
                    lease_version=row['lease_version'] + 1, lease_expires=min(now + lease_seconds, row['deadline'], row['reserved_until']),
                    worker_id=worker_id, updated=now, error_code=None)
                conn.execute(update(jobs).where(jobs.c.id == row['id']).values(**values))
                conn.execute(insert(runtime_records).values(job_id=row['id'], lease_version=values['lease_version'],
                    state='starting', deadline=min(row['deadline'], row['reserved_until']), updated=now, cleaned=False))
                self._event(conn, row, 'worker_claimed', now)
                return {**row, **values, 'media': item}
        return None

    def _lease(self, conn, job_id, lease_token, now):
        row = conn.execute(select(jobs).where(jobs.c.id == job_id).with_for_update()).mappings().first()
        if row is None or row['state'] not in ACTIVE or not lease_token or row['lease_token'] != lease_token or row['lease_expires'] <= now:
            raise LeaseLost('Worker lease has expired or changed')
        return dict(row)

    def heartbeat(self, job_id, lease_token, *, lease_seconds=60, progress=None):
        _number(lease_seconds, 'lease seconds', 5, 900)
        now = self.clock()
        with self._transaction() as conn:
            self._gate(conn)
            now = self._now(conn)
            row = self._lease(conn, job_id, lease_token, now)
            values = dict(lease_expires=min(now + lease_seconds, row['deadline'], row['reserved_until']), updated=now)
            if progress is not None:
                item = self._media(conn, row['tenant_id'], row['media_id'])
                _number(progress, 'progress', row['progress'], max(self.validation_duration, item['duration']) if row['target'] in PROCESSING_TARGETS else item['duration'])
                values['progress'] = progress
                if progress > 0 and not row['cancel_requested']:
                    values['state'] = 'streaming'
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))
            conn.execute(update(runtime_records).where(runtime_records.c.job_id == job_id,
                runtime_records.c.lease_version == row['lease_version']).values(state=values.get('state', row['state']), updated=now))
            return self._public({**row, **values})

    def _finish_side_effects(self, conn, row, state, error_code, now, validation_metadata=None):
        if state in TERMINAL:
            # Callers include restore tools with only the identity columns in
            # row. Pending PUTs are a separate liability and are not released.
            conn.execute(update(jobs).where(jobs.c.id == row['id']).values(output_reserved_bytes=0))
        conn.execute(update(runtime_records).where(runtime_records.c.job_id == row['id'],
            runtime_records.c.lease_version == row['lease_version']).values(state=state, updated=now))
        if row['target'] in PROCESSING_TARGETS and state in TERMINAL:
            if state == 'completed':
                info = validation_metadata or {}
                self._media_metadata(info.get('duration'), info.get('width'), info.get('height'), info.get('fps'))
                values = {key: info[key] for key in ('duration', 'width', 'height', 'fps')}
                if row['target'] == 'import':
                    values.update(object_key=info['object_key'], bytes=info['bytes'], sha256=info['sha256'])
                values.update(status='ready', error_code=None, updated=now)
            else:
                values = dict(status='failed', error_code=error_code or
                    ('SOURCE_IMPORT_FAILED' if row['target'] == 'import' else 'validation_failed'), updated=now)
            conn.execute(update(media).where(media.c.id == row['media_id'], media.c.tenant_id == row['tenant_id']).values(**values))
        for pending in conn.execute(select(upload_reservations).where(upload_reservations.c.job_id == row['id'],
                upload_reservations.c.lease_version == row['lease_version'], upload_reservations.c.status == 'pending')).mappings():
            self._enqueue_delete(conn, row['tenant_id'], pending['object_key'], now)

    def reserve_output(self, job_id, lease_token, *, object_key, bytes, sha256, expires=900):
        """Convert held budget to an immutable PUT liability, without double counting."""
        _object_key(object_key)
        _number(bytes, 'output bytes', 1)
        _number(expires, 'upload expiry', 1, 900)
        if int(bytes) != bytes or not isinstance(sha256, str) or not re.fullmatch('[a-f0-9]{64}', sha256):
            raise RepositoryError('Invalid output metadata')
        now = self.clock()
        with self._transaction() as conn:
            self._gate(conn)
            now = self._now(conn)
            row = self._lease(conn, job_id, lease_token, now)
            if row['target'] not in ('local', 'import') or row['cancel_requested']:
                raise Conflict('This job cannot create an output')
            if bytes > row['output_budget_bytes']:
                raise OutputCapacityExceeded('OUTPUT_LIMIT_EXCEEDED')
            previous = conn.execute(select(upload_reservations).where(upload_reservations.c.object_key == object_key)).mappings().first()
            if previous:
                if any(previous[key] != value for key, value in [('job_id', job_id), ('lease_version', row['lease_version']), ('bytes', bytes), ('sha256', sha256)]) or previous['status'] != 'pending':
                    raise Conflict('Output upload reservation does not match')
                expires_at = max(previous['expires_at'], now + expires)
                conn.execute(update(upload_reservations).where(upload_reservations.c.object_key == object_key).values(expires_at=expires_at))
            else:
                if conn.execute(select(upload_reservations.c.object_key).where(upload_reservations.c.job_id == job_id,
                    upload_reservations.c.lease_version == row['lease_version'])).first():
                    raise Conflict('The worker already reserved a different output')
                if bytes > row['output_reserved_bytes']:
                    raise OutputCapacityExceeded()
                expires_at = now + expires
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(
                    output_reserved_bytes=row['output_reserved_bytes'] - int(bytes)))
                conn.execute(insert(upload_reservations).values(object_key=object_key, tenant_id=row['tenant_id'],
                    job_id=job_id, lease_version=row['lease_version'], bytes=bytes, sha256=sha256, expires_at=expires_at, status='pending'))
            return {'object_key': object_key, 'bytes': bytes, 'sha256': sha256, 'expires_at': expires_at}

    def finish(self, job_id, lease_token, *, state, error_code=None, output_key=None,
               output_bytes=0, progress=None, validation_metadata=None):
        if state not in TERMINAL | {'retry_wait'}:
            raise RepositoryError('Invalid completion state')
        _code(error_code)
        _number(output_bytes, 'output bytes', 0, self.max_storage_bytes)
        if output_key:
            _object_key(output_key)
        elif output_bytes:
            raise RepositoryError('Output key is required')
        now = self.clock()
        with self._transaction() as conn:
            self._gate(conn)
            now = self._now(conn)
            row = self._lease(conn, job_id, lease_token, now)
            if row['cancel_requested']:
                state, error_code = 'stopped', None
            item = self._media(conn, row['tenant_id'], row['media_id'])
            position = row['progress'] if progress is None else _number(progress, 'progress', row['progress'], max(self.validation_duration, item['duration']))
            if state == 'completed' and row['target'] not in PROCESSING_TARGETS and position + max(.5, item['duration'] * .01) < item['duration']:
                state, error_code = 'failed', 'incomplete_output'
            next_run = now + self.retry_delay * 2 ** max(0, row['attempt'] - 1)
            remaining = (self.validation_duration if row['target'] in PROCESSING_TARGETS else
                         item['duration'] if row['target'] == 'local' else max(0, item['duration'] - position))
            if state == 'retry_wait' and (row['attempt'] >= row['max_attempts'] or next_run + remaining + self.runtime_allowance > min(row['deadline'], row['reserved_until'])):
                state, error_code = 'failed', error_code or 'retry_exhausted'
            if state == 'completed' and row['target'] == 'import' and not output_key:
                raise Conflict('Imported media requires a verified upload')
            if output_key and state == 'completed':
                reserved = conn.execute(select(upload_reservations).where(upload_reservations.c.object_key == output_key,
                    upload_reservations.c.job_id == job_id, upload_reservations.c.lease_version == row['lease_version'],
                    upload_reservations.c.status == 'pending')).mappings().first()
                if reserved is None or reserved['bytes'] != output_bytes:
                    raise Conflict('Output does not have a matching upload reservation')
                if row['target'] == 'import':
                    validation_metadata = {**(validation_metadata or {}), 'object_key': output_key,
                                           'bytes': int(output_bytes), 'sha256': reserved['sha256']}
                conn.execute(update(upload_reservations).where(upload_reservations.c.object_key == output_key).values(status='committed'))
            values = dict(state=state, error_code=error_code, progress=position, next_run=next_run,
                          lease_token=None, lease_expires=None, worker_id=None, updated=now)
            if state in TERMINAL:
                values['secret_ciphertext'] = None
            if output_key and state == 'completed' and row['target'] != 'import':
                values.update(output_key=output_key, output_bytes=int(output_bytes))
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))
            self._event(conn, row, error_code or f'job_{state}', now)
            self._finish_side_effects(conn, row, state, error_code, now, validation_metadata)
            return self._public(self._job(conn, row['tenant_id'], job_id))

    def cancel(self, tenant_id, job_id):
        with self._transaction() as conn:
            self._gate(conn)
            now = self._now(conn)
            row = self._job(conn, tenant_id, job_id)
            return self._cancel_job(conn, row, now)

    def _cancel_job(self, conn, row, now):
        if row['state'] in TERMINAL:
            return self._public(row)
        values = {'cancel_requested': True, 'updated': now,
                  'state': 'stopping' if row['state'] in ACTIVE else 'stopped'}
        if values['state'] == 'stopped':
            values['secret_ciphertext'] = None
        conn.execute(update(jobs).where(jobs.c.id == row['id'],
            jobs.c.tenant_id == row['tenant_id']).values(**values))
        self._event(conn, row, 'cancel_requested', now)
        if values['state'] == 'stopped':
            self._finish_side_effects(conn, row, 'stopped', 'cancel_requested', now)
        else:
            conn.execute(update(runtime_records).where(runtime_records.c.job_id == row['id'],
                runtime_records.c.lease_version == row['lease_version']).values(state='stopping', updated=now))
        return self._public(self._job(conn, row['tenant_id'], row['id']))

    def cancel_member_jobs(self, conn, tenant_id, now):
        """Cancel a member's jobs inside the caller's suspension transaction.

        The caller must own _gate(conn). Active leases remain valid long enough
        for workers to observe stopping and clean up their runtime; queued work
        loses its secret and releases its storage reservation immediately.
        """
        rows = conn.execute(select(jobs).where(jobs.c.tenant_id == tenant_id,
            jobs.c.state.in_(NONTERMINAL)).order_by(jobs.c.id).with_for_update()).mappings().all()
        for row in rows:
            self._cancel_job(conn, dict(row), now)
        return len(rows)

    def usage(self, tenant_id):
        with self._read() as conn:
            row = conn.execute(select(daily_usage).where(daily_usage.c.tenant_id == tenant_id,
                daily_usage.c.day == int(self.clock() // 86400))).mappings().first()
            used, reserved = self._storage_totals(conn, tenant_id)
            return {'storage_bytes': used, 'storage_limit_bytes': self.max_storage_bytes,
                    'storage_reserved_bytes': reserved, 'storage_available_bytes': max(0, self.max_storage_bytes - used),
                    'pending_jobs': conn.execute(select(func.count()).select_from(jobs).where(jobs.c.tenant_id == tenant_id, jobs.c.state.in_(NONTERMINAL))).scalar_one(),
                    'reserved_runtime_seconds_today': row['reserved_seconds'] if row else 0,
                    'broadcast_jobs_today': row['jobs'] if row else 0,
                    'runtime_limit_seconds_per_day': self.max_daily_runtime_seconds}

    def _enqueue_delete(self, conn, tenant_id, object_key, now):
        old = conn.execute(select(object_deletions).where(object_deletions.c.object_key == object_key)).mappings().first()
        if old:
            return dict(old)
        upload_expiry = conn.execute(select(upload_reservations.c.expires_at).where(upload_reservations.c.object_key == object_key)).scalar_one_or_none()
        media_created = conn.execute(select(media.c.created).where(media.c.object_key == object_key)).scalar_one_or_none()
        # A previously issued PUT can recreate an object after deletion. Wait
        # until every authorization has expired before acknowledging removal.
        not_before = max(now, upload_expiry or 0, (media_created + 900) if media_created is not None else 0)
        row = dict(id=uuid.uuid4().hex, tenant_id=tenant_id, object_key=object_key, created=now, not_before=not_before, attempt=0)
        conn.execute(insert(object_deletions).values(**row))
        return row

    def delete_media(self, tenant_id, media_id):
        """Tombstone media and atomically queue object deletion; return its key."""
        with self._transaction() as conn:
            self._gate(conn)
            row = self._media(conn, tenant_id, media_id, include_deleted=True)
            if row['status'] == 'deleted':
                return row['object_key']
            if conn.execute(select(jobs.c.id).where(jobs.c.tenant_id == tenant_id, jobs.c.media_id == media_id,
                jobs.c.state.in_(NONTERMINAL)).limit(1)).first():
                raise Conflict('Media is used by a queued or running job')
            conn.execute(update(media).where(media.c.id == media_id, media.c.tenant_id == tenant_id).values(status='deleting', updated=self.clock()))
            self._enqueue_delete(conn, tenant_id, row['object_key'], self.clock())
            return row['object_key']

    def retention_candidates(self, before, limit=100):
        """Read-only retention plan. It never includes active/recently used media."""
        _number(before, 'retention cutoff')
        with self._read() as conn:
            rows = conn.execute(select(media).where(media.c.created < before,
                media.c.status.not_in(['deleted', 'deleting']), ~select(jobs.c.id).where(
                    jobs.c.media_id == media.c.id, or_(jobs.c.state.in_(NONTERMINAL), jobs.c.updated >= before)).exists()
                ).order_by(media.c.created).limit(min(max(int(limit), 1), 1000))).mappings().all()
            return [dict(row) for row in rows]

    def cleanup(self, before, limit=100):
        """Queue deletions durably; blob removal must be acknowledged separately.

        The dispatcher drains ``pending_deletions`` and calls
        ``confirm_deletion`` only after storage.delete succeeds. Quota is not
        released before deletion succeeds, and failures remain retryable.
        """
        _number(before, 'retention cutoff')
        limit = min(max(int(limit), 1), 1000)
        now = self.clock()
        with self._transaction() as conn:
            self._gate(conn)
            # Reconcile interrupted administrative/restore paths even when
            # history retention is not due. Physical PUT records remain charged.
            abandoned_holds = select(jobs.c.id).where(jobs.c.state.in_(TERMINAL),
                jobs.c.output_reserved_bytes > 0).limit(limit)
            conn.execute(update(jobs).where(jobs.c.id.in_(abandoned_holds)).values(output_reserved_bytes=0))
            old_jobs = conn.execute(select(jobs).where(jobs.c.state.in_(TERMINAL), jobs.c.updated < before)
                .order_by(jobs.c.updated).limit(limit)).mappings().all()
            removed = queued = 0
            for row in old_jobs:
                if row['output_key']:
                    self._enqueue_delete(conn, row['tenant_id'], row['output_key'], now)
                    queued += 1
                else:
                    conn.execute(delete(jobs).where(jobs.c.id == row['id']))
                    removed += 1
            old_media = conn.execute(select(media).where(media.c.created < before,
                media.c.status.not_in(['deleted', 'deleting']), ~select(jobs.c.id).where(
                    jobs.c.media_id == media.c.id, or_(jobs.c.state.in_(NONTERMINAL), jobs.c.updated >= before)).exists()
                ).order_by(media.c.created).limit(limit)).mappings().all()
            for row in old_media:
                conn.execute(update(media).where(media.c.id == row['id']).values(status='deleting', updated=now))
                self._enqueue_delete(conn, row['tenant_id'], row['object_key'], now)
                queued += 1
            # Restore/recovery may mark a job terminal without running its
            # ordinary finish callback. Sweep those expired output intents too.
            orphan_uploads = conn.execute(select(upload_reservations).where(
                upload_reservations.c.status == 'pending', upload_reservations.c.expires_at <= now,
                ~select(jobs.c.id).where(jobs.c.id == upload_reservations.c.job_id,
                    jobs.c.lease_version == upload_reservations.c.lease_version,
                    jobs.c.state.in_(ACTIVE)).exists()).limit(limit)).mappings().all()
            for row in orphan_uploads:
                self._enqueue_delete(conn, row['tenant_id'], row['object_key'], now)
                queued += 1
            conn.execute(delete(media).where(media.c.status == 'deleted', media.c.updated < before,
                ~select(jobs.c.id).where(jobs.c.media_id == media.c.id).exists()))
            conn.execute(delete(workers).where(workers.c.last_seen < now - 7 * 86400))
            conn.execute(delete(runtime_records).where(runtime_records.c.cleaned.is_(True), runtime_records.c.updated < before))
            return {'jobs_deleted': removed, 'objects_queued': queued}

    def pending_deletions(self, limit=100):
        with self._read() as conn:
            return [dict(row) for row in conn.execute(select(object_deletions).where(object_deletions.c.not_before <= self.clock()).order_by(object_deletions.c.created)
                .limit(min(max(int(limit), 1), 1000))).mappings()]

    def confirm_deletion(self, deletion_id):
        with self._transaction() as conn:
            self._gate(conn)
            row = conn.execute(select(object_deletions).where(object_deletions.c.id == deletion_id)).mappings().first()
            if row is None:
                return False
            if row['not_before'] > self.clock():
                raise Conflict('Outstanding upload authorization has not expired')
            conn.execute(update(media).where(media.c.tenant_id == row['tenant_id'], media.c.object_key == row['object_key'],
                media.c.status == 'deleting').values(status='deleted', updated=self.clock()))
            conn.execute(update(jobs).where(jobs.c.tenant_id == row['tenant_id'], jobs.c.output_key == row['object_key'],
                jobs.c.state.in_(TERMINAL)).values(output_key=None, output_bytes=0))
            conn.execute(delete(object_deletions).where(object_deletions.c.id == deletion_id))
            conn.execute(delete(upload_reservations).where(upload_reservations.c.object_key == row['object_key']))
            return True

    def deletion_failed(self, deletion_id, error_code='storage_delete_failed'):
        with self._transaction() as conn:
            attempt = conn.execute(select(object_deletions.c.attempt).where(
                object_deletions.c.id == deletion_id)).scalar_one_or_none()
            if attempt is None:
                return
            # A permanent object-store failure must not create a fresh wakeup
            # every few seconds. Keep deletion durable with bounded backoff.
            delay = min(6 * 3600, 60 * 2 ** min(int(attempt), 9))
            conn.execute(update(object_deletions).where(object_deletions.c.id == deletion_id).values(
                attempt=object_deletions.c.attempt + 1, error_code=_code(error_code),
                not_before=self.clock() + delay))

    def heartbeat_worker(self, worker_id, metadata=None):
        _identity(worker_id, 'worker')
        # Only caller-approved operational tags; never accept arbitrary URLs/secrets.
        allowed = {k: v for k, v in (metadata or {}).items() if k in ('version', 'role', 'draining') and isinstance(v, (str, bool, int))}
        serialized = json.dumps(allowed, sort_keys=True)
        if len(serialized) > 1000:
            raise RepositoryError('Worker metadata is too large')
        with self._transaction() as conn:
            self._gate(conn)
            row = conn.execute(select(workers.c.id).where(workers.c.id == worker_id)).first()
            if row:
                conn.execute(update(workers).where(workers.c.id == worker_id).values(last_seen=self.clock(), metadata_json=serialized))
            else:
                conn.execute(insert(workers).values(id=worker_id, last_seen=self.clock(), metadata_json=serialized))

    def worker_health(self, max_age=60):
        _number(max_age, 'worker max age', 1, 3600)
        now = self.clock()
        with self._read() as conn:
            rows = conn.execute(select(workers).where(workers.c.last_seen >= now - max_age)).mappings().all()
            latest = conn.execute(select(func.max(workers.c.last_seen))).scalar_one()
            due = conn.execute(select(func.min(jobs.c.next_run)).where(jobs.c.state.in_(QUEUED), jobs.c.next_run <= now)).scalar_one()
            return {'alive': bool(rows), 'workers': len(rows), 'last_seen': latest,
                    'queue_lag_seconds': max(0, now - due) if due is not None else 0}

    def ping(self):
        with self._read() as conn:
            if conn.execute(select(coordination.c.id).where(coordination.c.id == 1)).first() is None:
                raise RepositoryError('Database migration is required')
        return True

    def next_wakeup(self):
        """Next durable work/lease deadline; idle projects need no minute poll."""
        with self._read() as conn:
            queued = conn.execute(select(func.min(jobs.c.next_run)).where(
                jobs.c.state.in_(QUEUED), jobs.c.cancel_requested.is_(False))).scalar_one()
            lease = conn.execute(select(func.min(jobs.c.lease_expires)).where(
                jobs.c.state.in_(ACTIVE))).scalar_one()
            cleanup = conn.execute(select(func.count()).select_from(runtime_records).where(
                runtime_records.c.cleaned.is_(False), runtime_records.c.state.in_(TERMINAL | {'retry_wait'}))).scalar_one()
            deletion = conn.execute(select(func.min(object_deletions.c.not_before))).scalar_one()
            upload_expiry = conn.execute(select(func.min(upload_reservations.c.expires_at)).where(
                upload_reservations.c.status == 'pending',
                ~select(object_deletions.c.id).where(object_deletions.c.object_key == upload_reservations.c.object_key).exists(),
                ~select(jobs.c.id).where(
                    jobs.c.id == upload_reservations.c.job_id,
                    jobs.c.lease_version == upload_reservations.c.lease_version,
                    jobs.c.state.in_(ACTIVE)).exists())).scalar_one()
            candidates = [value for value in (queued, lease, deletion, upload_expiry) if value is not None]
            if cleanup:
                candidates.append(self.clock())
            return min(candidates) if candidates else None

    def runtimes(self, limit=200):
        """Control plane only: every uncleaned attempt, including earlier retries."""
        with self._read() as conn:
            return [dict(row) for row in conn.execute(select(runtime_records.c.job_id.label('id'),
                runtime_records.c.lease_version, runtime_records.c.state, runtime_records.c.deadline,
                runtime_records.c.updated).where(runtime_records.c.cleaned.is_(False))
                .order_by(runtime_records.c.updated).limit(min(max(int(limit), 1), 1000))).mappings()]

    def mark_runtime_cleaned(self, job_id, version):
        with self._transaction() as conn:
            row = conn.execute(select(runtime_records).where(runtime_records.c.job_id == job_id,
                runtime_records.c.lease_version == version)).mappings().first()
            if row is None:
                return False
            if row['state'] in ACTIVE:
                raise Conflict('An active runtime cannot be marked cleaned')
            conn.execute(update(runtime_records).where(runtime_records.c.job_id == job_id,
                runtime_records.c.lease_version == version).values(cleaned=True))
            return True
