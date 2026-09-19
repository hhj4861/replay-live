"""Durable operational aggregates and a credential-free alert outbox.

Only aggregate counts, fixed alert codes and timestamps leave this module.
Collection never sends notifications. A trusted dispatcher acknowledges alerts
only after its separately configured delivery succeeds.
"""
import math
import re
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import (CheckConstraint, Column, Float, Index, Integer, MetaData,
                        String, Table, cast, delete, func, insert, or_, select, update)
from sqlalchemy.dialects.postgresql import JSONB, insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from .repository import ACTIVE, QUEUED, jobs, object_deletions, workers

metadata = MetaData()
outbox = Table('replay_alert_outbox', metadata,
    Column('id', String(32), primary_key=True),
    Column('code', String(80), nullable=False),
    Column('count', Integer, nullable=False),
    Column('created', Float, nullable=False),
    Column('acknowledged_at', Float),
    CheckConstraint('count >= 1', name='ck_replay_alert_count'))
cooldowns = Table('replay_alert_cooldowns', metadata,
    Column('code', String(80), primary_key=True),
    Column('next_emit_at', Float, nullable=False))
Index('ix_replay_alert_outbox_pending', outbox.c.acknowledged_at, outbox.c.created)
Index('uq_replay_pending_alert_code', outbox.c.code, unique=True,
      postgresql_where=outbox.c.acknowledged_at.is_(None), sqlite_where=outbox.c.acknowledged_at.is_(None))


