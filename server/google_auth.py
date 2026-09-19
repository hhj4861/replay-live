"""Google identity verification and revocable PostgreSQL application sessions.

Google claims never assign application tenants or roles. The browser uses GIS
with a server-issued nonce; only hashes of challenges and opaque sessions persist.
"""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time

from fastapi import HTTPException
import jwt
from sqlalchemy import Boolean, Column, Float, ForeignKey, Index, MetaData, String, Table, delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from .auth import AuthConfig, JWTAuthenticator, Principal, _identifier

metadata = MetaData()
identities = Table('replay_google_identities', metadata,
    Column('subject', String(255), primary_key=True),
    Column('tenant_id', String(128), nullable=False, unique=True),
    Column('email', String(254), nullable=False),
    Column('email_authoritative', Boolean, nullable=False),
    Column('roles', String(500), nullable=False),
    Column('enabled', Boolean, nullable=False),
    Column('created_at', Float, nullable=False),
    Column('updated_at', Float, nullable=False))
challenges = Table('replay_google_challenges', metadata,
    Column('id', String(64), primary_key=True),
    Column('nonce_hash', String(64), nullable=False),
    Column('secret_hash', String(64), nullable=False),
    Column('client_hash', String(64), nullable=False),
    Column('created_at', Float, nullable=False),
    Column('expires_at', Float, nullable=False, index=True))
Index('ix_replay_google_challenges_client_created', challenges.c.client_hash, challenges.c.created_at)
families = Table('replay_google_session_families', metadata,
    Column('id', String(64), primary_key=True),
    Column('subject', String(255), ForeignKey('replay_google_identities.subject', ondelete='CASCADE'), nullable=False, index=True),
    Column('absolute_expires_at', Float, nullable=False, index=True),
    Column('revoked', Boolean, nullable=False))
sessions = Table('replay_google_sessions', metadata,
    Column('token_hash', String(64), primary_key=True),
    Column('family_id', String(64), ForeignKey('replay_google_session_families.id', ondelete='CASCADE'), nullable=False, index=True),
    Column('retired', Boolean, nullable=False),
    Column('subject', String(255), ForeignKey('replay_google_identities.subject', ondelete='CASCADE'), nullable=False, index=True),
    Column('created_at', Float, nullable=False),
    Column('expires_at', Float, nullable=False, index=True),
    Column('absolute_expires_at', Float, nullable=False))
member_audit = Table('replay_member_audit', metadata,
    Column('id', String(64), primary_key=True),
    Column('actor_hash', String(64), nullable=False),
    Column('member_id', String(128), nullable=False),
    Column('action', String(32), nullable=False),
    Column('target', String(24), nullable=False),
    Column('at', Float, nullable=False))
Index('ix_replay_member_audit_member_at', member_audit.c.member_id, member_audit.c.at)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _email(value):
    return isinstance(value, str) and len(value) <= 254 and re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value) is not None


