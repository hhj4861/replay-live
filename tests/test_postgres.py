"""Real PostgreSQL integration. Uses only explicitly named ephemeral test databases.

Run with REPLAY_TEST_POSTGRES_URL pointing to a disposable database whose name
starts with replay_test. Each test owns a random schema and removes only that
schema. The production SQL migration is executed before any repository call.
"""
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import hashlib
import multiprocessing
import os
from pathlib import Path
import threading
import time
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, make_url, text

from server.access_policy import AccessPolicy
from server.auth import Principal
from server.output_policy import estimate_output_bytes
from server.repository import Conflict, LeaseLost, QuotaExceeded, Repository, metadata


URL = os.environ.get('REPLAY_TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not URL, reason='Explicit disposable PostgreSQL URL is required')


def _new_repo(url):
    return Repository(url, global_concurrency=2, tenant_concurrency=1,
                      runtime_allowance=1, validation_duration=5, reservation_grace=60)


@pytest.fixture
def postgres():
    parsed = make_url(URL)
    if parsed.get_backend_name() != 'postgresql' or not (parsed.database or '').startswith('replay_test'):
        pytest.fail('Real PostgreSQL tests require a disposable replay_test* database')
    schema = 'test_' + uuid.uuid4().hex
    owner = create_engine(parsed)
    with owner.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    # Every engine, including subprocesses, gets an isolated server-side search_path.
    test_url = parsed.update_query_dict({'options': f'-csearch_path={schema}'}).render_as_string(hide_password=False)
    migration_engine = create_engine(test_url)
    try:
        with migration_engine.begin() as conn:
            for migration in sorted(Path('migrations').glob('*.sql')):
                conn.exec_driver_sql(migration.read_text())
        repo = _new_repo(test_url)
        access = AccessPolicy(repo.engine, user_limit=10, tenant_limit=20, mode='production')
        access.migrate()
        yield repo, access, test_url
        repo.close()
    finally:
        migration_engine.dispose()
        with owner.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        owner.dispose()


def _media(repo, tenant='a', pending=False):
    return repo.add_media(tenant, name='synthetic.mp4', object_key=f'replay/{tenant}/media/{uuid.uuid4().hex}.mp4',
                          bytes=1000, duration=0 if pending else 15, width=0 if pending else 1280,
                          height=0 if pending else 720, fps=0 if pending else 30,
                          status='uploading' if pending else 'ready')


def _job(repo, item, key='same-key', **extra):
    return repo.create_job(item['tenant_id'], media_id=item['id'], title='Synthetic PostgreSQL test',
                           target=extra.pop('target', 'local'), idempotency_key=key, **extra)


def _claim_process(url, worker):
    repo = _new_repo(url)
    try:
        result = repo.claim(worker)
        return None if result is None else {'id': result['id'], 'lease_version': result['lease_version']}
    finally:
        repo.close()


def _create_process(url, item):
    repo = _new_repo(url)
    try:
        job = _job(repo, item)
        return job['id'], job['replayed']
    finally:
        repo.close()


def _output_admission_process(url, item, kind, scheduled):
    budget = estimate_output_bytes(15)
    repo = Repository(url, max_storage_bytes=1000 + budget, runtime_allowance=1)
    try:
        try:
            if kind == 'upload':
                repo.add_media(item['tenant_id'], name='synthetic.mp4',
                    object_key=f'replay/a/media/{uuid.uuid4().hex}.mp4', bytes=budget, status='uploading')
            else:
                _job(repo, item, key=kind, scheduled=scheduled)
            return True
        except QuotaExceeded:
            return False
    finally:
        repo.close()


def _batch_process(url, item):
    repo = Repository(url, tenant_concurrency=4, global_concurrency=4)
    try:
        result = repo.create_jobs_batch(item['tenant_id'], media_id=item['id'], title='Synthetic batch',
            idempotency_key='batch-same-request', destinations=[{'target': 'local'},
                {'target': 'youtube', 'secret_ciphertext': 'opaque-youtube'},
                {'target': 'twitch', 'secret_ciphertext': 'opaque-twitch'}])
        return [row['id'] for row in result['jobs']], result['replayed']
    finally:
        repo.close()


def test_postgres_sql_migration_matches_all_repository_columns(postgres):
    repo, _, _ = postgres
    inspector = inspect(repo.engine)
    for table in metadata.tables.values():
        assert {column['name'] for column in inspector.get_columns(table.name)} == set(table.c.keys())
    assert repo.ping() is True
    assert {'uq_replay_jobs_idempotency', 'uq_replay_jobs_tenant_id'} <= {
        item['name'] for item in inspector.get_unique_constraints('replay_jobs')}
    assert {'ck_replay_jobs_output_budget', 'ck_replay_jobs_output_reserved'} <= {
        item['name'] for item in inspector.get_check_constraints('replay_jobs')}


def test_postgres_two_process_claim_and_idempotency(postgres):
    repo, _, url = postgres
    item = _media(repo)
    context = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        results = list(pool.map(_create_process, [url] * 8, [item] * 8))
    assert len({item[0] for item in results}) == 1
    assert sum(not item[1] for item in results) == 1
    assert repo.usage('a')['storage_reserved_bytes'] == estimate_output_bytes(15)
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        claims = list(pool.map(_claim_process, [url] * 8, [f'worker-{i}' for i in range(8)]))
    assert len([claim for claim in claims if claim]) == 1
    assert repo.usage('a')['pending_jobs'] == 1


def test_postgres_capacity_tenant_isolation_and_fencing(postgres):
    repo, _, _ = postgres
    first, second = _job(repo, _media(repo, 'a')), _job(repo, _media(repo, 'b'))
    with pytest.raises(QuotaExceeded):
        _job(repo, _media(repo, 'c'))
    claims = [repo.claim('one'), repo.claim('two')]
    assert {claim['tenant_id'] for claim in claims} == {'a', 'b'}
    assert repo.claim('three') is None
    with pytest.raises(LeaseLost):
        repo.finish(first['id'], 'stale-token', state='completed', progress=15)
    with pytest.raises(Conflict):
        repo.delete_media('a', first['media_id'])
    claim = next(item for item in claims if item['id'] == first['id'])
    repo.cancel('a', first['id'])
    assert repo.heartbeat(first['id'], claim['lease_token'])['cancel_requested'] is True
    assert repo.finish(first['id'], claim['lease_token'], state='completed', progress=15)['state'] == 'stopped'
    assert repo.list_jobs('c') == []
    assert repo.get_job('b', second['id'])['state'] == 'starting'


def test_postgres_validation_output_quota_and_lease_expiry(postgres):
    repo, _, _ = postgres
    item = _media(repo, pending=True)
    validation = _job(repo, item, target='validate', max_attempts=1)
    claim = repo.claim('validator')
    assert claim is not None
    repo.finish(validation['id'], claim['lease_token'], state='completed',
        validation_metadata=dict(duration=15, width=1280, height=720, fps=30))
    assert repo.get_media('a', item['id'])['status'] == 'ready'
    job = _job(repo, repo.get_media('a', item['id']), key='broadcast')
    claim = repo.claim('worker')
    output_key = 'replay/a/outputs/result.flv'
    repo.reserve_output(job['id'], claim['lease_token'], object_key=output_key, bytes=2000, sha256='a' * 64)
    assert repo.usage('a')['storage_bytes'] == 1000 + estimate_output_bytes(15)
    repo.finish(job['id'], claim['lease_token'], state='completed', progress=15, output_key=output_key, output_bytes=2000)
    assert repo.usage('a')['storage_bytes'] == 3000
    another = _job(repo, item, key='expired')
    claim = repo.claim('lost-worker')
    with repo.engine.begin() as conn:
        conn.execute(text('UPDATE replay_jobs SET lease_expires=:expired WHERE id=:id'), {'expired': time.time() - 1, 'id': another['id']})
    assert repo.recover_expired() == 1
    assert repo.get_job('a', another['id'])['error_code'] == 'lease_expired'
    with pytest.raises(LeaseLost):
        repo.heartbeat(another['id'], claim['lease_token'])


@pytest.mark.parametrize('rival', ['upload', 'other-day'])
def test_postgres_output_and_upload_admission_share_atomic_storage_budget(postgres, rival):
    repo, _, url = postgres
    item = _media(repo)
    start = time.time() + 60
    context = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        futures = [pool.submit(_output_admission_process, url, item, kind, scheduled)
            for kind, scheduled in [('local', start), (rival, start + 86400)]]
        assert sum(future.result() for future in futures) == 1
    usage = repo.usage('a')
    assert usage['storage_bytes'] == 1000 + estimate_output_bytes(15)
    assert usage['storage_reserved_bytes'] == estimate_output_bytes(15)


def test_postgres_batch_process_race_admits_one_group_and_independent_leases(postgres):
    repo, _, url = postgres
    item = _media(repo)
    context = multiprocessing.get_context('spawn')
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        results = list(pool.map(_batch_process, [url] * 8, [item] * 8))
    assert len({tuple(result[0]) for result in results}) == 1
    assert sum(not result[1] for result in results) == 1
    assert repo.usage('a')['pending_jobs'] == 3
    assert repo.usage('a')['storage_reserved_bytes'] == estimate_output_bytes(15)
    repo.tenant_concurrency = repo.global_concurrency = 4
    claims = [repo.claim(f'platform-{index}') for index in range(3)]
    assert {claim['target'] for claim in claims} == {'local', 'youtube', 'twitch'}
    assert len({claim['lease_token'] for claim in claims}) == 3


def test_postgres_batch_late_quota_failure_rolls_back_all_children(postgres):
    repo, _, _ = postgres
    repo.tenant_concurrency = repo.global_concurrency = 4
    repo.max_storage_bytes = 1000
    item = _media(repo)
    before = repo.usage('a')
    with pytest.raises(QuotaExceeded):
        repo.create_jobs_batch('a', media_id=item['id'], title='Rollback batch', idempotency_key='atomic-rollback',
            destinations=[{'target': 'youtube', 'secret_ciphertext': 'opaque'}, {'target': 'local'}])
    assert repo.usage('a') == before
    with repo.engine.connect() as conn:
        for table in ('replay_jobs', 'replay_broadcast_batches', 'replay_daily_usage', 'replay_events', 'replay_idempotency'):
            assert conn.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() == 0


def test_postgres_access_quota_concurrency_and_revocation(postgres):
    repo, access, _ = postgres
    token = hashlib.sha256(b'synthetic-test-token').hexdigest()
    user = Principal('test-user', 'a', ('operator',), token, time.time() + 3600)

    def consume(_):
        try:
            access.check(user)
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(consume, range(40)))
    assert results.count(200) == 10 and results.count(429) == 30
    other = Principal('other-user', 'b', ('operator',), hashlib.sha256(b'other-token').hexdigest(), time.time() + 3600)
    access.check(other)
    access.revoke(other)
    fresh_policy = AccessPolicy(repo.engine, mode='production')
    with pytest.raises(HTTPException) as error:
        fresh_policy.check(other)
    assert error.value.status_code == 401
    assert access.purge_expired() == {'expired_revocations': 0, 'expired_access_counters': 0}


