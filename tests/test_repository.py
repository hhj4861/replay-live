"""Durable ownership/capacity tests using real SQLite transactions and threads."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import uuid

import pytest
from sqlalchemy import func, select, update

from server.output_policy import estimate_output_bytes
from server.repository import (Conflict, LeaseLost, NotFound, QuotaExceeded,
                               Repository, RepositoryError, job_events, jobs)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repo(tmp_path, clock):
    result = Repository(f'sqlite:///{tmp_path}/replay.db', clock=clock, runtime_allowance=0,
                        validation_duration=120, create_schema=True)
    yield result
    result.close()


def add_media(repo, tenant='a', **kwargs):
    token = kwargs.pop('id', uuid.uuid4().hex)
    # Callers supply IDs for repeated media in a tenant.
    return repo.add_media(tenant, id=token, name='sample.mp4', object_key=f'replay/{tenant}/media/{token}.mp4',
        bytes=kwargs.pop('bytes', 1000), duration=kwargs.pop('duration', 15),
        width=1280, height=720, fps=30, **kwargs)


def make_job(repo, item, key='request-1', **kwargs):
    return repo.create_job(item['tenant_id'], media_id=item['id'], title='Test broadcast',
        target=kwargs.pop('target', 'local'), idempotency_key=key, **kwargs)


def test_tenant_scope_hides_media_jobs_events_and_ownership(repo):
    item = add_media(repo)
    job = make_job(repo, item, target='youtube', secret_ciphertext='encrypted-not-plaintext')
    assert repo.list_media('b') == []
    assert repo.list_jobs('b') == []
    for method, args in [(repo.get_media, ('b', item['id'])), (repo.get_job, ('b', job['id'])),
                         (repo.events, ('b', job['id'])), (repo.cancel, ('b', job['id']))]:
        with pytest.raises(NotFound):
            method(*args)
    with pytest.raises(NotFound):
        repo.create_job('b', media_id=item['id'], title='Other', target='local', idempotency_key='x')
    claim = repo.claim('worker')
    assert claim['secret_ciphertext'] == 'encrypted-not-plaintext'
    for public in [job, repo.get_job('a', job['id']), repo.list_jobs('a')[0]]:
        assert 'lease_token' not in public
        assert 'secret_ciphertext' not in public
        assert 'payload_hash' not in public


def test_idempotency_retry_with_different_ciphertext_uses_request_fingerprint(repo, clock):
    item = add_media(repo)
    fingerprint = hashlib.sha256(b'canonical-user-request').hexdigest()
    first = make_job(repo, item, target='youtube', secret_ciphertext='ciphertext-one', request_fingerprint=fingerprint)
    clock.advance(10)
    second = make_job(repo, item, target='youtube', secret_ciphertext='ciphertext-two', request_fingerprint=fingerprint)
    assert first['id'] == second['id']
    assert second['replayed'] is True
    assert repo.usage('a')['broadcast_jobs_today'] == 1
    with pytest.raises(Conflict, match='different request'):
        make_job(repo, item, target='youtube', secret_ciphertext='ciphertext-three')


def test_concurrent_duplicate_requests_only_admit_one(repo, clock):
    item = add_media(repo)
    barrier = threading.Barrier(8)

    def request(_):
        barrier.wait()
        return make_job(repo, item)

    with ThreadPoolExecutor(max_workers=8) as workers:
        responses = list(workers.map(request, range(8)))
    assert len({response['id'] for response in responses}) == 1
    assert sum(not response['replayed'] for response in responses) == 1
    assert repo.usage('a')['pending_jobs'] == 1


def test_concurrent_claims_have_one_owner_across_repository_instances(repo, clock):
    item = add_media(repo)
    job = make_job(repo, item)
    other = Repository(str(repo.engine.url), clock=clock, runtime_allowance=0, validation_duration=120)
    barrier = threading.Barrier(2)

    def take(instance, name):
        barrier.wait()
        return instance.claim(name)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(take, repo, 'one'), pool.submit(take, other, 'two')]
            claims = [future.result() for future in futures]
        assert sum(claim is not None for claim in claims) == 1
        claim = next(claim for claim in claims if claim)
        assert claim['id'] == job['id'] and claim['lease_version'] == 1 and claim['attempt'] == 1
    finally:
        other.close()


def test_reservations_include_retry_envelope_and_do_not_overbook(repo, clock):
    item = add_media(repo)
    first = make_job(repo, item)
    assert first['reserved_until'] == clock() + 15 * 3 + 5 + 10 + 60
    with pytest.raises(QuotaExceeded, match='capacity'):
        make_job(repo, item, 'conflict', scheduled=clock() + 15)
    next_job = make_job(repo, item, 'next', scheduled=first['reserved_until'])
    assert next_job['scheduled'] == first['reserved_until']
    with pytest.raises(RepositoryError, match='deadline'):
        make_job(repo, item, 'short-deadline', scheduled=next_job['reserved_until'], deadline=next_job['reserved_until'] + 60)


def test_global_admission_and_per_tenant_independent_slots(tmp_path, clock):
    repo = Repository(f'sqlite:///{tmp_path}/slots.db', global_concurrency=2, create_schema=True, clock=clock)
    try:
        for tenant in ('a', 'b'):
            make_job(repo, add_media(repo, tenant, id=f'm-{tenant}'))
        with pytest.raises(QuotaExceeded):
            make_job(repo, add_media(repo, 'c', id='m-c'))
        first, second = repo.claim('worker-1'), repo.claim('worker-2')
        assert {first['tenant_id'], second['tenant_id']} == {'a', 'b'}
        assert repo.claim('worker-3') is None
    finally:
        repo.close()


def test_expired_lease_fails_without_replaying_external_broadcast(repo, clock):
    job = make_job(repo, add_media(repo))
    claim = repo.claim('worker', lease_seconds=10)
    clock.advance(11)
    with pytest.raises(LeaseLost):
        repo.heartbeat(job['id'], claim['lease_token'])
    assert repo.recover_expired() == 1
    assert repo.get_job('a', job['id'])['state'] == 'failed'
    assert repo.get_job('a', job['id'])['error_code'] == 'lease_expired'
    assert repo.claim('replacement') is None
    with pytest.raises(LeaseLost):
        repo.finish(job['id'], claim['lease_token'], state='completed', progress=15)


def test_explicit_retry_backoff_and_stale_callback_fencing(repo, clock):
    job = make_job(repo, add_media(repo))
    first = repo.claim('one')
    repo.heartbeat(job['id'], first['lease_token'], progress=5)
    result = repo.finish(job['id'], first['lease_token'], state='retry_wait', error_code='network_unavailable')
    assert result['next_run'] == clock() + 5
    assert repo.claim('two') is None
    clock.advance(5)
    second = repo.claim('two')
    assert second['lease_version'] == 2 and second['progress'] == 5
    assert second['lease_token'] != first['lease_token']
    with pytest.raises(LeaseLost):
        repo.finish(job['id'], first['lease_token'], state='completed', progress=15)
    assert repo.finish(job['id'], second['lease_token'], state='completed', progress=15)['state'] == 'completed'


def test_retry_limit_and_incomplete_output_cannot_be_completed(repo, clock):
    job = make_job(repo, add_media(repo), max_attempts=1)
    first = repo.claim('one')
    result = repo.finish(job['id'], first['lease_token'], state='retry_wait', error_code='network_unavailable')
    assert result['state'] == 'failed'
    item = add_media(repo, id='second')
    job2 = make_job(repo, item, 'second')
    claim = repo.claim('two')
    result = repo.finish(job2['id'], claim['lease_token'], state='completed', progress=7)
    assert result['state'] == 'failed' and result['error_code'] == 'incomplete_output'


def test_cancel_active_job_remains_owned_until_worker_stops(repo):
    job = make_job(repo, add_media(repo))
    claim = repo.claim('worker')
    assert repo.cancel('a', job['id'])['state'] == 'stopping'
    assert repo.heartbeat(job['id'], claim['lease_token'])['cancel_requested'] is True
    result = repo.finish(job['id'], claim['lease_token'], state='completed', progress=15)
    assert result['state'] == 'stopped'
    assert repo.cancel('a', job['id'])['state'] == 'stopped'


def test_cancel_queued_job_never_gets_claimed(repo):
    job = make_job(repo, add_media(repo))
    assert repo.cancel('a', job['id'])['state'] == 'stopped'
    assert repo.claim('worker') is None


def test_uploading_media_requires_validation_before_broadcast(repo):
    item = repo.add_media('a', name='sample.mp4', object_key='replay/a/media/upload.mp4', bytes=1000, status='uploading')
    with pytest.raises(Conflict, match='validation'):
        make_job(repo, item)
    repo.update_media('a', item['id'], status='validating')
    job = make_job(repo, item, 'validate', target='validate')
    claim = repo.claim('validator')
    assert claim['media']['status'] == 'validating'
    with pytest.raises(RepositoryError, match='duration'):
        repo.update_media('a', item['id'], status='ready')
    repo.finish(job['id'], claim['lease_token'], state='completed',
                validation_metadata=dict(duration=15, width=1280, height=720, fps=30))
    assert make_job(repo, repo.get_media('a', item['id']))['state'] == 'scheduled'
    with pytest.raises(Conflict, match='immutable'):
        repo.update_media('a', item['id'], status='ready', duration=999)


def test_storage_daily_and_pending_quotas_are_persisted(tmp_path, clock):
    url = f'sqlite:///{tmp_path}/quota.db'
    repo = Repository(url, max_storage_bytes=1000, max_media=2, max_pending_jobs=1,
        max_daily_runtime_seconds=50, create_schema=True, clock=clock)
    item = add_media(repo, bytes=1000)
    with pytest.raises(QuotaExceeded):
        add_media(repo, id='other', bytes=1)
    job = make_job(repo, item, target='youtube', secret_ciphertext='ciphertext')
    with pytest.raises(QuotaExceeded, match='Pending'):
        make_job(repo, item, 'other', scheduled=job['reserved_until'], target='youtube', secret_ciphertext='ciphertext')
    repo.cancel('a', job['id'])
    repo.close()
    repo = Repository(url, max_daily_runtime_seconds=50, clock=clock)
    try:
        assert repo.usage('a')['reserved_runtime_seconds_today'] == 45
        with pytest.raises(QuotaExceeded, match='Daily'):
            make_job(repo, item, 'other', target='youtube', secret_ciphertext='ciphertext')
    finally:
        repo.close()


def test_retention_excludes_active_and_keeps_failed_deletions_retryable(repo, clock):
    active_media = add_media(repo, id='active')
    inactive_media = add_media(repo, 'b', id='inactive')
    make_job(repo, active_media)
    clock.advance(1000)
    assert [item['id'] for item in repo.retention_candidates(clock())] == [inactive_media['id']]
    with pytest.raises(Conflict):
        repo.delete_media('a', active_media['id'])
    result = repo.cleanup(clock())
    assert result['objects_queued'] == 1
    deletion = repo.pending_deletions()[0]
    assert repo.usage('b')['storage_bytes'] == 1000
    assert repo.list_media('b') == []
    repo.deletion_failed(deletion['id'])
    assert repo.pending_deletions() == []
    clock.advance(61)
    assert repo.pending_deletions()[0]['attempt'] == 1
    repo.confirm_deletion(deletion['id'])
    assert repo.usage('b')['storage_bytes'] == 0
    assert repo.pending_deletions() == []
    assert repo.confirm_deletion(deletion['id']) is False


def test_retention_preserves_idempotency_after_job_history_removed(repo, clock):
    item = add_media(repo)
    job = make_job(repo, item)
    repo.cancel('a', job['id'])
    clock.advance(100)
    assert repo.cleanup(clock())['jobs_deleted'] == 1
    with pytest.raises(Conflict, match='expired'):
        make_job(repo, item)


def test_output_retention_and_quota_acknowledged_after_storage_delete(repo, clock):
    item = add_media(repo)
    job = make_job(repo, item)
    claim = repo.claim('worker')
    repo.reserve_output(job['id'], claim['lease_token'], object_key='replay/a/outputs/test.flv',
                        bytes=2000, sha256='f' * 64)
    repo.finish(job['id'], claim['lease_token'], state='completed', progress=15,
        output_key='replay/a/outputs/test.flv', output_bytes=2000)
    assert repo.usage('a')['storage_bytes'] == 3000
    clock.advance(1000)
    repo.cleanup(clock())
    deletes = repo.pending_deletions()
    assert len(deletes) == 2
    assert repo.get_job('a', job['id'])['output_key']
    for deletion in deletes:
        repo.confirm_deletion(deletion['id'])
    assert repo.usage('a')['storage_bytes'] == 0
    assert repo.get_job('a', job['id'])['output_key'] is None


def test_worker_liveness_detects_idle_dispatcher_death(repo, clock):
    assert repo.worker_health()['alive'] is False
    repo.heartbeat_worker('dispatcher', {'role': 'dispatcher', 'version': 'v1', 'secret': 'discard-me'})
    assert repo.worker_health()['alive'] is True
    clock.advance(61)
    assert repo.worker_health()['alive'] is False
    assert repo.ping() is True


def test_events_bounded_and_only_safe_codes_allowed(repo, clock):
    item = add_media(repo)
    job = make_job(repo, item)
    with repo._transaction() as conn:
        for index in range(150):
            repo._event(conn, job, f'event_{index}', clock())
    assert len(repo.events('a', job['id'])) == 100
    with repo.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(job_events)).scalar_one() == 100
    claim = repo.claim('worker')
    with pytest.raises(RepositoryError, match='error code'):
        repo.finish(job['id'], claim['lease_token'], state='failed', error_code='rtmps://secret-in-url')
    assert repo.get_job('a', job['id'])['state'] == 'starting'


def test_validation_completion_is_fenced_and_atomic(repo, clock):
    item = repo.add_media('a', name='sample.mp4', object_key='replay/a/media/v.mp4', bytes=1000, status='uploading')
    job = make_job(repo, item, 'validation', target='validate')
    assert repo.get_media('a', item['id'])['status'] == 'validating'
    claim = repo.claim('validator', lease_seconds=10)
    with pytest.raises(RepositoryError):
        repo.finish(job['id'], claim['lease_token'], state='completed', validation_metadata={'duration': 15})
    assert repo.get_job('a', job['id'])['state'] == 'starting'
    assert repo.get_media('a', item['id'])['status'] == 'validating'
    clock.advance(11)
    with pytest.raises(LeaseLost):
        repo.finish(job['id'], claim['lease_token'], state='completed',
                    validation_metadata=dict(duration=15, width=1280, height=720, fps=30))
    repo.recover_expired()
    assert repo.get_media('a', item['id'])['status'] == 'failed'
    assert repo.get_media('a', item['id'])['duration'] == 0


def test_validation_cancel_cannot_publish_late_ready_metadata(repo):
    item = repo.add_media('a', name='sample.mp4', object_key='replay/a/media/v.mp4', bytes=1000, status='uploading')
    job = make_job(repo, item, 'validation', target='validate')
    claim = repo.claim('validator')
    repo.cancel('a', job['id'])
    repo.finish(job['id'], claim['lease_token'], state='completed',
                validation_metadata=dict(duration=15, width=1280, height=720, fps=30))
    assert repo.get_media('a', item['id'])['status'] == 'failed'
    assert repo.get_job('a', job['id'])['state'] == 'stopped'


def test_output_quota_is_reserved_before_upload_and_deletion_waits_for_url_expiry(tmp_path, clock):
    budget = estimate_output_bytes(15)
    repo = Repository(f'sqlite:///{tmp_path}/outputs.db', max_storage_bytes=1000 + budget,
        runtime_allowance=0, clock=clock, create_schema=True)
    try:
        item = add_media(repo)
        job = make_job(repo, item)
        claim = repo.claim('one')
        key = 'replay/a/outputs/attempt-1.flv'
        with pytest.raises(QuotaExceeded):
            repo.reserve_output(job['id'], claim['lease_token'], object_key=key, bytes=budget + 1, sha256='a' * 64)
        repo.reserve_output(job['id'], claim['lease_token'], object_key=key, bytes=1500, sha256='a' * 64)
        assert repo.usage('a')['storage_bytes'] == 1000 + budget
        repo.reserve_output(job['id'], claim['lease_token'], object_key=key, bytes=1500, sha256='a' * 64)
        assert repo.usage('a')['storage_bytes'] == 1000 + budget
        with pytest.raises(Conflict):
            repo.reserve_output(job['id'], claim['lease_token'], object_key=key, bytes=1500, sha256='b' * 64)
        repo.finish(job['id'], claim['lease_token'], state='failed', error_code='upload_failed')
        assert repo.pending_deletions() == []
        clock.advance(901)
        deletion = repo.pending_deletions()[0]
        assert deletion['object_key'] == key
        assert repo.usage('a')['storage_bytes'] == 2500
        repo.confirm_deletion(deletion['id'])
        assert repo.usage('a')['storage_bytes'] == 1000
    finally:
        repo.close()


def test_committed_output_moves_quota_without_double_counting(repo):
    job = make_job(repo, add_media(repo))
    claim = repo.claim('one')
    key = 'replay/a/outputs/attempt.flv'
    with pytest.raises(Conflict, match='reservation'):
        repo.finish(job['id'], claim['lease_token'], state='completed', progress=15, output_key=key, output_bytes=200)
    repo.reserve_output(job['id'], claim['lease_token'], object_key=key, bytes=200, sha256='b' * 64)
    assert repo.usage('a')['storage_bytes'] == 1000 + estimate_output_bytes(15)
    repo.finish(job['id'], claim['lease_token'], state='completed', progress=15, output_key=key, output_bytes=200)
    assert repo.usage('a')['storage_bytes'] == 1200


def test_runtime_cleanup_keeps_every_attempt_until_explicit_ack(repo, clock):
    job = make_job(repo, add_media(repo))
    first = repo.claim('one')
    with pytest.raises(Conflict, match='active'):
        repo.mark_runtime_cleaned(job['id'], 1)
    repo.finish(job['id'], first['lease_token'], state='retry_wait')
    clock.advance(5)
    second = repo.claim('two')
    runtimes = repo.runtimes()
    assert [(row['lease_version'], row['state']) for row in runtimes] == [(1, 'retry_wait'), (2, 'starting')]
    assert repo.mark_runtime_cleaned(job['id'], 1) is True
    assert [row['lease_version'] for row in repo.runtimes()] == [2]
    repo.finish(job['id'], second['lease_token'], state='completed', progress=15)
    repo.mark_runtime_cleaned(job['id'], 2)
    assert repo.runtimes() == []


def test_default_runtime_envelope_includes_full_validation_and_startup(tmp_path, clock):
    repo = Repository(f'sqlite:///{tmp_path}/envelope.db', clock=clock, create_schema=True)
    try:
        item = repo.add_media('a', name='sample.mp4', object_key='replay/a/media/v.mp4', bytes=1000, status='uploading')
        validation = make_job(repo, item, 'validation', target='validate', max_attempts=1)
        assert validation['reserved_until'] - validation['scheduled'] == 600 + 120 + 60
        claim = repo.claim('validator')
        assert claim is not None
        repo.finish(validation['id'], claim['lease_token'], state='completed',
            validation_metadata=dict(duration=15, width=1280, height=720, fps=30))
        broadcast = make_job(repo, repo.get_media('a', item['id']), 'broadcast')
        assert broadcast['reserved_until'] - broadcast['scheduled'] == (15 + 120) * 3 + 5 + 10 + 60
    finally:
        repo.close()


def test_one_attempt_real_clock_claim_consumes_jitter_once(tmp_path):
    repo = Repository(f'sqlite:///{tmp_path}/realclock.db', create_schema=True)
    try:
        item = repo.add_media('a', name='sample.mp4', object_key='replay/a/media/real.mp4', bytes=1000, status='uploading')
        validation = make_job(repo, item, 'validation', target='validate', max_attempts=1)
        claim = repo.claim('validator')
        assert claim is not None
        repo.finish(validation['id'], claim['lease_token'], state='completed',
                    validation_metadata=dict(duration=15, width=1280, height=720, fps=30))
        broadcast = make_job(repo, repo.get_media('a', item['id']), 'broadcast', max_attempts=1)
        assert repo.claim('worker')['id'] == broadcast['id']
    finally:
        repo.close()


def test_restored_orphan_output_intent_is_reclaimed_without_callback(repo, clock):
    job = make_job(repo, add_media(repo))
    claim = repo.claim('lost-during-restore')
    key = 'replay/a/outputs/orphan.flv'
    repo.reserve_output(job['id'], claim['lease_token'], object_key=key, bytes=2000, sha256='c' * 64)
    with repo.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job['id']).values(state='failed', lease_token=None, lease_expires=None))
    clock.advance(901)
    repo.cleanup(clock() - 86400)
    deletions = repo.pending_deletions()
    assert len(deletions) == 1 and deletions[0]['object_key'] == key
    assert repo.usage('a')['storage_bytes'] == 3000
    repo.confirm_deletion(deletions[0]['id'])
    assert repo.usage('a')['storage_bytes'] == 1000