class Operations:
    def __init__(self, engine, clock=time.time, *, cooldown=900, thresholds=None,
                 acknowledged_retention=30 * 86400):
        if engine.dialect.name not in ('postgresql', 'sqlite'):
            raise ValueError('Operations requires PostgreSQL or development SQLite')
        self.engine, self.clock = engine, clock
        self._insert = postgres_insert if engine.dialect.name == 'postgresql' else sqlite_insert
        self.thresholds = {'queue_lag_seconds': 120, 'stale_leases': 1, 'failed_last_hour': 5,
                           'dispatcher_age_seconds': 150, 'outstanding_deletions': 100,
                           'deletion_lag_seconds': 3600}
        if thresholds and not set(thresholds) <= self.thresholds.keys():
            raise ValueError('Unknown operational threshold')
        self.thresholds.update(thresholds or {})
        for value in (*self.thresholds.values(), cooldown, acknowledged_retention):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Operational thresholds and retention must be finite and positive')
        self.cooldown, self.acknowledged_retention = cooldown, acknowledged_retention

    def migrate(self):
        """Explicit fixture/bootstrap migration; deploys use versioned SQL."""
        metadata.create_all(self.engine)

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError('Invalid operational clock')
        return float(now)

    def _snapshot(self, connection, now):
        def count(table, *criteria):
            return int(connection.execute(select(func.count()).select_from(table).where(*criteria)).scalar_one())
        due = jobs.c.state.in_(QUEUED) & (jobs.c.next_run <= now)
        oldest_due = connection.execute(select(func.min(jobs.c.next_run)).where(due)).scalar_one()
        role = (cast(workers.c.metadata_json, JSONB)['role'].astext if self.engine.dialect.name == 'postgresql'
                else func.json_extract(workers.c.metadata_json, '$.role'))
        last_seen = connection.execute(select(func.max(workers.c.last_seen)).where(role == 'dispatcher')).scalar_one()
        oldest_deletion = connection.execute(select(func.min(object_deletions.c.not_before)).where(
            object_deletions.c.not_before <= now)).scalar_one()
        return {'at': now,
            'due_jobs': count(jobs, due),
            'queued_jobs': count(jobs, jobs.c.state.in_(QUEUED)),
            'active_jobs': count(jobs, jobs.c.state.in_(ACTIVE)),
            'queue_lag_seconds': max(0.0, now - oldest_due) if oldest_due is not None else 0.0,
            'stale_leases': count(jobs, jobs.c.state.in_(ACTIVE), or_(jobs.c.lease_expires <= now, jobs.c.lease_expires.is_(None))),
            'failed_last_hour': count(jobs, jobs.c.state == 'failed', jobs.c.updated >= now - 3600, jobs.c.updated <= now),
            'dispatcher_last_seen': float(last_seen) if last_seen is not None else None,
            'dispatcher_age_seconds': max(0.0, now - last_seen) if last_seen is not None else None,
            'outstanding_deletions': count(object_deletions),
            'deletion_lag_seconds': max(0.0, now - oldest_deletion) if oldest_deletion is not None else 0.0}

    def snapshot(self):
        try:
            with self.engine.connect() as connection:
                return self._snapshot(connection, self._now())
        except SQLAlchemyError:
            raise HTTPException(503, '운영 지표 저장소에 연결할 수 없습니다.') from None

    def _triggered(self, metrics):
        result = {}
        if metrics['queue_lag_seconds'] >= self.thresholds['queue_lag_seconds']:
            result['QUEUE_DELAY'] = metrics['due_jobs']
        if metrics['stale_leases'] >= self.thresholds['stale_leases']:
            result['STALE_LEASES'] = metrics['stale_leases']
        if metrics['failed_last_hour'] >= self.thresholds['failed_last_hour']:
            result['FAILED_JOBS'] = metrics['failed_last_hour']
        if metrics['due_jobs'] or metrics['active_jobs']:
            age = metrics['dispatcher_age_seconds']
            if age is None or age >= self.thresholds['dispatcher_age_seconds']:
                result['DISPATCHER_STALE'] = 1
        if (metrics['outstanding_deletions'] >= self.thresholds['outstanding_deletions'] or
                metrics['deletion_lag_seconds'] >= self.thresholds['deletion_lag_seconds']):
            result['DELETION_BACKLOG'] = metrics['outstanding_deletions']
        return result

    def collect(self):
        """Atomically deduplicate alerts across collectors; never deliver them."""
        try:
            with self.engine.begin() as connection:
                now = self._now()
                metrics = self._snapshot(connection, now)
                created = 0
                # Fixed sorted lock order avoids deadlock between collectors.
                for code, count in sorted(self._triggered(metrics).items()):
                    connection.execute(self._insert(cooldowns).values(code=code, next_emit_at=0)
                        .on_conflict_do_nothing(index_elements=[cooldowns.c.code]))
                    claimed = connection.execute(update(cooldowns).where(cooldowns.c.code == code,
                        cooldowns.c.next_emit_at <= now).values(next_emit_at=now + self.cooldown).returning(cooldowns.c.code)).first()
                    if not claimed:
                        continue
                    pending = connection.execute(select(outbox.c.id).where(outbox.c.code == code,
                        outbox.c.acknowledged_at.is_(None))).scalar_one_or_none()
                    if pending:
                        connection.execute(update(outbox).where(outbox.c.id == pending).values(count=count))
                    else:
                        connection.execute(insert(outbox).values(id=uuid.uuid4().hex, code=code, count=count, created=now))
                        created += 1
                connection.execute(delete(outbox).where(outbox.c.acknowledged_at <= now - self.acknowledged_retention))
                pending_count = int(connection.execute(select(func.count()).select_from(outbox).where(
                    outbox.c.acknowledged_at.is_(None))).scalar_one())
                return {'metrics': metrics, 'alerts_created': created, 'alerts_pending': pending_count}
        except SQLAlchemyError:
            raise HTTPException(503, '운영 알림 저장소에 연결할 수 없습니다.') from None

    def pending_alerts(self, limit=10):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Alert limit must be between 1 and 100')
        try:
            with self.engine.connect() as connection:
                return [dict(row) for row in connection.execute(select(outbox.c.id, outbox.c.code,
                    outbox.c.count, outbox.c.created).where(outbox.c.acknowledged_at.is_(None))
                    .order_by(outbox.c.created, outbox.c.id).limit(limit)).mappings()]
        except SQLAlchemyError:
            raise HTTPException(503, '전달 대기 알림을 확인할 수 없습니다.') from None

    def acknowledge(self, alert_id):
        if not isinstance(alert_id, str) or not re.fullmatch(r'[a-f0-9]{32}', alert_id):
            raise ValueError('Invalid alert ID')
        try:
            with self.engine.begin() as connection:
                row = connection.execute(update(outbox).where(outbox.c.id == alert_id).values(
                    acknowledged_at=func.coalesce(outbox.c.acknowledged_at, self._now())).returning(outbox.c.id)).first()
                return bool(row)
        except SQLAlchemyError:
            raise HTTPException(503, '운영 알림 전달 확인을 저장하지 못했습니다.') from None
