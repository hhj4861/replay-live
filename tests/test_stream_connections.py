"""Account-owned stream-key persistence and explicit secret retrieval; no network."""
import hashlib
import json
import time

from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, inspect, select, update

from deploy.commercial.ops import check_schema, migrate, summary
from server.auth import Principal
from server.production_app import create_production_app
from server.repository import Repository
from server.secrets import LocalKeyringProvider, SecretError
from server.settings import Settings
from server.stream_connections import StreamConnections, connections


SECRET = 'synthetic-connection-key-canary'
SERVER = 'rtmps://ingest.example.com:443/app'


def principal(subject='alice', tenant='team-a', role='operator'):
    return Principal(subject, tenant, (role,), hashlib.sha256(f'{subject}:{tenant}:{role}'.encode()).hexdigest(), time.time() + 600)


class ConnectionAuth:
    async def authenticate(self, authorization, requested_tenant=None):
        users = {'Bearer alice': principal(), 'Bearer bob': principal('bob'),
                 'Bearer other-team': principal(tenant='team-b'),
                 'Bearer viewer': principal('reader', role='viewer'),
                 'Bearer admin': principal('admin', role='admin')}
        if authorization not in users:
            raise HTTPException(401, '로그인이 필요합니다.')
        user = users[authorization]
        if requested_tenant and requested_tenant != user.tenant_id:
            raise HTTPException(403, '소속을 확인하세요.')
        return user

    async def close(self):
        pass


@pytest.fixture
def api(tmp_path):
    cfg = Settings(mode='development', database_url=f'sqlite:///{tmp_path}/connections.db',
                   public_url='http://testserver', origins=('http://ui.test',),
                   control_token='c' * 32, callback_key='s' * 32, local_root=str(tmp_path), version='connection-tests')
    repo = Repository(cfg.database_url, create_schema=True)
    key = Fernet.generate_key()
    keys = LocalKeyringProvider({'test': key}, 'test', mode='test')
    app = create_production_app(cfg, repository=repo, keys=keys, authenticator=ConnectionAuth())
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    app.state.stream_connections.migrate()
    with TestClient(app, headers={'Authorization': 'Bearer alice', 'Origin': 'http://ui.test'}) as client:
        yield client, repo, keys, cfg, key


def test_save_list_explicit_use_replace_delete_and_no_store(api, caplog):
    client, repo, keys, _, _ = api
    caplog.set_level('INFO', logger='replay.requests')
    body = {'server_url': SERVER, 'stream_key': SECRET, 'channel_url': 'https://www.youtube.com/@example'}
    saved = client.put('/api/stream-connections/youtube', json=body)
    assert saved.status_code == 200
    assert saved.headers['cache-control'] == 'no-store'
    assert saved.json().keys() == {'target', 'server_url', 'channel_url', 'updated_at', 'has_stream_key'}
    assert saved.json()['has_stream_key'] is True
    listing = client.get('/api/stream-connections')
    assert listing.status_code == 200 and listing.json() == [saved.json()]
    assert listing.headers['cache-control'] == 'no-store'
    assert SECRET not in saved.text + listing.text
    with repo.engine.connect() as db:
        stored = dict(db.execute(select(connections)).mappings().one())
    assert all(value not in json.dumps(stored) for value in (SECRET, SERVER, body['channel_url']))
    with pytest.raises(SecretError):
        keys.decrypt(stored['secret_ciphertext'], {'tenant_id': 'team-a'})
    used = client.post('/api/stream-connections/youtube/use')
    assert used.status_code == 200 and used.headers['cache-control'] == 'no-store'
    assert used.json() == {'target': 'youtube', **body}
    replaced = client.put('/api/stream-connections/youtube', json={**body, 'stream_key': SECRET + '-new'})
    assert replaced.status_code == 200 and SECRET not in replaced.text
    assert client.post('/api/stream-connections/youtube/use').json()['stream_key'] == SECRET + '-new'
    assert len(client.get('/api/stream-connections').json()) == 1
    deleted = client.delete('/api/stream-connections/youtube')
    assert deleted.status_code == 204 and deleted.content == b''
    assert deleted.headers['cache-control'] == 'no-store'
    assert client.delete('/api/stream-connections/youtube').status_code == 204
    assert client.get('/api/stream-connections').json() == []
    missing = client.post('/api/stream-connections/youtube/use')
    assert missing.status_code == 404 and missing.headers['cache-control'] == 'no-store'
    assert SECRET not in caplog.text and SERVER not in caplog.text