@dataclass(frozen=True)
class GoogleAuthConfig:
    client_id: str
    allowed_emails: tuple[str, ...] = field(default=(), repr=False)
    allowed_subjects: tuple[str, ...] = field(default=(), repr=False)
    allow_signups: bool = False
    site_admin_emails: tuple[str, ...] = field(default=(), repr=False)
    mode: str = field(default='production', init=False)

    def __post_init__(self):
        if not isinstance(self.client_id, str) or not re.fullmatch(r'[0-9]+-[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', self.client_id):
            raise ValueError('A Google web client ID is required')
        if (type(self.allow_signups) is not bool or len(self.allowed_emails) > 1000
                or len(self.allowed_subjects) > 1000 or len(self.site_admin_emails) > 100):
            raise ValueError('Google account policy is invalid')
        if any(not _email(value) for value in (*self.allowed_emails, *self.site_admin_emails)) or any(
            not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', value) for value in self.allowed_subjects):
            raise ValueError('Google account allowlist is invalid')
        object.__setattr__(self, 'allowed_emails', tuple(value.lower() for value in self.allowed_emails))
        object.__setattr__(self, 'site_admin_emails', tuple(value.lower() for value in self.site_admin_emails))

    @classmethod
    def from_env(cls):
        signups = os.getenv('REPLAY_GOOGLE_ALLOW_SIGNUPS', '0')
        if signups not in ('0', '1'):
            raise ValueError('REPLAY_GOOGLE_ALLOW_SIGNUPS must be 0 or 1')
        return cls(client_id=os.getenv('REPLAY_GOOGLE_CLIENT_ID', ''),
            allowed_emails=tuple(value.strip() for value in os.getenv('REPLAY_GOOGLE_ALLOWED_EMAILS', '').split(',') if value.strip()),
            allowed_subjects=tuple(value.strip() for value in os.getenv('REPLAY_GOOGLE_ALLOWED_SUBJECTS', '').split(',') if value.strip()),
            allow_signups=signups == '1',
            site_admin_emails=tuple(value.strip() for value in os.getenv('REPLAY_SITE_ADMIN_EMAILS', '').split(',') if value.strip()))


def _unauthorized():
    return HTTPException(401, 'Google 로그인을 다시 진행하세요.')


def discard_restored_sessions(connection):
    """Called only by explicit restore, inside its verification transaction."""
    result = {'google_sessions_discarded': connection.execute(delete(sessions)).rowcount,
              'google_challenges_discarded': connection.execute(delete(challenges)).rowcount}
    connection.execute(delete(families))
    return result


class GoogleAuthenticator:
    def __init__(self, engine, config: GoogleAuthConfig, *, mode='production', http_client=None, clock=time.time):
        if engine.dialect.name != 'postgresql' and not (mode in ('development', 'test') and engine.dialect.name == 'sqlite'):
            raise ValueError('Google sessions require PostgreSQL outside explicit development/test mode')
        if mode not in ('production', 'development', 'test'):
            raise ValueError('Invalid Google authentication mode')
        self.engine, self.config, self.clock = engine, config, clock
        self._sqlite = engine.dialect.name == 'sqlite'
        self._lock = threading.RLock()
        self._insert = sqlite_insert if self._sqlite else pg_insert
        self._keys = JWTAuthenticator(AuthConfig(issuer='https://accounts.google.com', audience=config.client_id,
            jwks_url='https://www.googleapis.com/oauth2/v3/certs', algorithms=('RS256',)), http_client=http_client)

    def migrate(self):
        """Explicit migration helper, never called from authentication/routes."""
        metadata.create_all(self.engine)

    async def close(self):
        await self._keys.close()

    @contextmanager
    def _transaction(self):
        # SQLite is fixture-only; PostgreSQL uses row locks/atomic mutations.
        with self._lock:
            try:
                with self.engine.begin() as connection:
                    yield connection
            except SQLAlchemyError:
                raise HTTPException(503, '로그인 저장소에 연결할 수 없습니다. 잠시 후 다시 시도하세요.') from None

    def _now(self, connection):
        return self.clock() if self._sqlite else float(connection.execute(select(func.extract('epoch', func.clock_timestamp()))).scalar_one())

    def _allowed(self, subject, email, authoritative):
        return (self.config.allow_signups or subject in self.config.allowed_subjects
                or authoritative and email.lower() in self.config.allowed_emails)

    def is_site_admin(self, account):
        # Google claims and tenant-level admin roles never grant site authority.
        return (account['email_authoritative'] and account['email'].lower() in self.config.site_admin_emails
                and self._allowed(account['subject'], account['email'], account['email_authoritative']))

    def account_roles(self, account):
        try:
            roles = json.loads(account['roles'])
            if (not isinstance(roles, list) or any(role not in ('operator', 'admin', 'viewer') for role in roles)
                    or not _identifier(account['tenant_id'])):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(403, '계정 권한을 확인할 수 없습니다.') from None
        return tuple(roles) + (('site_admin',) if self.is_site_admin(account) else ())

    def challenge(self, client_key):
        if not isinstance(client_key, str) or not 1 <= len(client_key) <= 512:
            raise HTTPException(400, '로그인 요청을 확인하세요.')
        nonce, secret, identifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        client_hash = _hash(client_key)
        with self._transaction() as connection:
            now = self._now(connection)
            if not self._sqlite:
                connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': int(client_hash[:15], 16)})
            connection.execute(delete(challenges).where(challenges.c.expires_at <= now))
            attempts = connection.execute(select(func.count()).select_from(challenges).where(
                challenges.c.client_hash == client_hash, challenges.c.created_at > now - 60)).scalar_one()
            if attempts >= 20:
                raise HTTPException(429, '로그인 요청이 많습니다. 잠시 후 다시 시도하세요.', headers={'Retry-After': '60'})
            connection.execute(insert(challenges).values(id=identifier, nonce_hash=_hash(nonce), secret_hash=_hash(secret),
                client_hash=client_hash, created_at=now, expires_at=now + 300))
        return {'challenge_id': identifier, 'nonce': nonce, 'challenge_secret': secret, 'expires_at': now + 300}

    def _challenge(self, identifier, secret):
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32}', identifier) or not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', secret):
            raise _unauthorized()
        with self._transaction() as connection:
            row = connection.execute(select(challenges).where(challenges.c.id == identifier,
                challenges.c.expires_at > self._now(connection))).mappings().first()
            if row is None or not hmac.compare_digest(row['secret_hash'], _hash(secret)):
                raise _unauthorized()
            return dict(row)

    async def _verify(self, credential, nonce_hash):
        if not isinstance(credential, str) or not 1 <= len(credential) <= 12000 or any(c.isspace() for c in credential):
            raise _unauthorized()
        try:
            header = jwt.get_unverified_header(credential)
            if header.get('alg') != 'RS256' or not isinstance(header.get('kid'), str) or not 1 <= len(header['kid']) <= 256 or any(
                name in header for name in ('jku', 'x5u', 'jwk', 'crit')):
                raise ValueError()
            key = await self._keys._key(header['kid'], 'RS256')
            claims = jwt.decode(credential, key, algorithms=['RS256'], audience=self.config.client_id,
                issuer=['accounts.google.com', 'https://accounts.google.com'], leeway=30,
                options={'require': ['sub', 'iss', 'aud', 'iat', 'exp', 'nonce', 'email', 'email_verified']})
            now = self.clock()
            if any(isinstance(claims[name], bool) or not isinstance(claims[name], (int, float)) or not math.isfinite(claims[name]) for name in ('iat', 'exp')):
                raise ValueError()
            if claims['exp'] <= now or claims['iat'] > now + 30 or not 0 < claims['exp'] - claims['iat'] <= 7200:
                raise ValueError()
            if claims['aud'] != self.config.client_id or claims.get('azp', self.config.client_id) != self.config.client_id:
                raise ValueError()
            if not isinstance(claims['sub'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}', claims['sub']):
                raise ValueError()
            if claims['email_verified'] is not True or not _email(claims['email']):
                raise ValueError()
            if not isinstance(claims['nonce'], str) or len(claims['nonce']) > 256 or not hmac.compare_digest(_hash(claims['nonce']), nonce_hash):
                raise ValueError()
            hosted = claims.get('hd')
            if hosted is not None and (not isinstance(hosted, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}', hosted)):
                raise ValueError()
            claims['email'] = claims['email'].lower()
            claims['_authoritative'] = claims['email'].endswith('@gmail.com') or bool(hosted)
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
            raise _unauthorized() from None

    def _mint(self, connection, subject, now, absolute, family_id=None):
        if family_id is None:
            family_id = secrets.token_hex(32)
            connection.execute(insert(families).values(id=family_id, subject=subject, absolute_expires_at=absolute, revoked=False))
        token = secrets.token_urlsafe(32)
        expires = min(now + 3600, absolute)
        connection.execute(insert(sessions).values(token_hash=_hash(token), subject=subject, family_id=family_id, retired=False,
            created_at=now, expires_at=expires, absolute_expires_at=absolute))
        return {'token': token, 'expires_at': expires, 'absolute_expires_at': absolute}

    def _exchange(self, claims, challenge):
        with self._transaction() as connection:
            now = self._now(connection)
            consumed = connection.execute(delete(challenges).where(challenges.c.id == challenge['id'],
                challenges.c.secret_hash == challenge['secret_hash'], challenges.c.nonce_hash == challenge['nonce_hash'],
                challenges.c.expires_at > now).returning(challenges.c.id)).first()
            if not consumed:
                raise _unauthorized()
            allowed = self._allowed(claims['sub'], claims['email'], claims['_authoritative'])
            if not allowed:
                # Commit nonce consumption even for a valid but disallowed account.
                failure = HTTPException(403, '이 Google 계정은 접속이 허용되지 않았습니다. 관리자에게 문의하세요.')
                result = None
            else:
                tenant = 'google-' + _hash(claims['sub'])[:40]
                connection.execute(self._insert(identities).values(subject=claims['sub'], tenant_id=tenant,
                    email=claims['email'], email_authoritative=claims['_authoritative'], roles='["operator"]',
                    enabled=True, created_at=now, updated_at=now).on_conflict_do_nothing(index_elements=[identities.c.subject]))
                account = connection.execute(select(identities).where(identities.c.subject == claims['sub']).with_for_update()).mappings().one()
                failure = None if account['enabled'] else HTTPException(403, '사용이 중지된 계정입니다. 관리자에게 문의하세요.')
                result = None
                if not failure:
                    connection.execute(update(identities).where(identities.c.subject == claims['sub']).values(
                        email=claims['email'], email_authoritative=claims['_authoritative'], updated_at=now))
                    # Bound active sessions per account without invalidating active work.
                    count = connection.execute(select(func.count()).select_from(sessions.join(families)).where(
                        sessions.c.subject == claims['sub'], sessions.c.expires_at > now, sessions.c.retired.is_(False),
                        families.c.revoked.is_(False), families.c.absolute_expires_at > now)).scalar_one()
                    if count >= 20:
                        failure = HTTPException(429, '활성 로그인 수가 많습니다. 다른 기기에서 로그아웃한 뒤 다시 시도하세요.')
                    else:
                        result = self._mint(connection, claims['sub'], now, now + 86400)
        if failure:
            raise failure
        return result

    async def exchange(self, credential, challenge_id, challenge_secret):
        challenge = await asyncio.to_thread(self._challenge, challenge_id, challenge_secret)
        claims = await self._verify(credential, challenge['nonce_hash'])
        return await asyncio.to_thread(self._exchange, claims, challenge)

    def _token(self, authorization):
        if not isinstance(authorization, str) or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}', authorization):
            raise _unauthorized()
        return _hash(authorization[7:])

    def _session(self, connection, token_hash, now):
        reference = connection.execute(select(sessions.c.family_id, sessions.c.subject)
            .where(sessions.c.token_hash == token_hash)).first()
        if reference is None:
            raise _unauthorized()
        # Match administrative suspension's identity-before-family lock order.
        connection.execute(select(identities.c.subject).where(identities.c.subject == reference.subject).with_for_update()).first()
        family_id = reference.family_id
        family = connection.execute(select(families).where(families.c.id == family_id).with_for_update()).mappings().first()
        if family is None or family['revoked'] or family['absolute_expires_at'] <= now:
            raise _unauthorized()
        row = connection.execute(select(sessions, identities.c.tenant_id, identities.c.email,
            identities.c.email_authoritative, identities.c.roles, identities.c.enabled).join(identities).where(
                sessions.c.token_hash == token_hash, sessions.c.expires_at > now, sessions.c.retired.is_(False),
                sessions.c.absolute_expires_at > now).with_for_update()).mappings().first()
        if row is None or row['subject'] != family['subject']:
            raise _unauthorized()
        if not row['enabled'] or not self._allowed(row['subject'], row['email'], row['email_authoritative']):
            raise HTTPException(403, '이 계정은 현재 접속이 허용되지 않습니다.')
        return row, self.account_roles(row)

    def _authenticate(self, authorization, requested_tenant):
        token_hash = self._token(authorization)
        with self._transaction() as connection:
            row, roles = self._session(connection, token_hash, self._now(connection))
            if requested_tenant and requested_tenant != row['tenant_id']:
                raise HTTPException(403, '다른 조직의 자료에 접근할 수 없습니다.')
            return Principal('google:' + row['subject'], row['tenant_id'], roles,
                             token_id=token_hash, expires_at=row['expires_at'])

    async def authenticate(self, authorization, requested_tenant=None):
        return await asyncio.to_thread(self._authenticate, authorization, requested_tenant)

    def refresh(self, authorization):
        token_hash = self._token(authorization)
        with self._transaction() as connection:
            now = self._now(connection)
            row, _ = self._session(connection, token_hash, now)
            # Retain retired hashes until the family's absolute deadline so an
            # old-token logout racing this rotation still revokes this device.
            count = connection.execute(select(func.count()).select_from(sessions).where(sessions.c.family_id == row['family_id'])).scalar_one()
            if count >= 256:
                raise _unauthorized()
            consumed = connection.execute(update(sessions).where(sessions.c.token_hash == token_hash,
                sessions.c.expires_at > now, sessions.c.retired.is_(False)).values(retired=True).returning(sessions.c.token_hash)).first()
            if not consumed:
                raise _unauthorized()
            return self._mint(connection, row['subject'], now, row['absolute_expires_at'], row['family_id'])

    def logout(self, authorization):
        token_hash = self._token(authorization)
        with self._transaction() as connection:
            family_id = connection.execute(select(sessions.c.family_id).where(sessions.c.token_hash == token_hash)).scalar_one_or_none()
            family = connection.execute(select(families.c.id).where(families.c.id == family_id).with_for_update()).first()
            if family:
                connection.execute(update(families).where(families.c.id == family_id).values(revoked=True))

    def purge_expired(self):
        with self._transaction() as connection:
            now = self._now(connection)
            expired = list(connection.execute(select(families.c.id).where(families.c.absolute_expires_at <= now)
                .order_by(families.c.id).limit(1000).with_for_update()).scalars())
            result = {'google_challenges': connection.execute(delete(challenges).where(challenges.c.expires_at <= now)).rowcount,
                      'google_sessions': connection.execute(delete(sessions).where(sessions.c.family_id.in_(expired))).rowcount}
            connection.execute(delete(families).where(families.c.id.in_(expired)))
            return result
