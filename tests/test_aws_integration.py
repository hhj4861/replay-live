"""Production factory -> middleware -> S3/KMS proxy integration without AWS.

The PostgreSQL access-policy and saved-connection stores use their SQLite test
backends. Storage, KMS envelopes, workload middleware, API handlers and the AWS
credential provider are the actual production implementations.
"""
import base64
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import time
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from server.access_policy import AccessPolicy
from server.auth import Principal
from server.aws_identity import VercelAWSCredentials
import server.production_app as production
from server.repository import Repository
from server.settings import Settings
from server.stream_connections import StreamConnections


ROLE = 'arn:aws:iam::123456789012:role/replay-api'
ISSUER = 'https://oidc.vercel.com/replay-team'
AUDIENCE = 'https://vercel.com/replay-team'
SUBJECT = 'owner:replay-team:project:replay-api:environment:production'
SYNTHETIC_KEY = 'synthetic-youtube-key-1234'


def workload_token(nonce, *, expired=False):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    now = time.time()
    return '.'.join([encode({'alg': 'RS256', 'kid': 'synthetic'}),
        encode({'iss': ISSUER, 'aud': AUDIENCE, 'sub': SUBJECT,
                'iat': now - 60, 'exp': now - 1 if expired else now + 7200}),
        base64.urlsafe_b64encode(nonce.encode()).rstrip(b'=').decode()])


class ProductionAuth:
    config = SimpleNamespace(mode='production')
    closed = False

    async def authenticate(self, authorization, requested_tenant=None):
        if authorization != 'Bearer synthetic-customer':
            raise HTTPException(401, 'Unauthorized')
        return Principal('synthetic-user', 'synthetic-tenant', ('operator',),
                         hashlib.sha256(authorization.encode()).hexdigest(), time.time() + 300)

    async def close(self):
        self.closed = True


@pytest.fixture
def aws_app(tmp_path, monkeypatch):
    monkeypatch.delenv('VERCEL_OIDC_TOKEN', raising=False)
    trace = {'sts': [], 'services': [], 'clients': [], 'sts_closed': False}

    class STS:
        def assume_role_with_web_identity(self, **kwargs):
            trace['sts'].append(kwargs)
            return {'Credentials': {'AccessKeyId': 'ASIA' + str(len(trace['sts'])).zfill(16),
                'SecretAccessKey': 'synthetic-temporary-secret' * 2, 'SessionToken': 'synthetic-temporary-session',
                'Expiration': datetime.fromtimestamp(time.time() + 3600, timezone.utc)}}

        def close(self):
            trace['sts_closed'] = True

    class Client:
        def __init__(self, service, credential):
            self.service, self.credential, self.closed = service, credential, False
            trace['clients'].append(self)

        def record(self, operation):
            trace['services'].append((self.service, operation, self.credential))

        def generate_presigned_url(self, operation, **kwargs):
            self.record(operation)
            return 'https://private-s3.example/synthetic-object?authorization=opaque'

        def encrypt(self, **kwargs):
            self.record('encrypt')
            assert kwargs['EncryptionContext'] == {'tenant_id': 'synthetic-tenant'}
            return {'CiphertextBlob': b'synthetic-encrypted-data'}

        def decrypt(self, **kwargs):
            self.record('decrypt')
            assert kwargs['EncryptionContext'] == {'tenant_id': 'synthetic-tenant'}
            return {'Plaintext': SYNTHETIC_KEY.encode()}

        def close(self):
            self.closed = True

    class Session:
        def __init__(self, **kwargs):
            self.credential = kwargs['aws_access_key_id']

        def client(self, service, **kwargs):
            assert kwargs['config'].connect_timeout == 3
            assert kwargs['config'].read_timeout == 5
            assert kwargs['config'].retries['total_max_attempts'] == 2
            return Client(service, self.credential)

    providers = []

    def workload(*args, **kwargs):
        provider = VercelAWSCredentials(*args, **kwargs, sts_client=STS(), session_factory=Session)
        providers.append(provider)
        return provider

    monkeypatch.setattr(production, 'VercelAWSCredentials', workload)
    # This fixture tests AWS identity wiring, without DNS or publishing.
    monkeypatch.setattr(production, 'pin_destination', lambda destination: {**destination, 'addresses': ['8.8.8.8']})
    monkeypatch.setattr(production, 'AccessPolicy', lambda engine, mode: AccessPolicy(engine, mode='development'))
    monkeypatch.setattr(production, 'StreamConnections',
                        lambda engine, keys, mode: StreamConnections(engine, keys, mode='test'))
    cfg = Settings(mode='production', database_url='postgresql+psycopg://unused/replay',
        public_url='https://api.test', origins=('https://ui.test',), control_token='c' * 32,
        callback_key='k' * 32, bucket='replay-private-test',
        kms_key_id='arn:aws:kms:ap-northeast-2:123456789012:key/12345678-1234-1234-1234-123456789012',
        aws_auth_mode='vercel_oidc', aws_role_arn=ROLE, aws_oidc_issuer=ISSUER,
        aws_oidc_audience=AUDIENCE, aws_oidc_subject=SUBJECT, version='aws-integration')
    repo = Repository('sqlite:///' + str(tmp_path / 'state.db'), create_schema=True)
    auth = ProductionAuth()
    app = production.create_production_app(cfg, repository=repo, authenticator=auth)
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    return app, repo, trace, cfg, auth, providers