def test_subject_and_tenant_ownership_and_writer_permissions(api):
    client, _, _, _, _ = api
    assert client.put('/api/stream-connections/youtube', json={'stream_key': SECRET}).status_code == 200
    for token in ('bob', 'other-team', 'admin'):
        headers = {'Authorization': 'Bearer ' + token}
        assert client.get('/api/stream-connections', headers=headers).json() == []
        assert client.post('/api/stream-connections/youtube/use', headers=headers).status_code == 404
        assert client.delete('/api/stream-connections/youtube', headers=headers).status_code == 204
        assert client.put('/api/stream-connections/youtube', json={'stream_key': token + '-own-key'}, headers=headers).status_code == 200
    assert client.post('/api/stream-connections/youtube/use').json()['stream_key'] == SECRET
    for token, status in (('viewer', 403), ('unknown', 401)):
        for method, path, body in [('GET', '/api/stream-connections', None),
                                  ('PUT', '/api/stream-connections/youtube', {'stream_key': SECRET}),
                                  ('POST', '/api/stream-connections/youtube/use', None),
                                  ('DELETE', '/api/stream-connections/youtube', None)]:
            response = client.request(method, path, json=body, headers={'Authorization': 'Bearer ' + token})
            assert response.status_code == status and response.headers['cache-control'] == 'no-store'
            assert SECRET not in response.text
    assert client.get('/api/stream-connections', headers={'X-Replay-Tenant': 'team-b'}).status_code == 403


@pytest.mark.parametrize('target,body,status', [
    ('local', {'stream_key': SECRET}, 400),
    ('unknown', {'stream_key': SECRET}, 400),
    ('youtube', {'stream_key': ''}, 422),
    ('youtube', {'stream_key': SECRET + '\n'}, 400),
    ('youtube', {'stream_key': SECRET, 'server_url': 'https://example.com'}, 400),
    ('youtube', {'stream_key': SECRET, 'server_url': 'rtmp://127.0.0.1/app'}, 400),
    ('youtube', {'stream_key': SECRET, 'server_url': 'rtmps://user:password@example.com/app'}, 400),
    ('youtube', {'stream_key': SECRET, 'tenant_id': 'team-b'}, 422),
    ('youtube', {'stream_key': SECRET, 'subject': 'bob'}, 422),
    ('youtube', {'stream_key': SECRET, 'broadcast_url': 'https://youtu.be/abcdefghijk'}, 422),
    ('youtube', {'stream_key': SECRET, 'channel_url': 'https://www.twitch.tv/example'}, 400),
    ('youtube', {'stream_key': SECRET * 40}, 422),
])
def test_invalid_input_is_masked_and_never_saved(api, target, body, status):
    client, _, _, _, _ = api
    response = client.put('/api/stream-connections/' + target, json=body)
    assert response.status_code == status
    assert response.headers['cache-control'] == 'no-store'
    assert SECRET not in response.text and 'user:password' not in response.text
    assert client.get('/api/stream-connections').json() == []


def test_oversized_malformed_and_cross_origin_requests_do_not_expose_keys(api):
    client, _, _, _, _ = api
    oversized = client.put('/api/stream-connections/youtube', content=SECRET * 400,
                           headers={'Content-Type': 'application/json'})
    assert oversized.status_code == 413 and oversized.headers['cache-control'] == 'no-store'
    malformed = client.put('/api/stream-connections/youtube', content='{"stream_key":"' + SECRET,
                           headers={'Content-Type': 'application/json'})
    assert malformed.status_code == 422 and SECRET not in malformed.text + oversized.text
    assert client.put('/api/stream-connections/youtube', json={'stream_key': SECRET},
                      headers={'Origin': 'http://other.test'}).status_code == 403
    assert client.get('/api/stream-connections').json() == []


