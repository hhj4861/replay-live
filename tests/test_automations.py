"""Durable scheduling, account isolation and real local media preparation."""
from datetime import datetime
import json
import os

import pytest
from sqlalchemy import select, update

from server.auth import Principal
from server.automations import Automations, next_occurrence, runs, schedules
from server.repository import jobs, media, LeaseLost
from test_commercial_api import commercial, prepare, run_claimed
from test_poc import clip
from test_postgres import postgres


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv('REPLAY_AUTOMATIONS_ENABLED', '1')


@pytest.fixture
def automatic(enabled, commercial, clip):
    client, repo, objects = commercial
    app = client.app
    service = app.state.automations
    service.migrate()
    app.state.stream_connections.migrate()
    user = Principal('alpha', 'alpha', ('operator',))
    app.state.stream_connections.save(user, 'youtube', server_url='', stream_key='synthetic-test-key')
    intent = prepare(client, clip)
    run_claimed(client)  # Real FFmpeg validation through API, local signed storage and worker callbacks.
    now = [datetime.fromisoformat('2026-09-28T09:00:00+09:00').timestamp()]
    service.clock = lambda: now[0]
    payload = {'name': '매일 신제품', 'source': 'media', 'media_id': intent['media']['id'],
               'targets': ['youtube'], 'time': '18:00', 'weekdays': list(range(7)), 'timezone': 'Asia/Seoul'}
    return client, service, user, now, payload


def advance(service, now, count=1):
    for _ in range(count):
        now[0] += 2
        service.tick()


def create_due(automatic, *, youtube=False, monkeypatch=None):
    client, service, user, now, payload = automatic
    if youtube:
        monkeypatch.setattr(service.channel, 'status', lambda _: {'connected': True})
        monkeypatch.setattr(service.channel, 'latest', lambda _: {'id': 'GcOe4ILS6Ow'})
        payload = {**payload, 'source': 'youtube_latest', 'media_id': None}
    response = client.post('/api/automations', json=payload)
    assert response.status_code == 201, response.text
    rule = response.json()
    now[0] = rule['next_run']
    return rule


def test_api_auth_ownership_pause_reload_and_delete(automatic):
    client, service, user, now, payload = automatic
    rule = create_due(automatic)
    assert 'synthetic-test-key' not in client.get('/api/automations').text
    assert client.get('/api/automations', headers={'Authorization': 'Bearer beta'}).json()['items'] == []
    assert client.put('/api/automations/' + rule['id'], json={'enabled': False},
                      headers={'Authorization': 'Bearer beta'}).status_code == 404
    assert client.get('/api/automations', headers={'Authorization': 'Bearer viewer'}).status_code == 403
    assert client.post('/internal/automations/tick').status_code == 401
    # Same tenant but a different account cannot manage the owner's rule.
    assert service.list(Principal('another-person', 'alpha', ('operator',))) == []
    assert client.put('/api/automations/' + rule['id'], json={'enabled': 'false'}).status_code == 422
    assert client.put('/api/automations/' + rule['id'], json={'enabled': False}).status_code == 200
    advance(service, now)
    assert service.list(user)[0]['history'] == []
    restarted = Automations(service.repo, service.keys, service.connections, service.channel,
                            service.objects, service.cfg, clock=service.clock)
    assert restarted.list(user)[0]['enabled'] is False
    assert client.put('/api/automations/' + rule['id'], json={'enabled': True}).status_code == 200
    assert service.list(user)[0]['next_run'] > now[0]
    assert client.delete('/api/automations/' + rule['id']).status_code == 200
    assert service.list(user) == []


