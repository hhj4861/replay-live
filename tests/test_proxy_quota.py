from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine

from server.proxy_quota import ProxyQuota
from test_commercial_api import commercial


@pytest.fixture
def quota(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'quota.db'))
    now = [1_800_000_000.0]
    value = ProxyQuota(engine, clock=lambda: now[0])
    value.migrate()
    yield value, now
    engine.dispose()


def test_thresholds_deduplicate_and_rearm_only_after_topup(quota):
    q, now = quota
    assert not q.observe(5_000_000_000, now[0])
    now[0] += 1
    assert q.observe(900_000_000, now[0])
    alert = q.claim()
    assert alert['threshold_bytes'] == 1_000_000_000
    assert q.claim() is None
    assert q.acknowledge(alert['id'], alert['lease_token'])
    now[0] += 1
    assert not q.observe(800_000_000, now[0])
    now[0] += 1
    assert q.observe(200_000_000, now[0])
    alert = q.claim()
    assert alert['threshold_bytes'] == 250_000_000
    assert q.acknowledge(alert['id'], alert['lease_token'])
    now[0] += 1
    assert not q.observe(0, now[0])
    now[0] += 1
    assert not q.observe(5_000_000_000, now[0])
    now[0] += 1
    assert q.observe(900_000_000, now[0])


def test_late_observations_and_parallel_deliveries_cannot_repeat_alert(quota):
    q, now = quota
    q.observe(200_000_000, now[0])
    assert not q.observe(5_000_000_000, now[0] - 1)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = [v for v in pool.map(lambda _: q.claim(), range(8)) if v]
    assert len(claimed) == 1 and claimed[0]['threshold_bytes'] == q.CRITICAL
    alert = claimed[0]
    now[0] += 301
    assert not q.acknowledge(alert['id'], alert['lease_token'])
    retry = q.claim()
    assert retry['id'] == alert['id'] and retry['lease_token'] != alert['lease_token']
    assert q.acknowledge(retry['id'], retry['lease_token'])


def test_refill_retires_pending_alert_and_invalid_values_are_rejected(quota):
    q, now = quota
    q.observe(0, now[0])
    now[0] += 1
    q.observe(5_000_000_000, now[0])
    assert q.claim() is None
    for value in (-1, True, float('nan'), '0'):
        with pytest.raises(ValueError): q.observe(value, now[0])
    with pytest.raises(ValueError): q.observe(0, now[0] - 301)


def test_quota_control_flow_rejects_user_tokens_and_deduplicates_delivery(commercial):
    import time
    client, _, _ = commercial
    client.app.state.proxy_quota.migrate()
    payload = {'remaining_bytes': 200_000_000, 'observed_at': time.time()}
    for token in ('alpha', 'beta', 'viewer'):
        headers = {'Authorization': 'Bearer ' + token}
        assert client.post('/internal/proxy-balance', json=payload, headers=headers).status_code == 401
        assert client.post('/internal/proxy-alerts/claim', json={}, headers=headers).status_code == 401
    headers = {'Authorization': 'Bearer ' + 'c' * 32}
    assert client.post('/internal/proxy-balance', json=payload, headers=headers).json()['alert_created']
    assert not client.post('/internal/proxy-balance', json=payload, headers=headers).json()['alert_created']
    alert = client.post('/internal/proxy-alerts/claim', json={}, headers=headers).json()['alert']
    ack = {'id': alert['id'], 'lease_token': alert['lease_token']}
    assert client.post('/internal/proxy-alerts/ack', json=ack).status_code == 401
    assert client.post('/internal/proxy-alerts/ack', json=ack, headers=headers).json()['acknowledged']
    assert client.post('/internal/proxy-alerts/claim', json={}, headers=headers).json()['alert'] is None
    assert client.post('/internal/proxy-balance', json={**payload, 'remaining_bytes': True}, headers=headers).status_code == 422
