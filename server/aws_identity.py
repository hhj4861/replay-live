"""Request-scoped Vercel OIDC federation into short-lived AWS credentials.

Vercel supplies runtime tokens in ``x-vercel-oidc-token``; only code running
outside an HTTP request may use ``VERCEL_OIDC_TOKEN`` for build/local work.
Unsigned JWT parsing below is a rejection/expiry precheck, never authentication:
AWS STS verifies the signature and the fixed role's exact issuer/audience/subject
trust policy. No workload identity, credential or service URL is logged.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
import base64
import hashlib
import json
import math
import os
import re
import threading
import time
from urllib.parse import urlsplit


class AWSIdentityError(ValueError):
    """Safe failure to obtain the configured workload identity."""


_OUTSIDE_REQUEST = object()
_request_token = ContextVar('replay_vercel_oidc_token', default=_OUTSIDE_REQUEST)
_METHODS = {
    's3': frozenset({'generate_presigned_url', 'head_object', 'get_object', 'put_object',
                     'delete_object', 'get_public_access_block', 'head_bucket'}),
    'kms': frozenset({'encrypt', 'decrypt'}),
}


@contextmanager
def oidc_token_context(token):
    """Bind a request token, including None; always restore the previous context."""
    marker = _request_token.set(token)
    try:
        yield
    finally:
        _request_token.reset(marker)


class VercelOIDCMiddleware:
    """Pure ASGI middleware preserves the context through async/threadpool calls."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        headers = [value for name, value in scope.get('headers', []) if name.lower() == b'x-vercel-oidc-token']
        # Duplicate or non-ASCII tokens cannot select a different identity or
        # silently fall back to a stale build-time environment variable.
        token = None
        if len(headers) == 1:
            try:
                token = headers[0].decode('ascii')
            except UnicodeDecodeError:
                pass
        with oidc_token_context(token):
            await self.app(scope, receive, send)


@dataclass(repr=False)
class _Entry:
    expires_at: float
    token_expires_at: float
    session: object = field(repr=False)
    clients: dict = field(default_factory=dict, repr=False)


def _finite_timestamp(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _decode_segment(segment):
    try:
        return json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))
    except (ValueError, UnicodeError):
        raise AWSIdentityError('Vercel workload token is invalid') from None


