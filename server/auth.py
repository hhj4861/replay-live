"""Validate tenant-scoped access tokens against a fixed, trusted OIDC issuer."""
import asyncio
from dataclasses import dataclass, field
import hmac
import hashlib
import json
import math
import os
import re
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
import httpx
import jwt


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: tuple[str, ...] = ()
    token_id: str | None = field(default=None, repr=False)
    expires_at: float | None = None

    def has_role(self, role: str) -> bool:
        return role in self.roles or 'admin' in self.roles


@dataclass(frozen=True)
class AuthConfig:
    mode: str = 'production'
    issuer: str = ''
    audience: str = ''
    jwks_url: str = ''
    tenant_claim: str = 'tenant_id'
    roles_claim: str = 'roles'
    membership_claim: str | None = None
    algorithms: tuple[str, ...] = ('RS256', 'ES256')
    dev_token: str | None = field(default=None, repr=False)
    dev_subject: str = 'developer'
    dev_tenant_id: str = 'development'
    jwks_ttl: float = 300
    request_timeout: float = 5
    leeway: float = 30
    max_token_lifetime: float = 86400

    def __post_init__(self):
        if self.mode not in ('production', 'development'):
            raise ValueError('Authentication mode must be production or development')
        if self.mode == 'development':
            if not self.dev_token or len(self.dev_token) < 16:
                raise ValueError('Explicit development authentication requires a token of at least 16 characters')
            if not _identifier(self.dev_tenant_id) or not self.dev_subject:
                raise ValueError('Development principal is invalid')
        else:
            if self.dev_token:
                raise ValueError('Development credentials are forbidden in production')
            for value in (self.issuer, self.jwks_url):
                parsed = urlsplit(value)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError('Production issuer and JWKS URL must be fixed HTTPS URLs')
            if not self.audience:
                raise ValueError('Production authentication requires an API audience')
        if not self.tenant_claim or not self.roles_claim:
            raise ValueError('Tenant and roles claim names are required')
        if not self.algorithms or not set(self.algorithms) <= {'RS256', 'ES256'}:
            raise ValueError('Only RS256 and ES256 are supported')
        if not 1 <= self.jwks_ttl <= 3600 or not 0 < self.request_timeout <= 30 or not 0 <= self.leeway <= 60 or not 0 < self.max_token_lifetime <= 86400:
            raise ValueError('Authentication cache, timeout, or clock tolerance is invalid')

    @classmethod
    def from_env(cls, prefix='REPLAY_AUTH_'):
        return cls(mode=os.getenv(prefix + 'MODE', 'production'), issuer=os.getenv(prefix + 'ISSUER', ''),
                   audience=os.getenv(prefix + 'AUDIENCE', ''), jwks_url=os.getenv(prefix + 'JWKS_URL', ''),
                   tenant_claim=os.getenv(prefix + 'TENANT_CLAIM', 'tenant_id'),
                   roles_claim=os.getenv(prefix + 'ROLES_CLAIM', 'roles'),
                   membership_claim=os.getenv(prefix + 'MEMBERSHIP_CLAIM') or None,
                   dev_token=os.getenv(prefix + 'DEV_TOKEN'),
                   dev_subject=os.getenv(prefix + 'DEV_SUBJECT', 'developer'),
                   dev_tenant_id=os.getenv(prefix + 'DEV_TENANT_ID', 'development'))


def _identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}', value) is not None


def _unauthorized():
    return HTTPException(401, '유효한 사용자 인증이 필요합니다.', headers={'WWW-Authenticate': 'Bearer'})