def test_production_factory_context_reaches_s3_and_kms_and_closes_on_shutdown(aws_app):
    app, repo, trace, cfg, auth, providers = aws_app
    assert trace['sts'] == []  # cold start does not need an HTTP workload token
    first, second, third = [workload_token(nonce) for nonce in ('upload', 'encrypt', 'claim')]
    with TestClient(app, base_url=cfg.public_url, headers={'Authorization': 'Bearer synthetic-customer'}) as client:
        upload = client.post('/api/uploads', json={'name': 'synthetic.mp4', 'bytes': 1000, 'sha256': 'a' * 64},
                             headers={'x-vercel-oidc-token': first})
        assert upload.status_code == 201
        item = upload.json()['media']
        # This test scopes AWS factory wiring; full decoder validation is covered
        # by commercial API/worker integration tests.
        repo.update_media('synthetic-tenant', item['id'], status='ready', duration=15, width=1280, height=720, fps=30)
        job = client.post('/api/broadcasts', json={'media_id': item['id'], 'title': 'Synthetic AWS wiring',
            'target': 'youtube', 'stream_key': SYNTHETIC_KEY},
            headers={'x-vercel-oidc-token': second, 'Idempotency-Key': 'aws-wiring-test'})
        assert job.status_code == 201
        assert SYNTHETIC_KEY not in job.text
        claim = client.post('/internal/claim', json={'worker_id': 'synthetic-dispatcher', 'version': cfg.version},
                            headers={'Authorization': 'Bearer ' + cfg.control_token, 'x-vercel-oidc-token': third})
        assert claim.status_code == 200
        assert claim.json()['job']['id'] == job.json()['id']
        # No Sandbox or FFmpeg is launched by this control-plane request.
        assert repo.get_job('synthetic-tenant', job.json()['id'])['state'] == 'starting'
    assert [call['WebIdentityToken'] for call in trace['sts']] == [first, second, third]
    assert all(call['RoleArn'] == ROLE for call in trace['sts'])
    assert [(service, operation) for service, operation, _ in trace['services']] == [
        ('s3', 'put_object'), ('kms', 'encrypt'), ('kms', 'decrypt'), ('s3', 'get_object')]
    credentials = [credential for _, _, credential in trace['services']]
    assert credentials[0] != credentials[1] != credentials[2] == credentials[3]
    assert auth.closed and trace['sts_closed']
    assert all(client.closed for client in trace['clients'])
    assert all(not provider._cache for provider in providers)


def test_storage_and_kms_identity_failures_are_503_without_environment_token_fallback(aws_app, monkeypatch):
    app, repo, trace, cfg, _, _ = aws_app
    build_token = workload_token('build-only')
    monkeypatch.setenv('VERCEL_OIDC_TOKEN', build_token)
    key = app.state.storage.key('synthetic-tenant', 'known-ready')
    item = repo.add_media('synthetic-tenant', name='synthetic.mp4', object_key=key, bytes=1000,
                         duration=15, width=1280, height=720, fps=30)
    with TestClient(app, base_url=cfg.public_url, headers={'Authorization': 'Bearer synthetic-customer'}) as client:
        upload = client.post('/api/uploads', json={'name': 'synthetic.mp4', 'bytes': 1000, 'sha256': 'a' * 64})
        assert upload.status_code == 503
        kms = client.post('/api/broadcasts', json={'media_id': item['id'], 'title': 'Synthetic failure',
            'target': 'youtube', 'stream_key': SYNTHETIC_KEY}, headers={'Idempotency-Key': 'aws-failure-test'})
        assert kms.status_code == 503
        expired = client.get(f"/api/media/{item['id']}/preview", headers={'x-vercel-oidc-token': workload_token('expired', expired=True)})
        assert expired.status_code == 503
        unauthorized = client.post('/api/uploads', json={'name': 'synthetic.mp4', 'bytes': 1000, 'sha256': 'a' * 64},
                                   headers={'Authorization': 'Bearer wrong', 'x-vercel-oidc-token': workload_token('valid')})
        assert unauthorized.status_code == 401
        for response in (upload, kms, expired):
            assert build_token not in response.text and SYNTHETIC_KEY not in response.text
            assert response.headers['Cache-Control'] == 'no-store'
    assert trace['sts'] == []
    assert repo.list_jobs('synthetic-tenant') == []


def test_standard_aws_factory_keeps_default_identity_chain_and_explicit_region(aws_app, monkeypatch):
    import boto3
    _, repo, trace, cfg, _, providers = aws_app
    calls = []

    def sdk_client(service, **kwargs):
        calls.append((service, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(boto3, 'client', sdk_client)
    standard = replace(cfg, aws_auth_mode='standard')
    app = production.create_production_app(standard, repository=repo, authenticator=ProductionAuth())
    with TestClient(app, base_url=cfg.public_url) as client:
        assert client.get('/api/live').status_code == 200
    assert [(service, kwargs['region_name']) for service, kwargs in calls] == [
        ('s3', cfg.region), ('kms', cfg.region)]
    assert all(not any(name.startswith('aws_') for name in kwargs) for _, kwargs in calls)
    assert all(kwargs['config'].retries['total_max_attempts'] == 2 for _, kwargs in calls)
    assert len(providers) == 1 and trace['sts'] == []
