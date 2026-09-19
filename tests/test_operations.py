from concurrent.futures import ThreadPoolExecutor
import json

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, insert, select, update

from server.operations import Operations, outbox
from server.repository import Repository, jobs, object_deletions


@pytest.fixture
def fixture(tmp_path):
    now = [1_800_000_000.0]
    repo = Repository('sqlite:///' + str(tmp_path / 'operations.sqlite3'), clock=lambda: now[0],
                      tenant_concurrency=4, global_concurrency=8, create_schema=True)
    operations = Operations(repo.engine, clock=lambda: now[0])
    operations.migrate()
    try:
        yield repo, operations, now
    finally:
        repo.close()


def seed_job(repo, *, state='scheduled', offset=0, updated=None, lease_expires=None):
    item = repo.add_media('synthetic-private-tenant', name='synthetic-private-title.mp4',
        object_key='replay/synthetic/' + str(offset) + '.mp4', bytes=500, duration=15, width=1280, height=720, fps=30)
    row = repo.create_job('synthetic-private-tenant', media_id=item['id'], title='synthetic-confidential-title',
        target='local', idempotency_key='synthetic-idem-' + str(offset),
        request_fingerprint='a' * 64)
    with repo.engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == row['id']).values(state=state,
            next_run=repo.clock() - 200, updated=repo.clock() if updated is None else updated,
            lease_expires=lease_expires))
    return row


def test_snapshot_uses_safe_aggregates_and_ignores_non_dispatcher_heartbeats(fixture):
    repo, operations, now = fixture
    seed_job(repo, offset=1)
    seed_job(repo, state='streaming', offset=2, lease_expires=now[0] - 1)
    seed_job(repo, state='failed', offset=3)
    repo.heartbeat_worker('synthetic-decoder-private-id', {'role': 'worker'})
    result = operations.snapshot()
    assert result['queue_lag_seconds'] == 200 and result['due_jobs'] == 1
    assert result['active_jobs'] == 1 and result['stale_leases'] == 1
    assert result['failed_last_hour'] == 1 and result['dispatcher_last_seen'] is None
    repo.heartbeat_worker('synthetic-private-dispatcher-id', {'role': 'dispatcher'})
    assert operations.snapshot()['dispatcher_age_seconds'] == 0
    assert 'synthetic-' not in json.dumps(result)
    assert all(value is None or isinstance(value, (int, float)) for value in result.values())


def test_alerts_are_durable_deduplicated_and_acknowledgement_is_idempotent(fixture):
    repo, operations, now = fixture
    seed_job(repo)
    first = operations.collect()
    assert first['alerts_created'] == 2 and first['alerts_pending'] == 2
    other = Operations(repo.engine, clock=lambda: now[0])
    assert other.collect()['alerts_created'] == 0
    alerts = other.pending_alerts()
    assert {alert['code'] for alert in alerts} == {'QUEUE_DELAY', 'DISPATCHER_STALE'}
    assert all(set(alert) == {'id', 'code', 'count', 'created'} for alert in alerts)
    assert 'synthetic-' not in json.dumps(alerts)
    now[0] += 901
    assert other.collect()['alerts_created'] == 0, 'unacknowledged alerts never grow without delivery'
    assert len(other.pending_alerts()) == 2
    for alert in alerts:
        assert other.acknowledge(alert['id']) is True
        assert operations.acknowledge(alert['id']) is True
    assert operations.pending_alerts() == []
    assert operations.collect()['alerts_created'] == 0, 'acknowledgement does not bypass cooldown'
    now[0] += 901
    assert operations.collect()['alerts_created'] == 2


def test_parallel_collectors_create_one_pending_alert_per_code(fixture):
    repo, operations, now = fixture
    seed_job(repo)
    def collect(_):
        return Operations(repo.engine, clock=lambda: now[0]).collect()['alerts_created']
    with ThreadPoolExecutor(max_workers=6) as pool:
        totals = list(pool.map(collect, range(12)))
    assert sum(totals) == 2
    assert len(operations.pending_alerts()) == 2


def test_deferred_deletion_is_not_overdue_and_acknowledged_alerts_expire(fixture):
    repo, operations, now = fixture
    with repo.engine.begin() as connection:
        connection.execute(insert(object_deletions).values(id='a' * 32,
            tenant_id='synthetic-private-tenant', object_key='replay/synthetic/object.mp4',
            created=now[0] - 10000, not_before=now[0] + 600, attempt=0))
    assert operations.snapshot()['outstanding_deletions'] == 1
    assert operations.snapshot()['deletion_lag_seconds'] == 0
    assert operations.collect()['alerts_created'] == 0
    now[0] += 4200
    assert operations.collect()['alerts_created'] == 1
    alert = operations.pending_alerts()[0]
    assert alert['code'] == 'DELETION_BACKLOG'
    operations.acknowledge(alert['id'])
    now[0] += 30 * 86400 + 1
    operations.collect()
    with repo.engine.connect() as connection:
        assert connection.execute(select(outbox.c.id).where(outbox.c.id == alert['id'])).first() is None


def test_recent_failure_threshold_excludes_old_failed_jobs(fixture):
    repo, _, now = fixture
    operations = Operations(repo.engine, clock=lambda: now[0], thresholds={'failed_last_hour': 2})
    seed_job(repo, offset=1, state='failed', updated=now[0] - 3601)
    seed_job(repo, offset=2, state='failed')
    assert operations.collect()['alerts_created'] == 0
    seed_job(repo, offset=3, state='failed')
    assert operations.collect()['alerts_created'] == 1
    assert operations.pending_alerts()[0]['code'] == 'FAILED_JOBS'
    assert operations.pending_alerts()[0]['count'] == 2


def test_missing_schema_fails_safely_without_database_or_secret_details():
    engine = create_engine('sqlite://')
    try:
        operations = Operations(engine)
        for operation in (operations.snapshot, operations.collect, operations.pending_alerts,
                          lambda: operations.acknowledge('a' * 32)):
            with pytest.raises(HTTPException) as unavailable:
                operation()
            assert unavailable.value.status_code == 503
            assert 'SELECT' not in unavailable.value.detail and 'sqlite' not in unavailable.value.detail
        with pytest.raises(ValueError):
            Operations(engine, thresholds={'arbitrary-pii': 1})
        with pytest.raises(ValueError):
            operations.pending_alerts(limit=1000)
    finally:
        engine.dispose()
