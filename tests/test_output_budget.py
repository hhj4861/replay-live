"""Output admission and physical-upload liabilities use real SQLite transactions."""
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest
from sqlalchemy import select, update

from server.output_policy import estimate_output_bytes
from server.repository import (LeaseLost, OutputCapacityExceeded, QuotaExceeded,
                               Repository, jobs, upload_reservations)


BUDGET = estimate_output_bytes(15)


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def repo(tmp_path, clock):
    instance = Repository(f'sqlite:///{tmp_path}/output.db', clock=lambda: clock[0],
        max_storage_bytes=1000 + BUDGET, runtime_allowance=0, create_schema=True)
    yield instance
    instance.close()


def media(repo, tenant='a', **kwargs):
    return repo.add_media(tenant, name='synthetic.mp4', object_key=f'replay/{tenant}/media/{uuid.uuid4().hex}',
        bytes=kwargs.pop('bytes', 1000), duration=kwargs.pop('duration', 15), width=1280,
        height=720, fps=30, **kwargs)


def job(repo, item, **kwargs):
    return repo.create_job(item['tenant_id'], media_id=item['id'], title='Output quota test',
        target=kwargs.pop('target', 'local'), idempotency_key=kwargs.pop('key', 'same'), **kwargs)


def reserve(repo, claim, size=2000, *, expires=900):
    return repo.reserve_output(claim['id'], claim['lease_token'],
        object_key=f'replay/{claim["tenant_id"]}/outputs/{claim["id"]}-{claim["lease_version"]}.flv',
        bytes=size, sha256='a' * 64, expires=expires)


def test_admission_reserves_future_dates_and_idempotency_is_free(repo, clock):
    item = media(repo)
    first = job(repo, item, scheduled=clock[0] + 86400)
    assert first['output_budget_bytes'] == first['output_reserved_bytes'] == BUDGET
    assert job(repo, item, scheduled=clock[0] + 86400)['replayed'] is True
    usage = repo.usage('a')
    assert usage['storage_bytes'] == 1000 + BUDGET
    assert usage['storage_reserved_bytes'] == BUDGET
    assert usage['storage_available_bytes'] == 0
    with pytest.raises(OutputCapacityExceeded) as error:
        job(repo, item, key='other-day', scheduled=clock[0] + 2 * 86400)
    assert error.value.code == 'OUTPUT_STORAGE_QUOTA_EXCEEDED'
    assert len(repo.list_jobs('a')) == 1
    repo.cancel('a', first['id'])
    assert repo.usage('a')['storage_bytes'] == 1000
    assert job(repo, item, key='other-day', scheduled=clock[0] + 2 * 86400)['output_budget_bytes'] == BUDGET


def test_file_limit_rejects_before_any_admission_side_effects(repo):
    repo.max_output_bytes = BUDGET - 1
    item = media(repo)
    before = repo.usage('a')
    with pytest.raises(OutputCapacityExceeded) as error:
        job(repo, item)
    assert error.value.code == 'OUTPUT_LIMIT_EXCEEDED'
    assert repo.usage('a') == before
    assert repo.list_jobs('a') == []


def test_youtube_and_validation_do_not_reserve_output_capacity(repo):
    ready = media(repo)
    youtube = job(repo, ready, target='youtube', secret_ciphertext='synthetic-ciphertext')
    assert youtube['output_budget_bytes'] == youtube['output_reserved_bytes'] == 0
    pending = repo.add_media('b', name='pending.mp4', object_key='replay/b/media/pending', bytes=700, status='uploading')
    validation = job(repo, pending, target='validate')
    assert validation['output_budget_bytes'] == validation['output_reserved_bytes'] == 0
    assert repo.usage('a')['storage_reserved_bytes'] == 0
    assert repo.usage('b')['storage_reserved_bytes'] == 700


@pytest.mark.parametrize('rival', ['upload', 'job'])
def test_concurrent_storage_admission_never_overbooks_across_connections(repo, clock, rival):
    item = media(repo)
    other = Repository(str(repo.engine.url), clock=lambda: clock[0],
        max_storage_bytes=1000 + BUDGET, runtime_allowance=0)
    barrier = threading.Barrier(2)

    def admit(instance, name):
        barrier.wait()
        try:
            if name == 'rival' and rival == 'upload':
                media(instance, bytes=BUDGET)
            else:
                job(instance, item, key=name, scheduled=clock[0] + (86400 if name == 'rival' else 0))
            return True
        except QuotaExceeded:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(admit, repo, 'local'), pool.submit(admit, other, 'rival')]
            assert sum(f.result() for f in futures) == 1
        assert repo.usage('a')['storage_bytes'] == 1000 + BUDGET
    finally:
        other.close()