def test_real_media_to_saved_destination_and_single_dispatch_on_restart(automatic, monkeypatch):
    client, service, user, now, payload = automatic
    rule = create_due(automatic)
    advance(service, now)  # checking -> ready
    original = service.record
    crashed = [False]
    def crash_after_admission(rule, **values):
        if values.get('state') == 'broadcasting' and not crashed[0]:
            crashed[0] = True
            raise RuntimeError('synthetic process interruption')
        return original(rule, **values)
    monkeypatch.setattr(service, 'record', crash_after_admission)
    with pytest.raises(RuntimeError):
        advance(service, now)
    monkeypatch.setattr(service, 'record', original)
    advance(service, now)
    broadcasts = [j for j in service.repo.list_jobs('alpha') if j['target'] == 'youtube']
    assert len(broadcasts) == 1
    assert broadcasts[0]['media_id'] == payload['media_id']
    # Pausing an active rule retains observation of the current run, not a new broadcast.
    assert client.put('/api/automations/' + rule['id'], json={'enabled': False}).status_code == 200
    assert client.delete('/api/automations/' + rule['id']).status_code == 409
    with service.repo._transaction() as connection:
        connection.execute(update(jobs).where(jobs.c.id == broadcasts[0]['id']).values(state='completed'))
    advance(service, now)
    assert service.list(user)[0]['history'][0]['state'] == 'completed'
    assert len([j for j in service.repo.list_jobs('alpha') if j['target'] == 'youtube']) == 1


def test_latest_video_import_then_broadcast_and_skip_unchanged(automatic, monkeypatch):
    client, service, user, now, payload = automatic
    create_due(automatic, youtube=True, monkeypatch=monkeypatch)
    advance(service, now, 2)
    row = service.list(user)[0]
    run = row['history'][0]
    assert run['state'] == 'importing'
    # Downloader transport is stubbed; validate and import reservation APIs remain real.
    # Reuse already validated local MP4 metadata for deterministic offline verification.
    original_media = service.repo.get_media(user.tenant_id, payload['media_id'])
    with service.repo._transaction() as connection:
        imported = connection.execute(select(jobs).where(jobs.c.media_id == run['media_id'])).mappings().one()
        decoded = json.loads(service.keys.decrypt(imported['secret_ciphertext'], {'tenant_id': user.tenant_id}))
        assert decoded == {'provider': 'youtube', 'url': 'https://www.youtube.com/watch?v=GcOe4ILS6Ow', 'use_original_title': True}
        connection.execute(update(jobs).where(jobs.c.id == imported['id']).values(state='completed'))
        connection.execute(update(media).where(media.c.id == run['media_id']).values(status='ready',
            name='내 최신 영상 제목', bytes=original_media['bytes'], duration=original_media['duration']))
    advance(service, now, 2)
    broadcasts = [j for j in service.repo.list_jobs(user.tenant_id) if j['target'] == 'youtube']
    assert len(broadcasts) == 1 and broadcasts[0]['title'] == '내 최신 영상 제목'
    with service.repo._transaction() as connection:
        connection.execute(update(jobs).where(jobs.c.id == broadcasts[0]['id']).values(state='completed'))
    advance(service, now)
    now[0] = service.list(user)[0]['next_run']
    advance(service, now)
    history = service.list(user)[0]['history']
    assert [r['state'] for r in history] == ['skipped', 'completed']
    assert history[0]['error_code'] == 'NO_NEW_VIDEO'
    assert len([j for j in service.repo.list_jobs(user.tenant_id) if j['target'] == 'import']) == 1


def test_failed_import_never_broadcasts_and_disconnected_target_reports_failure(automatic, monkeypatch):
    client, service, user, now, payload = automatic
    create_due(automatic, youtube=True, monkeypatch=monkeypatch)
    advance(service, now, 2)
    run = service.list(user)[0]['history'][0]
    with service.repo._transaction() as connection:
        connection.execute(update(media).where(media.c.id == run['media_id']).values(status='failed', error_code='SOURCE_FETCH_TIMEOUT'))
    advance(service, now)
    assert service.list(user)[0]['history'][0]['state'] == 'failed'
    assert not any(j['target'] == 'youtube' for j in service.repo.list_jobs(user.tenant_id))
    client.post('/api/automations', json=payload)
    now[0] = service.list(user)[0]['next_run']
    service.connections.delete(user, 'youtube')
    advance(service, now, 2)
    assert any(r['history'] and r['history'][0]['error_code'] == 'CONNECTION_REQUIRED' for r in service.list(user))


