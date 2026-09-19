"""Shared PostgreSQL access budgets and expiration-bounded token revocation."""
import hashlib
import math
import re
import time

from fastapi import HTTPException
from sqlalchemy import Column, Float, Integer, MetaData, String, Table, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from .auth import Principal

metadata = MetaData()
counters = Table('replay_access_counters', metadata,
                 Column('bucket', String(64), primary_key=True),
                 Column('count', Integer, nullable=False),
                 Column('reset_at', Float, nullable=False, index=True))
revocations = Table('replay_revoked_tokens', metadata,
                    Column('token_hash', String(64), primary_key=True),
                    Column('expires_at', Float, nullable=False, index=True))


class AccessPolicy:
    def __init__(self, engine, *, mode='production', user_limit=240, tenant_limit=1200,
                 window=60.0, limits=None, max_token_lifetime=86400):
        if mode not in ('production', 'development', 'test'):
            raise ValueError('Invalid access policy mode')
        if engine.dialect.name != 'postgresql' and not (mode in ('development', 'test') and engine.dialect.name == 'sqlite'):
            raise ValueError('Production access policy requires PostgreSQL')
        self.engine = engine
        self.limits = {'api': (user_limit, tenant_limit), 'upload': (20, 100), 'broadcast': (30, 150)}
        self.limits.update(limits or {})
        if not math.isfinite(window) or window <= 0 or not 0 < max_token_lifetime <= 86400:
            raise ValueError('Invalid access policy lifetime')
        if any(not isinstance(pair, (tuple, list)) or len(pair) != 2 or any(not isinstance(value, int) or value < 1 for value in pair)
               for pair in self.limits.values()):
            raise ValueError('Access limits require positive user and tenant budgets')
        self.window, self.max_token_lifetime = window, max_token_lifetime
        self._insert = postgres_insert if engine.dialect.name == 'postgresql' else sqlite_insert

    def migrate(self):
        """Run explicitly from deployment migrations, never on an API request."""
        metadata.create_all(self.engine)

    def _now(self, connection):
        if self.engine.dialect.name == 'postgresql':
            return float(connection.execute(select(func.extract('epoch', func.clock_timestamp()))).scalar_one())
        return time.time()

    def _token_hash(self, principal, now):
        if not isinstance(principal.token_id, str) or not re.fullmatch(r'[a-f0-9]{64}', principal.token_id):
            raise HTTPException(401, '폐기 여부를 확인할 수 있는 인증 토큰이 필요합니다.')
        expiry = principal.expires_at
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= now:
            raise HTTPException(401, '사용자 인증이 만료되었습니다.')
        if expiry > now + self.max_token_lifetime + 60:
            raise HTTPException(401, '인증 토큰 유효 기간이 허용 범위를 초과했습니다.')
        return hashlib.sha256(principal.token_id.encode()).hexdigest()

    def _consume(self, connection, bucket, budget, now):
        fresh = counters.c.reset_at <= now
        statement = self._insert(counters).values(bucket=bucket, count=1, reset_at=now + self.window)
        statement = statement.on_conflict_do_update(index_elements=[counters.c.bucket], set_={
            'count': case((fresh, 1), else_=counters.c.count + 1),
            'reset_at': case((fresh, now + self.window), else_=counters.c.reset_at),
        }).returning(counters.c.count, counters.c.reset_at)
        count, reset_at = connection.execute(statement).one()
        if count > budget:
            raise HTTPException(429, '요청 한도를 초과했습니다. 잠시 후 다시 시도하세요.',
                                headers={'Retry-After': str(max(1, math.ceil(reset_at - now)))})

    def check(self, principal: Principal, action='api'):
        if action not in self.limits:
            raise ValueError('Unknown access policy action')
        try:
            with self.engine.begin() as connection:
                now = self._now(connection)
                token_hash = self._token_hash(principal, now)
                revoked = connection.execute(select(revocations.c.token_hash).where(
                    revocations.c.token_hash == token_hash, revocations.c.expires_at > now)).first()
                if revoked:
                    raise HTTPException(401, '폐기된 인증 토큰입니다. 다시 로그인하세요.')
                user_budget, tenant_budget = self.limits[action]
                # A consistent tenant-before-user lock order prevents cross-user deadlocks.
                tenant = hashlib.sha256(('tenant\x1f' + principal.tenant_id + '\x1f' + action).encode()).hexdigest()
                user = hashlib.sha256(('user\x1f' + principal.tenant_id + '\x1f' + principal.subject + '\x1f' + action).encode()).hexdigest()
                self._consume(connection, tenant, tenant_budget, now)
                self._consume(connection, user, user_budget, now)
        except SQLAlchemyError:
            raise HTTPException(503, '접근 권한 저장소에 연결할 수 없습니다. 잠시 후 다시 시도하세요.') from None

    def revoke(self, principal: Principal):
        try:
            with self.engine.begin() as connection:
                now = self._now(connection)
                token_hash = self._token_hash(principal, now)
                statement = self._insert(revocations).values(token_hash=token_hash, expires_at=principal.expires_at)
                connection.execute(statement.on_conflict_do_update(index_elements=[revocations.c.token_hash],
                    set_={'expires_at': case((revocations.c.expires_at > principal.expires_at, revocations.c.expires_at),
                                            else_=principal.expires_at)}))
        except SQLAlchemyError:
            raise HTTPException(503, '인증 토큰을 폐기하지 못했습니다. 잠시 후 다시 시도하세요.') from None

    def purge_expired(self):
        try:
            with self.engine.begin() as connection:
                now = self._now(connection)
                revoked = connection.execute(delete(revocations).where(revocations.c.expires_at <= now)).rowcount
                budgets = connection.execute(delete(counters).where(counters.c.reset_at <= now)).rowcount
                return {'expired_revocations': revoked, 'expired_access_counters': budgets}
        except SQLAlchemyError:
            raise HTTPException(503, '만료된 접근 기록을 정리하지 못했습니다.') from None