class JWTAuthenticator:
    def __init__(self, config: AuthConfig, http_client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = http_client or httpx.AsyncClient(timeout=config.request_timeout, follow_redirects=False)
        self._owns_client = http_client is None
        self._keys = {}
        self._expires = 0.0
        self._refreshed = float('-inf')
        self._unavailable_until = 0.0
        self._lock = asyncio.Lock()

    async def close(self):
        if self._owns_client:
            await self._client.aclose()

    async def _key(self, kid, algorithm):
        async with self._lock:
            now = time.monotonic()
            if now < self._unavailable_until:
                raise HTTPException(503, '인증 서버의 서명 키를 확인할 수 없습니다. 잠시 후 다시 시도하세요.')
            # Unknown kids can trigger rotation refresh, but cannot flood the issuer.
            refresh = now >= self._expires or (kid not in self._keys and now - self._refreshed >= 5)
            if refresh:
                self._refreshed = now
                try:
                    response = await self._client.get(self.config.jwks_url, timeout=self.config.request_timeout)
                    response.raise_for_status()
                    if len(response.content) > 256 * 1024:
                        raise ValueError('JWKS too large')
                    document = response.json()
                    entries = document.get('keys')
                    if not isinstance(entries, list) or not 1 <= len(entries) <= 64:
                        raise ValueError('Invalid JWKS')
                    keys = {}
                    for item in entries:
                        if not isinstance(item, dict) or item.get('use', 'sig') != 'sig':
                            continue
                        identifier = item.get('kid')
                        alg = item.get('alg') or {'RSA': 'RS256', 'EC': 'ES256'}.get(item.get('kty'))
                        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 256 or alg not in self.config.algorithms:
                            continue
                        if 'd' in item or ('key_ops' in item and (not isinstance(item['key_ops'], list) or 'verify' not in item['key_ops'])):
                            continue
                        if (alg == 'RS256' and item.get('kty') != 'RSA') or (alg == 'ES256' and (item.get('kty') != 'EC' or item.get('crv') != 'P-256')):
                            continue
                        key = jwt.PyJWK.from_dict(item, algorithm=alg).key
                        if alg == 'RS256' and key.key_size < 2048:
                            continue
                        if identifier in keys:
                            raise ValueError('Ambiguous JWKS kid')
                        keys[identifier] = (alg, key)
                    if not keys:
                        raise ValueError('No trusted signing keys')
                    self._keys = keys
                    self._expires = now + self.config.jwks_ttl
                except (httpx.HTTPError, ValueError, TypeError, AttributeError, jwt.PyJWTError):
                    self._unavailable_until = time.monotonic() + 5
                    raise HTTPException(503, '인증 서버의 서명 키를 확인할 수 없습니다. 잠시 후 다시 시도하세요.') from None
            entry = self._keys.get(kid)
            if not entry or entry[0] != algorithm:
                raise _unauthorized()
            return entry[1]

    async def authenticate(self, authorization: str, requested_tenant: str | None = None) -> Principal:
        if not authorization.startswith('Bearer '):
            raise _unauthorized()
        token = authorization[7:]
        if not token or len(token) > 16384 or any(character.isspace() for character in token):
            raise _unauthorized()
        if self.config.mode == 'development':
            if not hmac.compare_digest(token.encode(), self.config.dev_token.encode()):
                raise _unauthorized()
            principal = Principal(self.config.dev_subject, self.config.dev_tenant_id, ('admin',),
                                  hashlib.sha256(('development:' + token).encode()).hexdigest(), time.time() + 3600)
        else:
            try:
                header = jwt.get_unverified_header(token)
            except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
                raise _unauthorized() from None
            algorithm, kid = header.get('alg'), header.get('kid')
            if algorithm not in self.config.algorithms or not isinstance(kid, str) or not 1 <= len(kid) <= 256:
                raise _unauthorized()
            # Never follow jku/x5u or accept token-supplied keys.
            if any(name in header for name in ('jku', 'x5u', 'jwk', 'crit')):
                raise _unauthorized()
            key = await self._key(kid, algorithm)
            try:
                claims = jwt.decode(token, key=key, algorithms=[algorithm], issuer=self.config.issuer,
                                    audience=self.config.audience, leeway=self.config.leeway,
                                    options={'require': ['iss', 'aud', 'exp', 'iat', 'sub']})
            except (jwt.PyJWTError, ValueError, TypeError, OverflowError):
                raise _unauthorized() from None
            subject, tenant = claims.get('sub'), claims.get(self.config.tenant_claim)
            if not isinstance(subject, str) or not 1 <= len(subject) <= 256 or not _identifier(tenant):
                raise _unauthorized()
            if any(isinstance(claims.get(name), bool) or not isinstance(claims.get(name), (int, float))
                   or not math.isfinite(claims[name]) for name in ('exp', 'iat')):
                raise _unauthorized()
            if not 0 < claims['exp'] - claims['iat'] <= self.config.max_token_lifetime:
                raise _unauthorized()
            if claims.get('token_use', 'access') != 'access':
                raise _unauthorized()
            if self.config.membership_claim:
                members = claims.get(self.config.membership_claim)
                if not isinstance(members, list) or not members or len(members) > 256 or not all(_identifier(member) for member in members) or tenant not in members:
                    raise HTTPException(403, '이 조직의 구성원 권한이 필요합니다.')
            roles = claims.get(self.config.roles_claim, [])
            if not isinstance(roles, list) or len(roles) > 64 or not all(_identifier(role) for role in roles):
                raise _unauthorized()
            jti = claims.get('jti')
            if jti is not None and (not isinstance(jti, str) or not 1 <= len(jti) <= 256):
                raise _unauthorized()
            identity = json.dumps([self.config.issuer, subject, tenant, jti or token], separators=(',', ':'))
            principal = Principal(subject, tenant, tuple(dict.fromkeys(roles)),
                                  hashlib.sha256(identity.encode()).hexdigest(), claims['exp'])
        if requested_tenant is not None and requested_tenant != principal.tenant_id:
            raise HTTPException(403, '다른 조직의 자료에 접근할 수 없습니다.')
        return principal
