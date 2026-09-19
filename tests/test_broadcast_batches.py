"""Atomic multi-platform admission, tenant isolation and encrypted API destinations."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest
from sqlalchemy import delete, func, select

from server.output_policy import estimate_output_bytes
from server.repository import (BROADCAST_TARGETS, Conflict, NotFound, QuotaExceeded, Repository,
    RepositoryError, broadcast_batches, daily_usage, idempotency_records, job_events, jobs)
from server.stream_targets import STREAM_TARGETS
from test_commercial_api import commercial


@pytest.fixture
def repo(tmp_path):
    instance = Repository(f'sqlite:///{tmp_path}/batches.db', tenant_concurrency=4, create_schema=True)
    yield instance
    instance.close()


def media(repo, tenant='a'):
    return repo.add_media(tenant, name='synthetic.mp4', object_key=f'{tenant}/synthetic.mp4',
        bytes=1000, duration=15, width=1280, height=720, fps=30)


def batch(repo, item, key='batch-key', destinations=None, **kwargs):
    return repo.create_jobs_batch(item['tenant_id'], media_id=item['id'], title='Multi-platform',
        destinations=destinations or [{'target': 'local'}, {'target': 'youtube', 'secret_ciphertext': 'encrypted-youtube'},
            {'target': 'twitch', 'secret_ciphertext': 'encrypted-twitch'}], idempotency_key=key, **kwargs)


def counts(repo):
    with repo.engine.connect() as connection:
        return {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
                for table in (jobs, broadcast_batches, idempotency_records, daily_usage, job_events)}


def test_batch_admits_independent_jobs_and_replays_without_quota_or_event_changes(repo):
    item = media(repo)
    first = batch(repo, item)
    before = repo.usage('a'), counts(repo)
    second = batch(repo, item)
    assert first['replayed'] is False and second['replayed'] is True
    assert [row['id'] for row in first['jobs']] == [row['id'] for row in second['jobs']]
    assert before == (repo.usage('a'), counts(repo))
    assert {row['target'] for row in first['jobs']} == {'local', 'youtube', 'twitch'}
    assert len({row['scheduled'] for row in first['jobs']}) == 1
    assert repo.usage('a')['storage_reserved_bytes'] == estimate_output_bytes(15)
    claims = [repo.claim(f'worker-{i}') for i in range(3)]
    assert {row['id'] for row in claims} == {row['id'] for row in first['jobs']}
    assert len({row['lease_token'] for row in claims}) == 3
    assert BROADCAST_TARGETS == STREAM_TARGETS


@pytest.mark.parametrize('quota', ['pending', 'storage', 'runtime', 'capacity', 'bad_later_destination'])
def test_batch_failure_rolls_back_every_earlier_child_and_reservation(repo, quota):
    item = media(repo)
    destinations = [{'target': 'youtube', 'secret_ciphertext': 'opaque'}, {'target': 'local'}]
    if quota == 'pending':
        repo.max_pending_jobs = 1
    elif quota == 'storage':
        repo.max_storage_bytes = 1000
    elif quota == 'runtime':
        repo.max_daily_runtime_seconds = 60
    elif quota == 'capacity':
        repo.create_job('a', media_id=item['id'], title='Already scheduled', target='local', idempotency_key='prior')
        repo.tenant_concurrency = 2
    else:
        destinations[1] = {'target': 'twitch'}
    before = repo.usage('a'), counts(repo)
    with pytest.raises(RepositoryError):
        batch(repo, item, destinations=destinations)
    assert before == (repo.usage('a'), counts(repo))


def test_batch_rejects_duplicate_platforms_and_over_capacity(repo):
    item = media(repo)
    with pytest.raises(RepositoryError):
        batch(repo, item, destinations=[{'target': 'local'}, {'target': 'local'}])
    with pytest.raises(QuotaExceeded):
        batch(repo, item, destinations=[{'target': target, 'secret_ciphertext': 'opaque'}
            for target in ('youtube', 'twitch', 'facebook', 'instagram', 'tiktok')])
    assert repo.list_jobs('a') == []


def test_batch_idempotency_survives_completion_and_partial_history_deletion(repo):
    item = media(repo)
    first = batch(repo, item)
    for row in first['jobs']:
        repo.cancel('a', row['id'])
    assert batch(repo, item)['replayed'] is True
    with repo.engine.begin() as connection:
        connection.execute(delete(jobs).where(jobs.c.id == first['jobs'][0]['id']))
    before = counts(repo)
    with pytest.raises(Conflict, match='expired'):
        batch(repo, item)
    assert counts(repo) == before


def test_batch_rejects_changed_payload_and_single_group_key_collision(repo):
    item = media(repo)
    batch(repo, item)
    with pytest.raises(Conflict, match='different'):
        batch(repo, item, max_attempts=1)
    with pytest.raises(Conflict, match='batch'):
        repo.create_job('a', media_id=item['id'], title='Single', target='local', idempotency_key='batch-key')
    single = repo.create_job('a', media_id=item['id'], title='Single', target='local', idempotency_key='single-key')
    with pytest.raises(Conflict, match='single'):
        batch(repo, item, key='single-key')
    assert repo.get_job('a', single['id'])['state'] == 'scheduled'


def test_concurrent_batch_replays_across_instances_admit_exactly_one_group(repo):
    item = media(repo)
    other = Repository(str(repo.engine.url), tenant_concurrency=4)
    barrier = threading.Barrier(8)
    def submit(index):
        barrier.wait()
        return batch(repo if index % 2 else other, item)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        assert sum(not result['replayed'] for result in results) == 1
        assert len({tuple(row['id'] for row in result['jobs']) for result in results}) == 1
        assert counts(repo)['replay_jobs'] == 3
        assert counts(repo)['replay_broadcast_batches'] == 1
    finally:
        other.close()


def test_competing_batches_never_leave_partial_losing_group(repo):
    item = media(repo)
    barrier = threading.Barrier(2)
    def submit(key):
        barrier.wait()
        try:
            return batch(repo, item, key=key)
        except QuotaExceeded:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ('group-one', 'group-two')))
    assert sum(result is not None for result in results) == 1
    assert counts(repo)['replay_jobs'] == 3 and counts(repo)['replay_broadcast_batches'] == 1
    assert repo.usage('a')['storage_reserved_bytes'] == estimate_output_bytes(15)


def test_randomized_ciphertext_retry_uses_stable_request_fingerprint(repo):
    item = media(repo)
    first = batch(repo, item, destinations=[{'target': 'youtube', 'secret_ciphertext': 'randomized-one'}], request_fingerprint='a' * 64)
    repeated = batch(repo, item, destinations=[{'target': 'youtube', 'secret_ciphertext': 'randomized-two'}], request_fingerprint='a' * 64)
    assert repeated['replayed'] and first['jobs'][0]['id'] == repeated['jobs'][0]['id']


def test_batch_tenant_ownership_checked_before_commit(repo):
    item = media(repo)
    with pytest.raises(NotFound):
        repo.create_jobs_batch('b', media_id=item['id'], title='Other tenant', destinations=[{'target': 'local'}], idempotency_key='same-key')
    assert repo.list_jobs('a') == repo.list_jobs('b') == []


def api_payload(repo, objects):
    item = repo.add_media('alpha', name='batch.mp4', object_key=objects.key('alpha', 'batch'),
        bytes=1000, duration=15, width=1280, height=720, fps=30)
    return {'media_id': item['id'], 'title': '여러 플랫폼', 'destinations': [
        {'target': 'youtube', 'stream_key': 'private-youtube-secret'},
        {'target': 'custom', 'server_url': 'rtmps://broadcast.example.com:443/app', 'stream_key': 'private-custom-secret'},
        {'target': 'local'}]}


def test_api_batch_encrypts_endpoint_and_keys_and_only_claim_exposes_them(commercial, monkeypatch, caplog):
    client, repo, objects = commercial
    payload = api_payload(repo, objects)
    import server.production_app as module
    monkeypatch.setattr(module, 'pin_destination', lambda destination: {**destination, 'addresses': ['8.8.8.8']})
    headers = {'Idempotency-Key': 'api-batch-secret-test'}
    response = client.post('/api/broadcast-batches', json=payload, headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()['replayed'] is False
    replay = client.post('/api/broadcast-batches', json=payload, headers=headers)
    assert replay.status_code == 201 and replay.json()['replayed'] is True
    assert [row['id'] for row in response.json()['jobs']] == [row['id'] for row in replay.json()['jobs']]
    public = response.text + client.get('/api/broadcasts').text
    with repo.engine.connect() as connection:
        stored = '\n'.join(json.dumps(dict(row)) for row in connection.execute(select(jobs)).mappings())
        stored += str(connection.execute(select(broadcast_batches)).all())
    for forbidden in ('private-youtube-secret', 'private-custom-secret', 'broadcast.example.com'):
        assert forbidden not in public + stored + caplog.text
    claimed = []
    for index in range(3):
        result = client.post('/internal/claim', json={'worker_id': f'batch-test-{index}', 'version': 'integration-test'},
            headers={'Authorization': 'Bearer ' + 'c' * 32})
        assert result.status_code == 200, result.text
        claimed.append(result.json()['job'])
    custom = next(row for row in claimed if row['target'] == 'custom')
    assert custom['stream_destination']['server_url'] == 'rtmps://broadcast.example.com:443/app'
    assert custom['stream_destination']['stream_key'] == 'private-custom-secret'
    assert custom['stream_destination']['addresses'] == ['8.8.8.8']
    assert next(row for row in claimed if row['target'] == 'local')['stream_destination'] is None
    assert client.get('/api/broadcasts', headers={'Authorization': 'Bearer beta'}).json() == []


def test_api_metadata_auth_platforms_and_batch_input_security(commercial):
    client, repo, objects = commercial
    assert client.get('/api/stream-targets', headers={'Authorization': ''}).status_code == 401
    info = client.get('/api/stream-targets').json()
    assert {row['id'] for row in info['targets']} == STREAM_TARGETS
    assert info['max_destinations'] == 4
    payload = api_payload(repo, objects)
    headers = {'Idempotency-Key': 'api-batch-invalid'}
    for changes, code in [({'destinations': [{'target': 'local'}, {'target': 'local'}]}, 400),
        ({'destinations': [{'target': 'custom', 'server_url': 'rtmp://127.0.0.1/app', 'stream_key': 'secret'}]}, 400),
        ({'destinations': [{'target': 'custom', 'server_url': 'https://public.example/app', 'stream_key': 'secret'}]}, 400),
        ({'destinations': [{'target': 'validate'}]}, 422), ({'destinations': []}, 422)]:
        result = client.post('/api/broadcast-batches', json={**payload, **changes}, headers=headers)
        assert result.status_code == code, result.text
    assert client.post('/api/broadcast-batches', json=payload, headers={**headers, 'Authorization': 'Bearer viewer'}).status_code == 403
    assert client.post('/api/broadcast-batches', json=payload, headers={**headers, 'Authorization': 'Bearer beta'}).status_code == 404
    assert client.post('/api/broadcast-batches', json=payload).status_code == 400
    assert repo.list_jobs('alpha') == []


@pytest.mark.parametrize('alternate_split', [False, True])
def test_api_identical_effective_endpoint_rejected_even_across_platform_ids(commercial, alternate_split):
    client, repo, objects = commercial
    payload = api_payload(repo, objects)
    payload['destinations'] = [dict(target=target, server_url='rtmps://broadcast.example.com/app/', stream_key='same-secret')
                               for target in ('instagram', 'custom')]
    if alternate_split:
        payload['destinations'][1].update(server_url='rtmps://broadcast.example.com', stream_key='app/same-secret')
    result = client.post('/api/broadcast-batches', json=payload, headers={'Idempotency-Key': 'same-destination'})
    assert result.status_code == 400
    assert repo.list_jobs('alpha') == []


def test_api_single_custom_target_and_legacy_numeric_youtube_cipher(commercial, monkeypatch):
    from server.secrets import LocalKeyringProvider
    client, repo, objects = commercial
    payload = api_payload(repo, objects)
    single = {'media_id': payload['media_id'], 'title': 'Single custom', **payload['destinations'][1]}
    response = client.post('/api/broadcasts', json=single, headers={'Idempotency-Key': 'single-custom-target'})
    assert response.status_code == 201 and response.json()['target'] == 'custom'
    # The fake destination pin prevents all DNS/network calls during this test.
    import server.production_app as module
    monkeypatch.setattr(module, 'pin_destination', lambda destination: {**destination, 'addresses': ['8.8.8.8']})
    # Replace the decrypt closure's provider method with a valid legacy numeric
    # key; the repository continues to persist ciphertext only.
    decrypt = LocalKeyringProvider.decrypt
    monkeypatch.setattr(LocalKeyringProvider, 'decrypt', lambda self, ciphertext, context=None:
        '12345678' if ciphertext == 'legacy-encrypted' else decrypt(self, ciphertext, context))
    legacy = repo.create_job('alpha', media_id=payload['media_id'], title='Legacy YouTube', target='youtube',
        idempotency_key='legacy-numeric-key', secret_ciphertext='legacy-encrypted')
    claims = [client.post('/internal/claim', json={'worker_id': 'legacy-test', 'version': 'integration-test'},
        headers={'Authorization': 'Bearer ' + 'c' * 32}).json()['job'] for _ in range(2)]
    row = next(row for row in claims if row['id'] == legacy['id'])
    assert row['stream_destination']['stream_key'] == '12345678'
    assert row['stream_destination']['target'] == 'youtube'


def test_api_batch_encryption_failure_never_creates_partial_jobs(commercial, monkeypatch):
    from server.aws_identity import AWSIdentityError
    from server.secrets import LocalKeyringProvider
    client, repo, objects = commercial
    payload = api_payload(repo, objects)
    before = repo.usage('alpha'), counts(repo)
    original = LocalKeyringProvider.encrypt
    calls = []
    def encrypt(self, plaintext, context=None):
        calls.append(True)
        if len(calls) == 2:
            raise AWSIdentityError('CLOUD_IDENTITY_UNAVAILABLE')
        return original(self, plaintext, context)
    monkeypatch.setattr(LocalKeyringProvider, 'encrypt', encrypt)
    result = client.post('/api/broadcast-batches', json=payload, headers={'Idempotency-Key': 'batch-key-outage'})
    assert result.status_code == 503
    assert (repo.usage('alpha'), counts(repo)) == before