def test_put_is_idempotent_conversion_and_completion_settles_actual_bytes(repo, clock):
    created = job(repo, media(repo))
    claim = repo.claim('worker')
    intent = reserve(repo, claim)
    clock[0] += 1
    again = reserve(repo, claim, expires=1)
    assert again['expires_at'] == intent['expires_at']
    assert repo.get_job('a', created['id'])['output_reserved_bytes'] == BUDGET - 2000
    assert repo.usage('a')['storage_bytes'] == 1000 + BUDGET
    with pytest.raises(OutputCapacityExceeded) as error:
        reserve(repo, claim, BUDGET + 1)
    assert error.value.code == 'OUTPUT_LIMIT_EXCEEDED'
    result = repo.finish(created['id'], claim['lease_token'], state='completed', progress=15,
                         output_key=intent['object_key'], output_bytes=2000)
    assert result['output_budget_bytes'] == BUDGET and result['output_reserved_bytes'] == 0
    assert repo.usage('a')['storage_bytes'] == 3000
    assert repo.usage('a')['storage_reserved_bytes'] == 0
    assert repo.usage('a')['storage_available_bytes'] == BUDGET - 2000


@pytest.mark.parametrize('ending', ['failed', 'incomplete', 'cancelled', 'expired', 'restore', 'cleanup'])
def test_terminal_releases_only_hold_and_deletion_ack_releases_put(repo, clock, ending):
    created = job(repo, media(repo))
    claim = repo.claim('worker', lease_seconds=10)
    intent = reserve(repo, claim)
    if ending == 'failed':
        repo.finish(created['id'], claim['lease_token'], state='failed')
    elif ending == 'incomplete':
        result = repo.finish(created['id'], claim['lease_token'], state='completed', progress=7,
            output_key=intent['object_key'], output_bytes=2000)
        assert result['state'] == 'failed' and result['output_key'] is None
    elif ending == 'cancelled':
        repo.cancel('a', created['id'])
        # A late successful upload callback must still clean up the object.
        result = repo.finish(created['id'], claim['lease_token'], state='completed', progress=15,
                             output_key=intent['object_key'], output_bytes=2000)
        assert result['state'] == 'stopped' and result['output_key'] is None
    elif ending == 'expired':
        clock[0] += 11
        assert repo.recover_expired() == 1
    else:
        with repo._transaction() as conn:
            conn.execute(update(jobs).where(jobs.c.id == created['id']).values(state='failed', lease_token=None))
            if ending == 'restore':
                restore_row = {key: claim[key] for key in ('id', 'tenant_id', 'media_id', 'target', 'lease_version')}
                repo._finish_side_effects(conn, restore_row, 'failed', 'RESTORE_INTERRUPTED', clock[0])
        if ending == 'cleanup':
            repo.cleanup(clock[0] - 86400)
    assert repo.usage('a')['storage_bytes'] == 3000
    assert repo.usage('a')['storage_reserved_bytes'] == 2000
    assert repo.get_job('a', created['id'])['output_reserved_bytes'] == 0
    with pytest.raises(LeaseLost):
        reserve(repo, claim)
    clock[0] = intent['expires_at'] + 1
    repo.cleanup(clock[0] - 86400)
    deletion = repo.pending_deletions()[0]
    assert repo.usage('a')['storage_bytes'] == 3000
    repo.confirm_deletion(deletion['id'])
    assert repo.usage('a')['storage_bytes'] == 1000
    assert repo.usage('a')['storage_reserved_bytes'] == 0
    assert repo.confirm_deletion(deletion['id']) is False


@pytest.mark.parametrize('ending', ['failed', 'cancelled', 'expired', 'retry_exhausted'])
def test_terminal_without_put_returns_entire_budget_immediately(repo, clock, ending):
    created = job(repo, media(repo), max_attempts=1)
    claim = repo.claim('worker', lease_seconds=10)
    if ending == 'expired':
        clock[0] += 11
        repo.recover_expired()
    else:
        if ending == 'cancelled':
            repo.cancel('a', created['id'])
        repo.finish(created['id'], claim['lease_token'],
            state='retry_wait' if ending == 'retry_exhausted' else 'failed')
    assert repo.get_job('a', created['id'])['output_reserved_bytes'] == 0
    assert repo.usage('a')['storage_bytes'] == 1000
    assert repo.usage('a')['storage_reserved_bytes'] == 0
    with repo.engine.connect() as conn:
        assert list(conn.execute(select(upload_reservations))) == []


