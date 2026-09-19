"""Federation tests perform no AWS or Vercel network calls."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import threading

import pytest
from starlette.concurrency import run_in_threadpool

from server.aws_identity import (AWSIdentityError, VercelAWSCredentials,
                                  VercelOIDCMiddleware, oidc_token_context)


ROLE = 'arn:aws:iam::123456789012:role/replay/control-plane'
ISSUER = 'https://oidc.vercel.com/test-team'
AUDIENCE = 'https://vercel.com/test-team'
SUBJECT = 'owner:test-team:project:replay-api:environment:production'


def token(*, expiry=9000, nonce='first', alg='RS256', **claims):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=').decode()
    return '.'.join([encode({'alg': alg, 'typ': 'JWT', 'kid': 'synthetic'}),
        encode({'iss': ISSUER, 'aud': AUDIENCE, 'sub': SUBJECT, 'iat': 1000, 'exp': expiry, **claims}),
        base64.urlsafe_b64encode(('synthetic-' + nonce).encode()).rstrip(b'=').decode()])


class FakeSTS:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.reject = set()
        self.closed = False

    def assume_role_with_web_identity(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs['WebIdentityToken'] in self.reject:
            raise RuntimeError('invalid JWT: ' + kwargs['WebIdentityToken'])
        return {'Credentials': {
            'AccessKeyId': 'ASIA' + str(len(self.calls)).zfill(16),
            'SecretAccessKey': 'test-private-access-secret' * 2,
            'SessionToken': 'test-private-session-token-' + str(len(self.calls)),
            'Expiration': datetime.fromtimestamp(self.clock() + kwargs['DurationSeconds'], timezone.utc),
        }}

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, service, credential):
        self.service, self.credential = service, credential
        self.closed = False

    def head_object(self, **kwargs):
        return {'service': self.service, 'credential': self.credential}

    def encrypt(self, **kwargs):
        return {'credential': self.credential}

    def generate_presigned_url(self, operation, **kwargs):
        return {'credential': self.credential, 'expires': kwargs['ExpiresIn']}

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, **kwargs):
        self.credential = kwargs['aws_access_key_id']
        self.clients = []

    def client(self, service, **kwargs):
        result = FakeClient(service, self.credential)
        self.clients.append(result)
        return result


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.delenv('VERCEL_OIDC_TOKEN', raising=False)
    now = [1000.0]
    sts = FakeSTS(lambda: now[0])
    provider = VercelAWSCredentials(ROLE, 'ap-northeast-2', issuer=ISSUER, audience=AUDIENCE,
        subject=SUBJECT, sts_client=sts, session_factory=FakeSession, clock=lambda: now[0])
    return provider, sts, now


def test_lazy_clients_need_no_identity_until_called_and_use_fixed_role(identity):
    provider, sts, _ = identity
    client = provider.client('s3')
    assert sts.calls == []
    with pytest.raises(AWSIdentityError, match='current'):
        client.head_object(Bucket='private', Key='object')
    with oidc_token_context(token()):
        first = client.head_object(Bucket='private', Key='object')
        second = provider.client('kms').encrypt(KeyId='fixed-key', Plaintext=b'synthetic')
    assert first['credential'] == second['credential']
    assert len(sts.calls) == 1
    assert sts.calls[0]['RoleArn'] == ROLE
    assert sts.calls[0]['DurationSeconds'] == 3600
    assert token() not in sts.calls[0]['RoleSessionName']


def test_expired_sts_credentials_refresh_before_they_are_used(identity):
    provider, sts, now = identity
    client = provider.client('s3')
    with oidc_token_context(token()):
        first = client.head_object()['credential']
        now[0] = 4550  # initial STS expiry 4600; within the 60 second margin
        second = client.head_object()['credential']
    assert first != second and len(sts.calls) == 2


def test_presigned_url_refreshes_if_credentials_cannot_cover_its_entire_lifetime(identity):
    provider, sts, now = identity
    client = provider.client('s3')
    with oidc_token_context(token()):
        first = client.head_object()['credential']
        now[0] = 4000  # 600 seconds remain, insufficient for a 900 second URL
        signed = client.generate_presigned_url('put_object', Params={}, ExpiresIn=900)
    assert signed['credential'] != first and signed['expires'] == 900
    assert len(sts.calls) == 2


def test_expired_workload_token_cannot_borrow_still_valid_cached_aws_credentials(identity):
    provider, sts, now = identity
    with oidc_token_context(token(expiry=1100)):
        provider.client('s3').head_object()
        now[0] = 1101
        with pytest.raises(AWSIdentityError, match='expired'):
            provider.client('s3').head_object()
    assert len(sts.calls) == 1


def test_same_claims_different_signature_must_pass_sts_independently(identity):
    provider, sts, _ = identity
    good, forged = token(nonce='trusted-by-fake-sts'), token(nonce='rejected-by-fake-sts')
    with oidc_token_context(good):
        provider.client('s3').head_object()
    sts.reject.add(forged)
    with oidc_token_context(forged), pytest.raises(AWSIdentityError) as caught:
        provider.client('s3').head_object()
    assert len(sts.calls) == 2
    assert forged not in str(caught.value)
    assert 'test-private' not in str(caught.value)


@pytest.mark.parametrize('changes', [
    {'iss': 'https://oidc.vercel.com/other-team'}, {'aud': 'https://vercel.com/other-team'},
    {'sub': SUBJECT.replace('production', 'preview')}, {'sub': SUBJECT.replace('replay-api', 'other-project')},
    {'alg': 'none'}, {'nbf': 2000}, {'expiry': True}, {'expiry': 1e100},
])
def test_unexpected_deployment_claims_rejected_before_sts(identity, changes):
    provider, sts, _ = identity
    with oidc_token_context(token(**changes)), pytest.raises(AWSIdentityError):
        provider.client('s3').head_object()
    assert sts.calls == []


def test_request_identities_never_mix_even_with_a_bound_proxy_method(identity):
    provider, sts, _ = identity
    call = provider.client('s3').head_object
    with oidc_token_context(token(nonce='one')):
        first = call()['credential']
        with oidc_token_context(token(nonce='two')):
            second = call()['credential']
        assert call()['credential'] == first
    assert first != second
    with pytest.raises(AWSIdentityError):
        call()
    assert len(sts.calls) == 2


def test_concurrent_same_token_has_one_sts_exchange(identity):
    provider, sts, _ = identity
    barrier = threading.Barrier(12)

    def call(_):
        with oidc_token_context(token()):
            barrier.wait()
            return provider.client('s3').head_object()['credential']

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(call, range(12)))
    assert len(set(responses)) == 1 and len(sts.calls) == 1


def test_threaded_different_requests_keep_distinct_credentials(identity):
    provider, sts, _ = identity
    barrier = threading.Barrier(8)

    def call(index):
        with oidc_token_context(token(nonce=str(index))):
            barrier.wait()
            return provider.client('s3').head_object()['credential']

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(call, range(8)))
    assert len(set(responses)) == 8 and len(sts.calls) == 8


def test_cache_is_bounded_and_old_identity_requires_sts_again(identity):
    provider, sts, _ = identity
    for index in range(10):
        with oidc_token_context(token(nonce=str(index))):
            provider.client('s3').head_object()
    assert len(provider._cache) == 8
    with oidc_token_context(token(nonce='0')):
        provider.client('s3').head_object()
    assert len(sts.calls) == 11


def test_http_missing_header_cannot_fall_back_to_stale_environment(identity, monkeypatch):
    provider, sts, _ = identity
    monkeypatch.setenv('VERCEL_OIDC_TOKEN', token(nonce='build'))
    assert provider.client('s3').head_object()

    async def application(scope, receive, send):
        with pytest.raises(AWSIdentityError, match='current'):
            await run_in_threadpool(provider.client('s3').head_object)

    async def unused():
        return {}

    async def send(_):
        pass

    asyncio.run(VercelOIDCMiddleware(application)({'type': 'http', 'headers': []}, unused, send))
    assert len(sts.calls) == 1
    assert provider.client('s3').head_object()


def test_middleware_propagates_current_header_into_threadpool_and_resets_after_failure(identity):
    provider, sts, _ = identity
    current = token(nonce='runtime')

    async def application(scope, receive, send):
        assert await run_in_threadpool(provider.client('kms').encrypt)
        raise RuntimeError('synthetic app failure')

    async def unused():
        return {}

    async def send(_):
        pass

    with pytest.raises(RuntimeError, match='synthetic'):
        asyncio.run(VercelOIDCMiddleware(application)(
            {'type': 'http', 'headers': [(b'x-vercel-oidc-token', current.encode())]}, unused, send))
    assert sts.calls[0]['WebIdentityToken'] == current
    with pytest.raises(AWSIdentityError, match='current'):
        provider.client('kms').encrypt()


def test_duplicate_headers_and_invalid_utf8_do_not_select_a_token(identity):
    provider, sts, _ = identity

    async def application(scope, receive, send):
        with pytest.raises(AWSIdentityError):
            provider.client('s3').head_object()

    async def unused():
        return {}

    async def send(_):
        pass

    for headers in [[(b'x-vercel-oidc-token', token().encode())] * 2,
                    [(b'x-vercel-oidc-token', b'\xff\xfe')]]:
        asyncio.run(VercelOIDCMiddleware(application)({'type': 'http', 'headers': headers}, unused, send))
    assert sts.calls == []


def test_token_expiring_while_waiting_for_refresh_cannot_establish_a_session(identity):
    provider, sts, now = identity
    entered = threading.Event()

    def worker():
        with oidc_token_context(token(expiry=1010)):
            entered.set()
            return provider.client('s3').head_object()

    with provider._lock:
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(worker)
        assert entered.wait(2)
        now[0] = 1011
    with pytest.raises(AWSIdentityError, match='expired'):
        future.result(timeout=5)
    pool.shutdown()
    assert sts.calls == []


def test_fixed_role_and_deployment_config_reject_wildcards(identity):
    for changes in [{'role_arn': 'arn:aws:iam::123456789012:role/*'},
                    {'subject': 'owner:test-team:project:*:environment:production'},
                    {'issuer': 'https://evil.example'}, {'audience': '*'}]:
        kwargs = dict(role_arn=ROLE, region='ap-northeast-2', issuer=ISSUER, audience=AUDIENCE, subject=SUBJECT)
        with pytest.raises(AWSIdentityError):
            VercelAWSCredentials(**(kwargs | changes))
    provider, _, _ = identity
    with pytest.raises(AWSIdentityError):
        provider.client('iam')
    with pytest.raises(AttributeError):
        provider.client('s3').get_paginator


def test_short_or_malformed_sts_credentials_fail_without_a_cached_session(identity):
    provider, sts, now = identity
    original = sts.assume_role_with_web_identity

    def short_lived(**kwargs):
        result = original(**kwargs)
        result['Credentials']['Expiration'] = datetime.fromtimestamp(now[0] + 10, timezone.utc)
        return result

    sts.assume_role_with_web_identity = short_lived
    with oidc_token_context(token()), pytest.raises(AWSIdentityError, match='could not'):
        provider.client('s3').head_object()
    assert len(provider._cache) == 0


def test_close_releases_cached_clients(identity):
    provider, sts, _ = identity
    with oidc_token_context(token()):
        provider.client('s3').head_object()
    clients = list(next(iter(provider._cache.values())).clients.values())
    provider.close()
    assert not provider._cache and sts.closed
    assert all(client.closed for client in clients)


def test_oidc_expiry_during_sts_exchange_never_releases_credentials(identity):
    provider, sts, now = identity
    original = sts.assume_role_with_web_identity

    def slow_sts(**kwargs):
        result = original(**kwargs)
        now[0] = 1101
        return result

    sts.assume_role_with_web_identity = slow_sts
    with oidc_token_context(token(expiry=1100)), pytest.raises(AWSIdentityError):
        provider.client('s3').head_object()
    assert not provider._cache


def test_default_sts_client_is_unsigned_and_has_bounded_timeouts(identity, monkeypatch):
    import boto3
    from botocore import UNSIGNED
    provider, sts, _ = identity
    calls = []

    def build(service, **kwargs):
        calls.append((service, kwargs))
        return sts

    monkeypatch.setattr(boto3, 'client', build)
    provider._sts = None
    with oidc_token_context(token()):
        provider.client('s3').head_object()
    service, kwargs = calls[0]
    assert service == 'sts'
    assert kwargs['config'].signature_version is UNSIGNED
    assert kwargs['config'].connect_timeout == 3
    assert kwargs['config'].read_timeout == 5
    assert kwargs['config'].retries['total_max_attempts'] == 2
    assert not any(key.startswith('aws_') for key in kwargs)
