"""Recovery waits stay bounded, cancellable and visible through the real API."""
import json
import time

import pytest

from server import media_sources as sources
from server.worker import LeaseClient
from test_commercial_api import commercial, run_claimed
from test_media_sources import Response, sample_mp4
from test_proxy_session_recovery import PROXY, SOURCE, setup_transport, run


def failure():
    return sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502)


def test_delayed_recovery_reaches_library_and_restores_phase(commercial, monkeypatch, sample_mp4, caplog):
    client, repo, _ = commercial
    attempts, requests = setup_transport(monkeypatch, [failure(), failure(), Response(sample_mp4)])
    monkeypatch.setenv('REPLAY_SOURCE_PROXY_URL', PROXY)
    phases = []
    original = LeaseClient.recovery
    def observe(lease, active):
        original(lease, active)
        item = client.get('/api/media').json()[0]
        phases.append((active, item['status'], item.get('recovering', False), item.get('error_code')))
        assert client.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []
        assert client.get('/api/usage').json()['storage_reserved_bytes'] > 0
    monkeypatch.setattr(LeaseClient, 'recovery', observe)
    response = client.post('/api/media/imports', json=SOURCE, headers={'Idempotency-Key': 'delayed-recovery'})
    assert response.status_code == 202
    started = time.monotonic()
    with caplog.at_level('INFO'):
        job = run_claimed(client)
    assert time.monotonic() - started >= 5
    assert len(attempts) == len(requests) == 3
    assert len({a.proxy for a in attempts}) == 3
    assert all(a.budget is attempts[0].budget for a in attempts)
    assert phases == [(True, 'importing', True, None), (False, 'importing', False, None)] * 2
    waits = [json.loads(r.message)['delay_seconds'] for r in caplog.records
             if r.name == sources.__name__ and 'source_recovery_wait' in r.message]
    assert waits == [0, 5]
    saved = client.get('/api/media').json()[0]
    assert saved['id'] == response.json()['media']['id'] and saved['status'] == 'ready'
    assert not saved.get('recovering') and saved['error_code'] is None
    preview = client.get('/api/media/' + saved['id'] + '/preview').json()
    assert client.get(preview['url']).content[4:8] == b'ftyp'
    assert client.get('/api/usage').json()['storage_reserved_bytes'] == 0
    assert repo.get_job('alpha', job['id'])['state'] == 'completed'


@pytest.mark.parametrize('interrupt', ['cancel', 'deadline'])
def test_delay_stops_before_a_third_connection(monkeypatch, tmp_path, interrupt):
    attempts, requests = setup_transport(monkeypatch, [failure(), failure()])
    calls = 0
    cancelled = RuntimeError('cancelled lease')
    stop = False
    def phase(active):
        nonlocal calls, stop
        if active:
            calls += 1
            if calls == 2:
                if interrupt == 'deadline':
                    attempts[0].budget.deadline = time.monotonic() - 1
                else:
                    stop = True
    def active():
        if stop:
            raise cancelled
    with pytest.raises((RuntimeError, sources.SourceImportError)) as caught:
        run(tmp_path / 'out.mp4', on_recovery=phase, check_active=active)
    if interrupt == 'cancel':
        assert caught.value is cancelled
    else:
        assert caught.value.code == 'SOURCE_TIMEOUT'
    assert len(attempts) == len(requests) == 2
    assert not list(tmp_path.iterdir())


def test_recovery_heartbeat_requires_lease_and_clears_on_terminal(commercial):
    client, repo, _ = commercial
    created = client.post('/api/media/imports', json=SOURCE, headers={'Idempotency-Key': 'phase-validation'})
    claim = client.post('/internal/claim', json={'worker_id': 'test', 'version': 'integration-test'},
                        headers={'Authorization': 'Bearer ' + 'c' * 32}).json()['job']
    headers = {'Authorization': 'Bearer ' + claim['callback_token']}
    url = '/internal/jobs/' + claim['id']
    assert client.post(url + '/heartbeat', json={'source_recovering': True}).status_code == 401
    assert client.post(url + '/heartbeat', json={'source_recovering': 'true'}, headers=headers).status_code == 422
    assert client.post(url + '/heartbeat', json={'source_recovering': True}, headers=headers).status_code == 200
    assert client.get('/api/media').json()[0]['recovering'] is True
    assert client.post(url + '/finish', json={'state': 'failed', 'error_code': 'SOURCE_PROXY_UNAVAILABLE'}, headers=headers).status_code == 200
    assert client.post(url + '/heartbeat', json={'source_recovering': True}, headers=headers).status_code == 409
    item = client.get('/api/media').json()[0]
    assert item['id'] == created.json()['media']['id'] and item['status'] == 'failed'
    assert not item.get('recovering') and item['error_code'] == 'SOURCE_PROXY_UNAVAILABLE'
    assert client.get('/api/usage').json()['storage_reserved_bytes'] == 0