def test_retry_waits_for_previous_put_deletion_and_does_not_block_other_tenant(repo, clock):
    created = job(repo, media(repo))
    first = repo.claim('one')
    intent = reserve(repo, first, expires=10)
    repo.finish(created['id'], first['lease_token'], state='retry_wait', error_code='upload_failed')
    assert repo.get_job('a', created['id'])['output_reserved_bytes'] == BUDGET - 2000
    clock[0] += 5
    other = job(repo, media(repo, 'b'))
    second_tenant = repo.claim('other-tenant')
    assert second_tenant['id'] == other['id']
    assert repo.get_job('a', created['id'])['state'] == 'retry_wait'
    assert repo.get_job('a', created['id'])['error_code'] == 'OUTPUT_STORAGE_QUOTA_EXCEEDED'
    repo.finish(other['id'], second_tenant['lease_token'], state='failed')
    assert repo.claim('blocked') is None
    clock[0] += 6
    deletion = repo.pending_deletions()[0]
    assert deletion['object_key'] == intent['object_key']
    repo.confirm_deletion(deletion['id'])
    second = repo.claim('retry')
    assert second['id'] == created['id'] and second['lease_version'] == 2
    assert second['output_reserved_bytes'] == BUDGET
    assert repo.usage('a')['storage_bytes'] == 1000 + BUDGET
    with pytest.raises(LeaseLost):
        reserve(repo, first)


def test_retry_can_hold_new_budget_while_old_put_remains_charged(repo, clock):
    repo.max_storage_bytes += 2000
    created = job(repo, media(repo))
    first = repo.claim('one')
    reserve(repo, first)
    repo.finish(created['id'], first['lease_token'], state='retry_wait')
    clock[0] += 5
    second = repo.claim('two')
    assert second['output_reserved_bytes'] == BUDGET
    assert repo.usage('a')['storage_bytes'] == 3000 + BUDGET
    intent = reserve(repo, second, size=1000)
    repo.finish(created['id'], second['lease_token'], state='completed', progress=15,
        output_key=intent['object_key'], output_bytes=1000)
    assert repo.usage('a')['storage_bytes'] == 4000
    assert repo.usage('a')['storage_reserved_bytes'] == 2000
    with repo.engine.connect() as conn:
        assert sorted(conn.execute(select(upload_reservations.c.status)).scalars()) == ['committed', 'pending']


def test_blocked_retry_deadline_returns_hold_but_keeps_issued_put(repo, clock):
    created = job(repo, media(repo))
    first = repo.claim('one')
    reserve(repo, first)
    repo.finish(created['id'], first['lease_token'], state='retry_wait')
    clock[0] = created['deadline'] + 1
    assert repo.recover_expired() == 1
    assert repo.get_job('a', created['id'])['state'] == 'failed'
    assert repo.usage('a')['storage_bytes'] == 3000
    assert repo.usage('a')['storage_reserved_bytes'] == 2000


@pytest.mark.parametrize('scenario', ['available', 'storage_full', 'file_limit'])
def test_legacy_queued_jobs_cannot_start_without_budget(repo, scenario):
    created = job(repo, media(repo))
    with repo.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == created['id']).values(output_budget_bytes=0, output_reserved_bytes=0))
    if scenario == 'storage_full':
        media(repo, bytes=BUDGET)
    elif scenario == 'file_limit':
        repo.max_output_bytes = BUDGET - 1
    claim = repo.claim('legacy')
    result = repo.get_job('a', created['id'])
    if scenario == 'available':
        assert claim['output_budget_bytes'] == claim['output_reserved_bytes'] == BUDGET
        assert repo.usage('a')['storage_bytes'] == 1000 + BUDGET
    else:
        assert claim is None and result['attempt'] == 0 and result['lease_version'] == 0
        assert result['output_reserved_bytes'] == 0
        assert result['error_code'] == ('OUTPUT_LIMIT_EXCEEDED' if scenario == 'file_limit' else 'OUTPUT_STORAGE_QUOTA_EXCEEDED')
        assert result['state'] == ('failed' if scenario == 'file_limit' else 'scheduled')
