"""Shared provider balance and leased administrator notifications; no credentials."""
import math
import time
import uuid

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Float, Integer,
                        MetaData, String, Table, insert, select, update)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

metadata = MetaData()
balance = Table('replay_proxy_balance', metadata,
    Column('id', Integer, primary_key=True),
    Column('remaining_bytes', BigInteger, nullable=False),
    Column('observed_at', Float, nullable=False),
    Column('low_fired', Boolean, nullable=False),
    Column('critical_fired', Boolean, nullable=False),
    CheckConstraint('id = 1 AND remaining_bytes >= 0', name='ck_proxy_balance'))
alerts = Table('replay_proxy_alerts', metadata,
    Column('id', String(32), primary_key=True),
    Column('threshold_bytes', BigInteger, nullable=False),
    Column('remaining_bytes', BigInteger, nullable=False),
    Column('created_at', Float, nullable=False),
    Column('lease_token', String(32)),
    Column('lease_until', Float, nullable=False),
    Column('acknowledged_at', Float),
    CheckConstraint('remaining_bytes >= 0', name='ck_proxy_alert_remaining'))


class ProxyQuota:
    LOW, CRITICAL = 1_000_000_000, 250_000_000

    def __init__(self, engine, clock=time.time):
        self.engine, self.clock = engine, clock
        self._insert = pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert

    def migrate(self):
        metadata.create_all(self.engine)

    def observe(self, remaining_bytes, observed_at):
        if (type(remaining_bytes) is not int or not 0 <= remaining_bytes <= 10**15
                or type(observed_at) not in (int, float) or not math.isfinite(observed_at)
                or not self.clock() - 300 <= observed_at <= self.clock() + 5):
            raise ValueError('Invalid or stale provider balance')
        with self.engine.begin() as conn:
            conn.execute(self._insert(balance).values(id=1, remaining_bytes=remaining_bytes,
                observed_at=0, low_fired=False, critical_fired=False).on_conflict_do_nothing())
            # UPDATE both locks the shared row and rejects late poll responses.
            row = conn.execute(update(balance).where(balance.c.id == 1,
                balance.c.observed_at < observed_at).values(remaining_bytes=remaining_bytes,
                observed_at=observed_at).returning(balance)).mappings().first()
            if not row:
                return False
            conn.execute(update(alerts).where(alerts.c.acknowledged_at.is_(None)).values(
                remaining_bytes=remaining_bytes))
            flags = {key: row[key] for key in ('low_fired', 'critical_fired')}
            triggered = []
            for threshold, key in ((self.LOW, 'low_fired'), (self.CRITICAL, 'critical_fired')):
                if remaining_bytes > threshold * 1.1:
                    flags[key] = False
                    # A confirmed top-up retires obsolete pending notices.
                    conn.execute(update(alerts).where(alerts.c.threshold_bytes == threshold,
                        alerts.c.acknowledged_at.is_(None)).values(acknowledged_at=self.clock()))
                elif remaining_bytes <= threshold and not flags[key]:
                    flags[key] = True
                    triggered.append(threshold)
            if triggered:
                threshold = min(triggered)  # A jump below 250 MB needs one urgent notice.
                conn.execute(update(alerts).where(alerts.c.threshold_bytes > threshold,
                    alerts.c.acknowledged_at.is_(None)).values(acknowledged_at=self.clock()))
                conn.execute(insert(alerts).values(id=uuid.uuid4().hex, threshold_bytes=threshold,
                    remaining_bytes=remaining_bytes, created_at=self.clock(), lease_until=0))
            conn.execute(update(balance).where(balance.c.id == 1).values(**flags))
            return bool(triggered)

    def claim(self):
        with self.engine.begin() as conn:
            now = self.clock()
            candidate = conn.execute(select(alerts.c.id).where(alerts.c.acknowledged_at.is_(None),
                alerts.c.lease_until <= now).order_by(alerts.c.created_at).limit(1)).scalar_one_or_none()
            if not candidate:
                return None
            row = conn.execute(update(alerts).where(alerts.c.id == candidate,
                alerts.c.acknowledged_at.is_(None), alerts.c.lease_until <= now).values(
                lease_token=uuid.uuid4().hex, lease_until=now + 300).returning(alerts)).mappings().first()
            return dict(row) if row else None

    def acknowledge(self, id, token):
        with self.engine.begin() as conn:
            return conn.execute(update(alerts).where(alerts.c.id == id, alerts.c.lease_token == token,
                alerts.c.acknowledged_at.is_(None), alerts.c.lease_until > self.clock()).values(
                acknowledged_at=self.clock())).rowcount == 1
