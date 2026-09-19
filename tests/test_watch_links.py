"""Viewing metadata parity, source immutability and explicit migration; no streams."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import uuid

import pytest
from sqlalchemy import create_engine, inspect, make_url, text
from sqlalchemy.exc import SQLAlchemyError

from deploy.commercial.ops import check_schema, migrate
from server.repository import Conflict, Repository
from server.watch_links import WatchLinkError, normalize_watch_url
from test_commercial_api import commercial  # noqa: F401: existing isolated API fixture
from test_postgres import postgres  # noqa: F401: explicit disposable PostgreSQL fixture


CASES = json.loads((Path(__file__).resolve().parents[1] / 'web/tests/fixtures/watch-links.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=[f"{case['target']}-{case['kind']}-{index}" for index, case in enumerate(CASES)])
def test_shared_watch_link_contract(case, monkeypatch):
    import socket
    def no_network(*args, **kwargs):
        raise AssertionError('Viewing links never resolve or fetch an external host')
    monkeypatch.setattr(socket, 'getaddrinfo', no_network)
    if case['expected'] is None:
        with pytest.raises(WatchLinkError) as caught:
            normalize_watch_url(case['target'], case['input'], case['kind'])
        assert str(caught.value) == str(WatchLinkError())
    else:
        actual = normalize_watch_url(case['target'], case['input'], case['kind'])
        assert actual == case['expected']
        assert normalize_watch_url(case['target'], actual, case['kind']) == actual


def media(repo, tenant='alpha', name='original-17s.mp4', duration=17):
    return repo.add_media(tenant, name=name, object_key=f'replay/{hashlib.sha256(tenant.encode()).hexdigest()}/media/{name}', bytes=100,
        sha256=hashlib.sha256((name.encode() * 100)[:100]).hexdigest(),
        status='ready', duration=duration, width=640, height=360, fps=30)


def test_history_source_and_links_are_tenant_scoped_without_changing_the_worker_input(commercial, monkeypatch):
    from server import stream_targets
    monkeypatch.setattr(stream_targets, '_resolve', lambda host, port: ['8.8.8.8'])
    client, repo, objects = commercial
    original = media(repo)
    objects.put_bytes('alpha', original['object_key'], (original['name'].encode() * 100)[:100],
                      size=original['bytes'], sha256=original['sha256'])
    media(repo, name='newer-60s.mp4', duration=60)
    body = {'media_id': original['id'], 'title': 'Original broadcast', 'target': 'twitch',
            'stream_key': 'synthetic-stream-key', 'channel_url': 'https://twitch.tv/example/',
            'broadcast_url': 'https://www.twitch.tv/videos/123'}
    created = client.post('/api/broadcasts', json=body, headers={'Idempotency-Key': 'watch-link-single'})
    assert created.status_code == 201, created.text
    row = created.json()
    assert row['media_id'] == original['id'] and row['media_name'] == original['name'] and row['duration'] == 17
    assert row['channel_url'] == 'https://www.twitch.tv/example'
    path = '/api/broadcasts/' + row['id'] + '/watch-links'
    links = {'channel_url': row['channel_url'], 'broadcast_url': 'https://www.twitch.tv/videos/456'}
    for token, status in [('viewer', 403), ('beta', 404)]:
        assert client.put(path, json=links, headers={'Authorization': 'Bearer ' + token}).status_code == status
    assert client.put(path, json={**links, 'media_id': 'different-source'}).status_code == 422
    invalid = client.put(path, json={**links, 'broadcast_url': 'https://www.twitch.tv/videos/123?token=synthetic-secret'})
    assert invalid.status_code == 400 and invalid.json()['code'] == 'WATCH_LINK_INVALID'
    assert 'synthetic-secret' not in invalid.text
    changed = client.put(path, json=links)
    assert changed.status_code == 200 and changed.headers['cache-control'] == 'no-store'
    for key in ('media_id', 'media_name', 'title', 'target', 'state', 'scheduled', 'updated', 'duration'):
        assert changed.json()[key] == row[key]
    assert changed.json()['broadcast_url'] == links['broadcast_url']
    assert client.get('/api/broadcasts').json()[0]['broadcast_url'] == links['broadcast_url']
    replay = client.post('/api/broadcasts', json=body, headers={'Idempotency-Key': 'watch-link-single'})
    assert replay.status_code == 201 and replay.json()['id'] == row['id'] and replay.json()['replayed']
    different = client.post('/api/broadcasts', json={**body, 'broadcast_url': links['broadcast_url']},
                            headers={'Idempotency-Key': 'watch-link-single'})
    assert different.status_code == 409
    assert client.put(path, json={}).json()['broadcast_url'] == ''
    response = client.post('/internal/claim', json={'worker_id': 'watch-link-check', 'version': 'integration-test'},
                           headers={'Authorization': 'Bearer ' + 'c' * 32})
    assert response.status_code == 200, response.text
    claimed = response.json()['job']
    assert claimed['id'] == row['id'] and claimed['duration'] == 17 and claimed['sha256'] == original['sha256']
    assert original['name'] in claimed['input']['url']
    assert 'broadcast_url' not in claimed and 'channel_url' not in claimed


def test_batch_links_are_per_destination_and_invalid_input_is_atomic(commercial):
    client, repo, _ = commercial
    repo.tenant_concurrency = repo.global_concurrency = 2
    original = media(repo)
    body = {'media_id': original['id'], 'title': 'Two channels', 'destinations': [
        {'target': 'youtube', 'stream_key': 'synthetic-youtube', 'channel_url': 'https://youtube.com/@example',
         'broadcast_url': 'https://youtu.be/abcdefghijk'},
        {'target': 'twitch', 'stream_key': 'synthetic-twitch', 'channel_url': 'https://twitch.tv/example',
         'broadcast_url': 'https://twitch.tv/videos/123'}]}
    invalid = {**body, 'destinations': [body['destinations'][0], {**body['destinations'][1], 'broadcast_url': 'https://evil.example/video'}]}
    assert client.post('/api/broadcast-batches', json=invalid, headers={'Idempotency-Key': 'watch-link-batch'}).status_code == 400
    assert repo.list_jobs('alpha') == [] and repo.usage('alpha')['pending_jobs'] == 0
    response = client.post('/api/broadcast-batches', json=body, headers={'Idempotency-Key': 'watch-link-batch'})
    assert response.status_code == 201, response.text
    rows = response.json()['jobs']
    assert [row['broadcast_url'] for row in rows] == ['https://www.youtube.com/watch?v=abcdefghijk', 'https://www.twitch.tv/videos/123']
    assert all(row['media_id'] == original['id'] and row['media_name'] == original['name'] for row in rows)
    replay = client.post('/api/broadcast-batches', json=body, headers={'Idempotency-Key': 'watch-link-batch'})
    assert replay.status_code == 201 and replay.json()['replayed']
    assert [row['id'] for row in replay.json()['jobs']] == [row['id'] for row in rows]
    body['destinations'][0]['channel_url'] = 'https://youtube.com/@different'
    assert client.post('/api/broadcast-batches', json=body, headers={'Idempotency-Key': 'watch-link-batch'}).status_code == 409


@pytest.mark.parametrize('batch', [False, True])
def test_empty_links_preserve_pre_upgrade_api_idempotency(commercial, batch):
    client, repo, _ = commercial
    original = media(repo)
    destination = {'target': 'twitch', 'server_url': '', 'stream_key': 'synthetic-key'}
    common = {'media_id': original['id'], 'title': 'Existing request', 'scheduled_at': None, 'max_attempts': 3}
    old = {**common, 'destinations': [destination]} if batch else {**destination, **common}
    fingerprint = hmac.new(b's' * 32, json.dumps(old, ensure_ascii=False, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
    args = dict(media_id=original['id'], title=common['title'], idempotency_key='pre-links-request', request_fingerprint=fingerprint)
    if batch:
        saved = repo.create_jobs_batch('alpha', **args, destinations=[{'target': 'twitch', 'secret_ciphertext': 'opaque'}])['jobs'][0]
    else:
        saved = repo.create_job('alpha', **args, target='twitch', secret_ciphertext='opaque')
    response = client.post('/api/broadcast-batches' if batch else '/api/broadcasts', json=old,
                           headers={'Idempotency-Key': 'pre-links-request'})
    assert response.status_code == 201, response.text
    assert response.json()['replayed']
    assert (response.json()['jobs'][0] if batch else response.json())['id'] == saved['id']


def test_legacy_sqlite_has_no_implicit_alter_and_explicit_upgrade_preserves_history(tmp_path):
    url = f'sqlite:///{tmp_path}/legacy-watch-links.db'
    repo = Repository(url, create_schema=True)
    original = media(repo)
    saved = repo.create_job('alpha', media_id=original['id'], title='Legacy history', target='local', idempotency_key='legacy-watch-links')
    repo.cancel('alpha', saved['id'])
    before = repo.get_job('alpha', saved['id'])
    with repo.engine.begin() as db:
        db.exec_driver_sql('ALTER TABLE replay_jobs DROP COLUMN channel_url')
        db.exec_driver_sql('ALTER TABLE replay_jobs DROP COLUMN broadcast_url')
    repo.close()
    reopened = Repository(url, create_schema=True)
    try:
        assert 'channel_url' not in {column['name'] for column in inspect(reopened.engine).get_columns('replay_jobs')}
        with pytest.raises(SQLAlchemyError):
            reopened.get_job('alpha', saved['id'])
        reopened.migrate_watch_links()
        reopened.migrate_watch_links()
        assert reopened.get_job('alpha', saved['id']) == before
        assert '008_watch_links.sql' in migrate(url, development=True)['versions']
        with reopened.engine.connect() as db:
            assert check_schema(db)
    finally:
        reopened.close()


@pytest.mark.skipif(not os.environ.get('REPLAY_TEST_POSTGRES_URL'), reason='Explicit disposable PostgreSQL URL is required')
def test_postgres_watch_links_survive_reopen_and_do_not_rebind_source(postgres):
    repo, _, url = postgres
    original = media(repo)
    args = dict(media_id=original['id'], title='PostgreSQL history', target='twitch',
                secret_ciphertext='opaque', idempotency_key='pg-watch-links', channel_url='https://twitch.tv/example')
    saved = repo.create_job('alpha', **args)
    reopened = Repository(url)
    try:
        updated = reopened.update_watch_links('alpha', saved['id'], broadcast_url='https://twitch.tv/videos/123')
        assert updated['media_id'] == original['id'] and updated['media_name'] == original['name']
        assert repo.get_job('alpha', saved['id'])['broadcast_url'] == 'https://www.twitch.tv/videos/123'
        assert reopened.create_job('alpha', **args)['id'] == saved['id']
        with pytest.raises(Conflict):
            reopened.create_job('alpha', **{**args, 'channel_url': 'https://twitch.tv/changed'})
    finally:
        reopened.close()


@pytest.mark.skipif(not os.environ.get('REPLAY_TEST_POSTGRES_URL'), reason='Explicit disposable PostgreSQL URL is required')
def test_postgres_008_upgrades_existing_007_history(postgres):
    owner, _, url = postgres
    schema = 'legacy_' + uuid.uuid4().hex
    with owner.engine.begin() as db:
        db.execute(text(f'CREATE SCHEMA "{schema}"'))
    legacy_url = make_url(url).update_query_dict({'options': f'-csearch_path={schema}'})
    engine = create_engine(legacy_url)
    reopened = None
    try:
        with engine.begin() as db:
            paths = sorted((Path(__file__).resolve().parents[1] / 'migrations').glob('*.sql'))
            for path in paths:
                if path.name < '008_watch_links.sql':
                    db.exec_driver_sql(path.read_text())
            now = time.time()
            db.execute(text("INSERT INTO replay_media (id,tenant_id,name,object_key,bytes,duration,width,height,fps,sha256,etag,status,created,updated) "
                "VALUES ('legacy-media','alpha','legacy-original.mp4','legacy/media.mp4',100,17,640,360,30,:sha,'','ready',:now,:now)"),
                {'sha': 'a' * 64, 'now': now})
            values = dict(id='legacy-job', tenant_id='alpha', media_id='legacy-media', title='Existing history', target='twitch',
                idempotency_key='legacy-request', payload_hash='a' * 64, scheduled=now,
                reserved_until=now + 600, deadline=now + 600, state='completed', progress=17, attempt=1,
                max_attempts=1, next_run=now, lease_version=1, cancel_requested=False, output_bytes=0, created=now, updated=now)
            db.execute(text('INSERT INTO replay_jobs (' + ','.join(values) + ') VALUES (' + ','.join(':' + key for key in values) + ')'), values)
            assert 'channel_url' not in {column['name'] for column in inspect(db).get_columns('replay_jobs')}
            db.exec_driver_sql(next(path for path in paths if path.name == '008_watch_links.sql').read_text())
        reopened = Repository(legacy_url.render_as_string(hide_password=False))
        before = reopened.get_job('alpha', 'legacy-job')
        assert before['channel_url'] == before['broadcast_url'] == ''
        assert before['media_name'] == 'legacy-original.mp4'
        after = reopened.update_watch_links('alpha', 'legacy-job', channel_url='https://twitch.tv/example',
                                           broadcast_url='https://twitch.tv/videos/123')
        assert after['channel_url'] == 'https://www.twitch.tv/example'
        assert after['broadcast_url'] == 'https://www.twitch.tv/videos/123'
        assert {key: value for key, value in after.items() if key not in ('channel_url', 'broadcast_url')} == {
            key: value for key, value in before.items() if key not in ('channel_url', 'broadcast_url')}
    finally:
        if reopened:
            reopened.close()
        engine.dispose()
        with owner.engine.begin() as db:
            db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