def test_lease_fencing_and_disabled_member(automatic):
    client, service, user, now, payload = automatic
    create_due(automatic)
    first = service.claim()
    assert service.claim() is None
    now[0] += 121
    second = service.claim()
    assert second['active_run'] == first['active_run']
    with pytest.raises(LeaseLost):
        service.record(first, state='failed')
    service.record(second, state='skipped')
    now[0] = service.list(user)[0]['next_run'] + 121
    from fastapi import HTTPException
    def disabled(*args):
        raise HTTPException(403, 'Account disabled')
    service.repo.admission_guard = disabled
    advance(service, now)
    assert service.list(user)[0]['history'][0]['state'] == 'failed'


def test_recurrence_timezone_and_dst():
    config = {'timezone': 'Asia/Seoul', 'weekdays': [0], 'time': '18:00'}
    now = datetime.fromisoformat('2026-09-28T18:00:00+09:00').timestamp()
    assert next_occurrence(config, now) == now + 7*86400
    spring = {'timezone': 'America/New_York', 'weekdays': list(range(7)), 'time': '02:30'}
    expected = datetime.fromisoformat('2026-03-09T02:30:00-04:00').timestamp()
    assert next_occurrence(spring, datetime.fromisoformat('2026-03-08T00:00:00-05:00').timestamp()) == expected
    weekly = {**spring, 'weekdays': [6]}
    assert next_occurrence(weekly, datetime.fromisoformat('2026-03-02T00:00:00-05:00').timestamp()) == datetime.fromisoformat('2026-03-15T02:30:00-04:00').timestamp()
    fall = {**spring, 'time': '01:30'}
    first = datetime.fromisoformat('2026-11-01T01:30:00-04:00').timestamp()
    assert next_occurrence(fall, first) == datetime.fromisoformat('2026-11-02T01:30:00-05:00').timestamp()


def test_default_off_has_no_schema_requirement(commercial):
    client, repo, objects = commercial
    assert client.get('/api/automations').json() == {'enabled': False, 'items': []}
    assert client.post('/api/automations', json={}).status_code == 404


@pytest.mark.skipif(not os.environ.get('REPLAY_TEST_POSTGRES_URL'), reason='Disposable PostgreSQL required')
def test_postgres_two_coordinators_only_claim_one_durable_run(postgres, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from cryptography.fernet import Fernet
    from server.secrets import LocalKeyringProvider
    from server.settings import Settings
    from server.storage import LocalStorage
    from server.stream_connections import StreamConnections
    from server.youtube_channel import YouTubeChannel
    repo, policy, url = postgres
    keys = LocalKeyringProvider({'test': Fernet.generate_key()}, 'test', mode='development')
    connections = StreamConnections(repo.engine, keys)
    user = Principal('owner', 'automation-tenant', ('operator',))
    connections.save(user, 'youtube', server_url='', stream_key='synthetic-key')
    objects = LocalStorage(tmp_path, signing_key='s'*32, base_url='http://testserver', allow_development=True)
    cfg = Settings(mode='development', database_url=url, public_url='http://testserver',
                   control_token='c'*32, callback_key='s'*32, origins=('http://ui.test',))
    channel = YouTubeChannel(repo, keys, 'http://testserver/api/youtube-channel/callback')
    try:
        service = Automations(repo, keys, connections, channel, objects, cfg)
        source = repo.add_media(user.tenant_id, name='source.mp4', object_key=objects.key(user.tenant_id, 'source'),
                                bytes=1000, duration=10, width=640, height=360, fps=30, status='ready')
        rule = service.create(user, {'name': 'concurrency', 'source': 'media', 'media_id': source['id'],
            'targets': ['youtube'], 'time': '18:00', 'weekdays': [0], 'timezone': 'Asia/Seoul'})
        with repo._transaction() as connection:
            connection.execute(update(schedules).where(schedules.c.id == rule['id']).values(next_run=0))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: service.claim(), range(2)))
        assert len([claim for claim in claims if claim]) == 1
        with repo.engine.connect() as connection:
            assert len(connection.execute(select(runs).where(runs.c.schedule_id == rule['id'])).all()) == 1
    finally:
        channel.http.close()