def test_ciphertext_cannot_move_between_subject_tenant_or_target(api):
    client, repo, _, _, _ = api
    assert client.put('/api/stream-connections/youtube', json={'stream_key': SECRET}).status_code == 200
    with repo.engine.connect() as db:
        original = dict(db.execute(select(connections)).mappings().one())
    variants = [{'subject_hash': hashlib.sha256(b'bob').hexdigest()},
                {'tenant_id': 'team-b'}, {'target': 'twitch'}]
    for patch in variants:
        with repo.engine.begin() as db:
            db.execute(connections.insert().values(**{**original, **patch}))
    for token, target in [('bob', 'youtube'), ('other-team', 'youtube'), ('alice', 'twitch')]:
        response = client.post(f'/api/stream-connections/{target}/use', headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 503 and SECRET not in response.text
    assert client.post('/api/stream-connections/youtube/use').json()['stream_key'] == SECRET


def test_survives_new_repository_and_key_provider_instance(api):
    client, _, _, cfg, key = api
    assert client.put('/api/stream-connections/youtube', json={'stream_key': SECRET}).status_code == 200
    reopened = create_engine(cfg.database_url)
    try:
        service = StreamConnections(reopened, LocalKeyringProvider({'test': key}, 'test', mode='test'), mode='development')
        assert service.use(principal(), 'youtube')['stream_key'] == SECRET
        assert 'stream_key' not in service.list(principal())[0]
    finally:
        reopened.dispose()


def test_legacy_connection_payload_without_channel_url_remains_usable(api):
    client, repo, keys, _, _ = api
    assert client.put('/api/stream-connections/youtube', json={'stream_key': SECRET}).status_code == 200
    owner = {'tenant_id': 'team-a', 'subject_hash': hashlib.sha256(b'alice').hexdigest()}
    payload = {'target': 'youtube', 'server_url': SERVER, 'stream_key': SECRET}
    ciphertext = keys.encrypt(json.dumps(payload), {**owner, 'target': 'youtube', 'purpose': 'stream-connection-v1'})
    with repo.engine.begin() as db:
        db.execute(update(connections).values(secret_ciphertext=ciphertext))
    assert client.get('/api/stream-connections').json()[0]['channel_url'] == ''
    assert client.post('/api/stream-connections/youtube/use').json() == {**payload, 'channel_url': ''}


def test_corrupted_ciphertext_and_key_provider_failures_are_masked(api):
    client, repo, keys, _, _ = api
    assert client.put('/api/stream-connections/youtube', json={'stream_key': SECRET}).status_code == 200
    with repo.engine.begin() as db:
        db.execute(update(connections).values(secret_ciphertext=SECRET))
    for response in (client.get('/api/stream-connections'), client.post('/api/stream-connections/youtube/use')):
        assert response.status_code == 503 and SECRET not in response.text
        assert response.headers['cache-control'] == 'no-store'

    class FailingKeys:
        def encrypt(self, *args):
            raise SecretError(SECRET)

    service = StreamConnections(repo.engine, FailingKeys(), mode='test')
    with pytest.raises(HTTPException) as caught:
        service.save(principal(), 'youtube', server_url='', stream_key=SECRET)
    assert caught.value.status_code == 503 and SECRET not in str(caught.value)


def test_no_implicit_migration_and_explicit_release_schema(tmp_path):
    url = f'sqlite:///{tmp_path}/migration.db'
    engine = create_engine(url)
    try:
        keys = LocalKeyringProvider({'test': Fernet.generate_key()}, 'test', mode='test')
        service = StreamConnections(engine, keys, mode='development')
        for task in (lambda: service.list(principal()),
                     lambda: service.save(principal(), 'youtube', server_url='', stream_key=SECRET),
                     lambda: service.use(principal(), 'youtube'),
                     lambda: service.delete(principal(), 'youtube')):
            with pytest.raises(HTTPException) as caught:
                task()
            assert caught.value.status_code == 503 and SECRET not in str(caught.value)
        assert 'replay_stream_connections' not in inspect(engine).get_table_names()
        assert '007_stream_connections.sql' in migrate(url, development=True)['versions']
        assert migrate(url, development=True)['migrated'] is True
        service.save(principal(), 'youtube', server_url='', stream_key=SECRET)
        with engine.connect() as db:
            assert check_schema(db)
            assert summary(db)['table_counts']['replay_stream_connections'] == 1
        with pytest.raises(ValueError, match='require PostgreSQL'):
            StreamConnections(engine, keys, mode='production')
        with pytest.raises(HTTPException) as caught:
            service.use(principal(role='viewer'), 'youtube')
        assert caught.value.status_code == 403
    finally:
        engine.dispose()