def _https_origin_or_path(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == 'https' and bool(parsed.hostname) and not any(
        [parsed.username, parsed.password, parsed.query, parsed.fragment])


class VercelAWSCredentials:
    def __init__(self, role_arn, region, *, issuer, audience, subject,
                 sts_client=None, session_factory=None, clock=time.time,
                 refresh_margin=60, max_cache_entries=8, session_duration=3600):
        if not isinstance(role_arn, str) or not re.fullmatch(
                r'arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}', role_arn):
            raise AWSIdentityError('A fixed AWS IAM role ARN is required')
        if not isinstance(region, str) or not re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-[0-9]', region):
            raise AWSIdentityError('A fixed AWS region is required')
        identity = re.fullmatch(r'owner:([A-Za-z0-9_-]{1,128}):project:([A-Za-z0-9_.-]{1,128}):environment:(production|preview|development)', subject or '')
        if not identity or issuer not in ('https://oidc.vercel.com', f'https://oidc.vercel.com/{identity.group(1)}'):
            raise AWSIdentityError('Exact Vercel issuer and deployment subject are required')
        if not _https_origin_or_path(audience) or '*' in audience:
            raise AWSIdentityError('An exact Vercel OIDC audience is required')
        if isinstance(refresh_margin, bool) or not isinstance(refresh_margin, int) or not 30 <= refresh_margin <= 300:
            raise AWSIdentityError('Invalid AWS credential refresh margin')
        if isinstance(session_duration, bool) or not isinstance(session_duration, int) or not 1800 <= session_duration <= 3600:
            raise AWSIdentityError('AWS workload sessions must last 30 to 60 minutes')
        if isinstance(max_cache_entries, bool) or not isinstance(max_cache_entries, int) or not 1 <= max_cache_entries <= 64:
            raise AWSIdentityError('Invalid AWS credential cache size')
        self.role_arn, self.region = role_arn, region
        self.issuer, self.audience, self.subject = issuer, audience, subject
        self.clock, self.refresh_margin = clock, refresh_margin
        self.max_cache_entries, self.session_duration = max_cache_entries, session_duration
        self._sts = sts_client
        self._session_factory = session_factory
        self._cache = OrderedDict()
        self._lock = threading.RLock()

    def _token(self):
        token = _request_token.get()
        if token is _OUTSIDE_REQUEST:
            token = os.environ.get('VERCEL_OIDC_TOKEN')
        if not isinstance(token, str) or not 32 <= len(token) <= 16384 or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token):
            raise AWSIdentityError('A current Vercel workload token is required')
        header, payload, _ = token.split('.')
        header, payload = _decode_segment(header), _decode_segment(payload)
        now = self.clock()
        if not isinstance(header, dict) or header.get('alg') != 'RS256' or not isinstance(payload, dict):
            raise AWSIdentityError('Vercel workload token is invalid')
        if payload.get('iss') != self.issuer or payload.get('sub') != self.subject or payload.get('aud') not in (self.audience, [self.audience]):
            raise AWSIdentityError('Vercel workload identity does not match this deployment')
        expiry = payload.get('exp')
        if not _finite_timestamp(expiry) or not now < expiry <= now + 12 * 3600 + 60:
            raise AWSIdentityError('Vercel workload token has expired or has an invalid lifetime')
        for name in ('nbf', 'iat'):
            if name in payload and (not _finite_timestamp(payload[name]) or payload[name] > now + 30):
                raise AWSIdentityError('Vercel workload token is not active')
        # The complete token fingerprint prevents an unsigned modified JWT from
        # borrowing credentials cached for an AWS-verified token with the same claims.
        return token, hashlib.sha256(token.encode()).hexdigest(), float(expiry)

    def _sts_client(self):
        if self._sts is None:
            import boto3
            from botocore import UNSIGNED
            from botocore.config import Config
            # AssumeRoleWithWebIdentity authenticates the supplied JWT itself;
            # no static credentials or metadata-service lookup are needed.
            self._sts = boto3.client('sts', region_name=self.region,
                config=Config(signature_version=UNSIGNED, connect_timeout=3, read_timeout=5,
                              retries={'mode': 'standard', 'total_max_attempts': 2}))
        return self._sts

    def _assume(self, token, fingerprint, token_expiry, required_lifetime):
        try:
            result = self._sts_client().assume_role_with_web_identity(
                RoleArn=self.role_arn, RoleSessionName='replay-' + fingerprint[:24],
                WebIdentityToken=token, DurationSeconds=self.session_duration)
            credentials = result['Credentials']
            expiration = credentials['Expiration']
            if isinstance(expiration, datetime):
                if expiration.tzinfo is None:
                    raise ValueError()
                expiration = expiration.timestamp()
            elif isinstance(expiration, str):
                parsed = datetime.fromisoformat(expiration.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    raise ValueError()
                expiration = parsed.timestamp()
            if not _finite_timestamp(expiration) or not self.clock() + required_lifetime < expiration <= self.clock() + self.session_duration + 60:
                raise ValueError()
            if token_expiry <= self.clock():
                raise ValueError()
            access = credentials['AccessKeyId']
            secret = credentials['SecretAccessKey']
            session_token = credentials['SessionToken']
            if not isinstance(access, str) or not re.fullmatch(r'[A-Za-z0-9]{16,128}', access):
                raise ValueError()
            if not isinstance(secret, str) or not 16 <= len(secret) <= 1024 or not isinstance(session_token, str) or not session_token:
                raise ValueError()
            factory = self._session_factory
            if factory is None:
                import boto3
                factory = boto3.session.Session
            session = factory(aws_access_key_id=access, aws_secret_access_key=secret,
                              aws_session_token=session_token, region_name=self.region)
            return _Entry(float(expiration), token_expiry, session)
        except Exception:
            # AWS exceptions may echo JWTs or request bodies. Never forward them.
            raise AWSIdentityError('AWS workload credentials could not be established') from None

    def _client_for_request(self, service, required_lifetime):
        token, fingerprint, token_expiry = self._token()
        with self._lock:
            # Recheck after waiting for another in-flight STS exchange.
            now = self.clock()
            if token_expiry <= now:
                raise AWSIdentityError('Vercel workload token has expired')
            for key, entry in list(self._cache.items()):
                if min(entry.expires_at, entry.token_expires_at) <= now:
                    self._cache.pop(key)
            entry = self._cache.get(fingerprint)
            if entry is None or entry.expires_at <= now + required_lifetime:
                entry = self._assume(token, fingerprint, token_expiry, required_lifetime)
                self._cache[fingerprint] = entry
            self._cache.move_to_end(fingerprint)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)
            if service not in entry.clients:
                from botocore.config import Config
                entry.clients[service] = entry.session.client(service, region_name=self.region,
                    config=Config(signature_version='s3v4' if service == 's3' else 'v4',
                                  connect_timeout=3, read_timeout=5,
                                  retries={'mode': 'standard', 'total_max_attempts': 2}))
            return entry.clients[service]

    def client(self, service):
        """Create a proxy safely at module scope; resolve identity on each call."""
        if service not in _METHODS:
            raise AWSIdentityError('This application may federate only S3 and KMS clients')
        return _ClientProxy(self, service)

    def close(self):
        with self._lock:
            entries, self._cache = list(self._cache.values()), OrderedDict()
            sts, self._sts = self._sts, None
        for entry in entries:
            for client in entry.clients.values():
                if hasattr(client, 'close'):
                    client.close()
        if sts is not None and hasattr(sts, 'close'):
            sts.close()


class _ClientProxy:
    def __init__(self, provider, service):
        self._provider, self._service = provider, service

    def __getattr__(self, name):
        if name not in _METHODS[self._service]:
            raise AttributeError(name)

        def invoke(*args, **kwargs):
            lifetime = self._provider.refresh_margin
            if name == 'generate_presigned_url':
                # A URL also expires when the signing STS credentials expire.
                # Renew first if cached credentials cannot cover the whole URL.
                expires = kwargs.get('ExpiresIn', 3600)
                if isinstance(expires, bool) or not isinstance(expires, int) or not 1 <= expires <= 900:
                    raise AWSIdentityError('Presigned objects must expire within 15 minutes')
                lifetime = max(lifetime, expires + 30)
            client = self._provider._client_for_request(self._service, lifetime)
            return getattr(client, name)(*args, **kwargs)

        return invoke
