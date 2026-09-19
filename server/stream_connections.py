"""Account-owned, encrypted live destinations; plaintext is returned only by use()."""
import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy import CheckConstraint, Column, Float, MetaData, String, Table, Text, delete, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from .auth import Principal
from .secrets import KeyProvider, SecretError
from .stream_targets import LIVE_TARGETS, StreamTargetError, validate_destination
from .watch_links import normalize_watch_url


metadata = MetaData()
connections = Table(
    'replay_stream_connections', metadata,
    Column('tenant_id', String(200), primary_key=True),
    Column('subject_hash', String(64), primary_key=True),
    Column('target', String(24), primary_key=True),
    Column('secret_ciphertext', Text, nullable=False),
    Column('updated_at', Float, nullable=False),
    CheckConstraint("target IN ('youtube','twitch','facebook','instagram','tiktok','naver','chzzk','kick','custom')",
                    name='replay_stream_connections_live_target'),
)


class StreamConnections:
    def __init__(self, engine, keys: KeyProvider, *, mode='production'):
        if mode not in ('production', 'development', 'test'):
            raise ValueError('Invalid stream connections mode')
        if engine.dialect.name != 'postgresql' and not (mode in ('development', 'test') and engine.dialect.name == 'sqlite'):
            raise ValueError('Production stream connections require PostgreSQL')
        self.engine, self.keys = engine, keys
        self._insert = postgres_insert if engine.dialect.name == 'postgresql' else sqlite_insert

    def migrate(self):
        """Explicit local/deployment setup only; never invoked while handling a request."""
        metadata.create_all(self.engine)

    @staticmethod
    def _owner(principal: Principal):
        if not principal.has_role('operator'):
            raise HTTPException(403, '방송 운영 권한이 필요합니다.')
        if (not isinstance(principal.tenant_id, str) or not 1 <= len(principal.tenant_id) <= 200
                or not isinstance(principal.subject, str) or not 1 <= len(principal.subject) <= 4096):
            raise HTTPException(401, '저장한 연결 정보를 사용할 계정을 확인할 수 없습니다.')
        return {'tenant_id': principal.tenant_id,
                'subject_hash': hashlib.sha256(principal.subject.encode()).hexdigest()}

    @staticmethod
    def _target(target):
        if not isinstance(target, str) or target not in LIVE_TARGETS:
            raise StreamTargetError()
        return target

    @staticmethod
    def _context(owner, target):
        return {**owner, 'target': target, 'purpose': 'stream-connection-v1'}

    @staticmethod
    def _where(owner, target=None):
        scope = ((connections.c.tenant_id == owner['tenant_id'])
                 & (connections.c.subject_hash == owner['subject_hash']))
        return scope if target is None else scope & (connections.c.target == target)

    def _now(self, connection):
        if self.engine.dialect.name == 'postgresql':
            return float(connection.execute(select(func.extract('epoch', func.clock_timestamp()))).scalar_one())
        return time.time()

    def _decode(self, row, owner):
        try:
            payload = json.loads(self.keys.decrypt(row['secret_ciphertext'], self._context(owner, row['target'])))
            if not isinstance(payload, dict) or payload.get('target') != row['target']:
                raise ValueError()
            destination = validate_destination(row['target'], payload.get('server_url'), payload.get('stream_key'))
            if destination is None:
                raise ValueError()
            return {'target': row['target'], 'server_url': destination['server_url'],
                    'stream_key': destination['stream_key'],
                    'channel_url': normalize_watch_url(row['target'], payload.get('channel_url', ''), 'channel')}
        except (SecretError, ValueError, TypeError, KeyError):
            raise HTTPException(503, '저장한 연결 정보를 불러오지 못했습니다. 다시 저장해 주세요.') from None

    def list(self, principal: Principal):
        owner = self._owner(principal)
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(select(connections).where(self._where(owner))
                                          .order_by(connections.c.target)).mappings().all()
            result = []
            for row in rows:
                destination = self._decode(row, owner)
                result.append({'target': row['target'], 'server_url': destination['server_url'],
                               'channel_url': destination['channel_url'],
                               'updated_at': row['updated_at'], 'has_stream_key': True})
            return result
        except SQLAlchemyError:
            raise HTTPException(503, '연결 정보 저장소를 사용할 수 없습니다. 잠시 후 다시 시도하세요.') from None

    def save(self, principal: Principal, target, *, server_url, stream_key, channel_url=''):
        owner, target = self._owner(principal), self._target(target)
        destination = validate_destination(target, server_url, stream_key)
        payload = {name: destination[name] for name in ('target', 'server_url', 'stream_key')}
        payload['channel_url'] = normalize_watch_url(target, channel_url, 'channel')
        try:
            ciphertext = self.keys.encrypt(json.dumps(payload, separators=(',', ':')), self._context(owner, target))
            with self.engine.begin() as connection:
                updated = self._now(connection)
                statement = self._insert(connections).values(**owner, target=target,
                                                             secret_ciphertext=ciphertext, updated_at=updated)
                connection.execute(statement.on_conflict_do_update(
                    index_elements=[connections.c.tenant_id, connections.c.subject_hash, connections.c.target],
                    set_={'secret_ciphertext': ciphertext, 'updated_at': updated}))
            return {'target': target, 'server_url': destination['server_url'],
                    'channel_url': payload['channel_url'],
                    'updated_at': updated, 'has_stream_key': True}
        except (SQLAlchemyError, SecretError):
            raise HTTPException(503, '연결 정보를 저장하지 못했습니다. 잠시 후 다시 시도하세요.') from None

    def use(self, principal: Principal, target):
        owner, target = self._owner(principal), self._target(target)
        try:
            with self.engine.connect() as connection:
                row = connection.execute(select(connections).where(self._where(owner, target))).mappings().first()
            if row is None:
                raise HTTPException(404, '저장한 연결 정보가 없습니다.')
            return self._decode(row, owner)
        except SQLAlchemyError:
            raise HTTPException(503, '연결 정보 저장소를 사용할 수 없습니다. 잠시 후 다시 시도하세요.') from None

    def delete(self, principal: Principal, target):
        owner, target = self._owner(principal), self._target(target)
        try:
            with self.engine.begin() as connection:
                connection.execute(delete(connections).where(self._where(owner, target)))
        except SQLAlchemyError:
            raise HTTPException(503, '연결 정보를 삭제하지 못했습니다. 잠시 후 다시 시도하세요.') from None