def test_postgres_lease_checks_use_time_after_lock_wait(postgres):
    repo, _, url = postgres
    item = _media(repo)
    job = _job(repo, item)
    claimed = repo.claim('one', lease_seconds=5)
    other = _new_repo(url)
    clock = [time.time()]
    other.clock = lambda: clock[0]
    entered_gate = threading.Event()
    original_gate = other._gate

    def gate(connection):
        entered_gate.set()
        return original_gate(connection)

    other._gate = gate
    try:
        with repo.engine.begin() as conn:
            conn.execute(text('SELECT id FROM replay_coordination WHERE id=1 FOR UPDATE'))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(other.heartbeat, job['id'], claimed['lease_token'])
                assert entered_gate.wait(5)
                # Move the worker's clock past its unchanged lease while its
                # heartbeat waits on a real PostgreSQL row lock. Capturing time
                # before lock acquisition would incorrectly renew this lease.
                clock[0] = claimed['lease_expires'] + 1
                conn.commit()
                with pytest.raises(LeaseLost):
                    future.result(timeout=10)
    finally:
        other.close()


def test_postgres_skip_locked_can_claim_another_tenant(postgres):
    repo, _, url = postgres
    first = _job(repo, _media(repo, 'a'))
    second = _job(repo, _media(repo, 'b'))
    other = _new_repo(url)
    try:
        with repo.engine.begin() as conn:
            conn.execute(text('SELECT id FROM replay_jobs WHERE id=:id FOR UPDATE'), {'id': first['id']})
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(other.claim, 'skip-locked-worker')
                assert future.result(timeout=5)['id'] == second['id']
    finally:
        other.close()
