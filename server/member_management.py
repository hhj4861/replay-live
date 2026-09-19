"""Google member administration with no access to connection plaintext.

Authority is derived from explicit server configuration and rechecked in every
transaction. Tenant-level admins cannot use these cross-tenant operations.
"""
from contextlib import contextmanager
import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from .google_auth import identities, families, sessions, member_audit
from .repository import jobs, NONTERMINAL
from .stream_connections import connections
from .stream_targets import LIVE_TARGETS


class MemberManagement:
    def __init__(self, auth, repository):
        self.auth, self.repo = auth, repository

    @contextmanager
    def _transaction(self, *, mutation=False):
        try:
            with self.repo._transaction() as connection:
                if mutation:
                    self.repo._gate(connection)
                yield connection
        except SQLAlchemyError:
            raise HTTPException(503, '회원 관리 저장소를 사용할 수 없습니다. 잠시 후 다시 시도하세요.') from None

    def _actor(self, connection, principal, *, lock=False):
        # has_role('site_admin') would also accept an ordinary tenant admin.
        if 'site_admin' not in principal.roles or not principal.subject.startswith('google:'):
            raise HTTPException(403, '사이트 관리자 권한이 필요합니다.')
        statement = select(identities).where(
            identities.c.subject == principal.subject.removeprefix('google:'),
            identities.c.tenant_id == principal.tenant_id)
        row = connection.execute(statement.with_for_update() if lock else statement).mappings().first()
        if row is None or not row['enabled'] or not self.auth.is_site_admin(row):
            raise HTTPException(403, '사이트 관리자 권한이 필요합니다.')
        return row

    def _member(self, connection, member_id, *, lock=False):
        statement = select(identities).where(identities.c.tenant_id == member_id)
        row = connection.execute(statement.with_for_update() if lock else statement).mappings().first()
        if row is None:
            raise HTTPException(404, '회원을 찾을 수 없습니다.')
        return row

    def check_admission(self, connection, tenant_id):
        """Runs after repository admission lock, before any replay or new job."""
        row = connection.execute(select(identities).where(identities.c.tenant_id == tenant_id)
            .with_for_update()).mappings().first()
        # Explicit non-Google service tenants retain their existing behavior.
        if row is None and not tenant_id.startswith('google-'):
            return
        if row is None or not row['enabled'] or not self.auth._allowed(
                row['subject'], row['email'], row['email_authoritative']):
            raise HTTPException(403, '이 계정은 현재 접속이 허용되지 않습니다.')

    def profile(self, principal):
        with self._transaction() as connection:
            row = self._member(connection, principal.tenant_id)
            if principal.subject != 'google:' + row['subject'] or not row['enabled']:
                raise HTTPException(403, '이 계정은 현재 접속이 허용되지 않습니다.')
            return {'id': row['tenant_id'], 'email': row['email'], 'enabled': bool(row['enabled']),
                    'created_at': row['created_at'], 'updated_at': row['updated_at']}

    @staticmethod
    def _owner(row):
        return {'tenant_id': row['tenant_id'],
                'subject_hash': hashlib.sha256(('google:' + row['subject']).encode()).hexdigest()}

    def _summaries(self, connection, rows, now):
        subjects, tenant_ids = [row['subject'] for row in rows], [row['tenant_id'] for row in rows]
        active = dict(connection.execute(select(sessions.c.subject, func.count(func.distinct(sessions.c.family_id)))
            .select_from(sessions.join(families)).where(sessions.c.subject.in_(subjects),
                sessions.c.retired.is_(False), sessions.c.expires_at > now,
                families.c.revoked.is_(False), families.c.absolute_expires_at > now)
            .group_by(sessions.c.subject)).all()) if rows else {}
        stored = connection.execute(select(connections.c.tenant_id, connections.c.subject_hash, func.count())
            .where(connections.c.tenant_id.in_(tenant_ids)).group_by(connections.c.tenant_id,
                connections.c.subject_hash)).all() if rows else []
        counts = {(tenant, owner): count for tenant, owner, count in stored}
        result = []
        for row in rows:
            owner = self._owner(row)
            result.append({'id': row['tenant_id'], 'email': row['email'], 'enabled': bool(row['enabled']),
                'roles': self.auth.account_roles(row), 'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'active_session_count': active.get(row['subject'], 0),
                'stream_connection_count': counts.get((owner['tenant_id'], owner['subject_hash']), 0)})
        return result

    def list(self, principal, *, q='', offset=0, limit=25):
        if not isinstance(q, str) or len(q) > 254 or offset < 0 or not 1 <= limit <= 100:
            raise HTTPException(400, '회원 검색 조건을 확인하세요.')
        with self._transaction() as connection:
            self._actor(connection, principal)
            condition = func.lower(identities.c.email).contains(q.strip().lower(), autoescape=True)
            total = connection.execute(select(func.count()).select_from(identities).where(condition)).scalar_one()
            rows = connection.execute(select(identities).where(condition)
                .order_by(identities.c.created_at.desc(), identities.c.tenant_id).offset(offset).limit(limit)).mappings().all()
            return {'items': self._summaries(connection, rows, self.auth._now(connection)),
                    'total': total, 'offset': offset, 'limit': limit}

    def _detail(self, connection, row, now):
        result = self._summaries(connection, [row], now)[0]
        owner = self._owner(row)
        # Select metadata columns only: never load ciphertext, decrypt, or call use().
        result['connections'] = [dict(value, has_stream_key=True) for value in connection.execute(
            select(connections.c.target, connections.c.updated_at).where(
                connections.c.tenant_id == owner['tenant_id'], connections.c.subject_hash == owner['subject_hash'])
            .order_by(connections.c.target)).mappings()]
        result['active_jobs_count'] = connection.execute(select(func.count()).select_from(jobs).where(
            jobs.c.tenant_id == row['tenant_id'], jobs.c.state.in_(NONTERMINAL))).scalar_one()
        return result

    def detail(self, principal, member_id):
        with self._transaction() as connection:
            self._actor(connection, principal)
            return self._detail(connection, self._member(connection, member_id), self.auth._now(connection))

    @staticmethod
    def _audit(connection, principal, member_id, action, now, target=''):
        connection.execute(insert(member_audit).values(id=uuid.uuid4().hex,
            actor_hash=hashlib.sha256(principal.subject.encode()).hexdigest(), member_id=member_id,
            action=action, target=target, at=now))

    def set_enabled(self, principal, member_id, enabled):
        if type(enabled) is not bool:
            raise HTTPException(400, '회원 상태를 확인하세요.')
        with self._transaction(mutation=True) as connection:
            actor = self._actor(connection, principal, lock=True)
            member = self._member(connection, member_id, lock=True)
            now = self.auth._now(connection)
            if not enabled:
                if actor['subject'] == member['subject']:
                    raise HTTPException(409, '본인 관리자 계정은 정지할 수 없습니다.')
                if self.auth.is_site_admin(member):
                    admins = connection.execute(select(identities).where(identities.c.enabled.is_(True),
                        identities.c.email_authoritative.is_(True),
                        identities.c.email.in_(self.auth.config.site_admin_emails)).with_for_update()).mappings().all()
                    if sum(self.auth.is_site_admin(row) for row in admins) <= 1:
                        raise HTTPException(409, '마지막 사이트 관리자는 정지할 수 없습니다.')
                connection.execute(update(families).where(families.c.subject == member['subject']).values(revoked=True))
                self.repo.cancel_member_jobs(connection, member['tenant_id'], now)
            connection.execute(update(identities).where(identities.c.subject == member['subject'])
                .values(enabled=enabled, updated_at=now))
            self._audit(connection, principal, member_id, 'member_enabled' if enabled else 'member_suspended', now)
            return self._detail(connection, {**member, 'enabled': enabled, 'updated_at': now}, now)

    def revoke_sessions(self, principal, member_id):
        with self._transaction(mutation=True) as connection:
            self._actor(connection, principal, lock=True)
            member = self._member(connection, member_id, lock=True)
            connection.execute(update(families).where(families.c.subject == member['subject']).values(revoked=True))
            self._audit(connection, principal, member_id, 'sessions_revoked', self.auth._now(connection))
            return {'revoked': True}

    def delete_connection(self, principal, member_id, target):
        if target not in LIVE_TARGETS:
            raise HTTPException(400, '연결할 플랫폼을 확인하세요.')
        with self._transaction(mutation=True) as connection:
            self._actor(connection, principal, lock=True)
            member = self._member(connection, member_id, lock=True)
            owner = self._owner(member)
            connection.execute(delete(connections).where(connections.c.tenant_id == owner['tenant_id'],
                connections.c.subject_hash == owner['subject_hash'], connections.c.target == target))
            self._audit(connection, principal, member_id, 'connection_deleted', self.auth._now(connection), target)
