import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import pytest

from deploy.commercial.ops import (backup, database_identity, database_url, file_hash, migrate, pg_environment,
                                   private_json, restore, summary)
from server.repository import LeaseLost, Repository, object_deletions, upload_reservations


def test_explicit_migration_is_repeatable_and_reports_schema(tmp_path):
    value = f'sqlite:///{tmp_path}/release.db'
    assert migrate(value, development=True)['migrated']
    assert migrate(value, development=True)['migrated']
    repo = Repository(value)
    try:
        with repo.engine.connect() as connection:
            result = summary(connection)
        assert result['table_counts']['replay_jobs'] == 0
        assert result['table_counts']['replay_coordination'] == 1
        assert len(result['migrations']) >= 2
    finally:
        repo.close()


def test_development_migration_upgrades_legacy_jobs_and_preserves_queue(tmp_path):
    import time
    from sqlalchemy import create_engine, inspect, insert, text
    from sqlalchemy.dialects.sqlite import dialect
    from sqlalchemy.schema import CreateTable
    from server.repository import jobs, media, metadata

    url = f'sqlite:///{tmp_path}/legacy.db'
    engine = create_engine(url)
    # Reconstruct the preceding SQLite table definition without 004's fields
    # or constraints; all original columns and foreign keys remain intact.
    statement = str(CreateTable(jobs).compile(dialect=dialect()))
    statement = '\n'.join(line for line in statement.splitlines() if not any(value in line for value in
        ('output_budget_bytes', 'output_reserved_bytes', 'ck_replay_jobs_output_budget', 'ck_replay_jobs_output_reserved')))
    with engine.begin() as connection:
        connection.exec_driver_sql(statement)
    metadata.create_all(engine)
    now = time.time()
    with engine.begin() as connection:
        connection.execute(insert(media).values(id='legacy-media', tenant_id='a', name='legacy.mp4',
            object_key='a/legacy.mp4', bytes=100, duration=15, width=320, height=180, fps=30,
            sha256='', etag='', status='ready', created=now, updated=now))
        values = dict(id='legacy-job', tenant_id='a', media_id='legacy-media', title='Preserved queue', target='local',
            idempotency_key='legacy-idempotency', payload_hash='a' * 64, scheduled=now,
            reserved_until=now + 600, deadline=now + 600, state='scheduled', progress=0, attempt=0,
            max_attempts=1, next_run=now, lease_version=0, cancel_requested=False, output_bytes=0, created=now, updated=now)
        connection.execute(text('INSERT INTO replay_jobs (' + ','.join(values) + ') VALUES (' + ','.join(':' + name for name in values) + ')'), values)
        assert 'output_budget_bytes' not in {column['name'] for column in inspect(connection).get_columns('replay_jobs')}
    engine.dispose()
    assert '004_output_budget.sql' in migrate(url, development=True)['versions']
    assert migrate(url, development=True)['migrated']
    repo = Repository(url)
    try:
        row = repo.get_job('a', 'legacy-job')
        assert row['title'] == 'Preserved queue' and row['state'] == 'scheduled'
        assert row['output_budget_bytes'] == row['output_reserved_bytes'] == 0
        claim = repo.claim('development-upgrade-test')
        assert claim['id'] == 'legacy-job' and claim['output_reserved_bytes'] > 0
    finally:
        repo.close()


def test_postgres_credentials_stay_in_environment(monkeypatch):
    monkeypatch.setenv('PGSERVICE', 'unrelated')
    value = 'postgresql+psycopg://operator:p%40ss@db.example:5432/replay?sslmode=verify-full'
    env = pg_environment(value)
    assert env['PGPASSWORD'] == 'p@ss' and env['PGDATABASE'] == 'replay'
    assert env['PGSSLMODE'] == 'verify-full' and 'PGSERVICE' not in env


def test_database_command_default_uses_provisioned_url_and_preserves_tls(monkeypatch):
    monkeypatch.delenv('REPLAY_DATABASE_URL', raising=False)
    source = 'postgres://operator:p%40ss@db.synthetic.invalid/replay?sslmode=require&channel_binding=require'
    monkeypatch.setenv('DATABASE_URL', source)
    assert database_url() == source.replace('postgres://', 'postgresql+psycopg://', 1)
    assert database_url('DATABASE_URL') == database_url()
    env = pg_environment(database_url())
    assert env['PGSSLMODE'] == 'require' and env['PGCHANNELBINDING'] == 'require'
    assert env['PGPASSWORD'] == 'p@ss'
    monkeypatch.setenv('REPLAY_DATABASE_URL', 'postgresql://explicit.synthetic.invalid/replay')
    assert database_url() == 'postgresql+psycopg://explicit.synthetic.invalid/replay'
    monkeypatch.setenv('REPLAY_DATABASE_URL', '')
    with pytest.raises(ValueError, match='must be set'):
        database_url()


def test_explicit_database_env_does_not_fall_back_to_runtime_database(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://unrelated.synthetic.invalid/replay')
    monkeypatch.delenv('REPLAY_RESTORE_DATABASE_URL', raising=False)
    with pytest.raises(ValueError, match='must be set'):
        database_url('REPLAY_RESTORE_DATABASE_URL')
    monkeypatch.setenv('REPLAY_RESTORE_DATABASE_URL', 'postgresql://restore.synthetic.invalid/replay')
    assert database_url('REPLAY_RESTORE_DATABASE_URL') == 'postgresql+psycopg://restore.synthetic.invalid/replay'


def test_invalid_database_input_error_contains_no_connection_value(monkeypatch):
    monkeypatch.setenv('REPLAY_DATABASE_URL', 'sensitive-database-canary')
    with pytest.raises(ValueError, match='Invalid database URL') as caught:
        database_url()
    assert 'sensitive-database-canary' not in str(caught.value)
    assert caught.value.__suppress_context__


def test_backup_manifest_private_and_restore_cannot_target_source(tmp_path):
    source = tmp_path / 'fixture.dump'
    source.write_bytes(b'synthetic-custom-dump')
    value = 'postgresql+psycopg://operator:synthetic@db.example:5432/replay'
    manifest = Path(str(source) + '.manifest.json')
    private_json(manifest, {'format': 1, 'sha256': file_hash(source), 'source_database': database_identity(value)})
    assert manifest.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='source database'):
        restore(value, source, 'replay')
    source.write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum'):
        restore(value, source, 'replay')


def test_verifier_rejects_unapproved_live_or_long_run():
    script = str(Path(__file__).resolve().parents[2] / 'scripts/commercial-verify.py')
    env = {key: value for key, value in os.environ.items() if key not in ('REPLAY_ALLOW_LIVE_YOUTUBE', 'REPLAY_YOUTUBE_STREAM_KEY')}
    for args in (['--live-youtube'], ['--soak-seconds', '3600']):
        result = subprocess.run([sys.executable, script, *args], env=env, capture_output=True, text=True)
        assert result.returncode == 1
        assert json.loads(result.stderr)['operation'] == 'verification_failed'


@pytest.mark.skipif(not os.environ.get('REPLAY_TEST_POSTGRES_URL'), reason='Explicit disposable PostgreSQL URL is required')
def test_postgres_backup_restore_preserves_queued_budget_and_settles_interrupted_holds(tmp_path):
    """Run actual pg_dump/pg_restore in two newly created, isolated databases."""
    from sqlalchemy import create_engine, make_url, select, text
    from server.output_policy import estimate_output_bytes

    parsed = make_url(os.environ['REPLAY_TEST_POSTGRES_URL'])
    if parsed.get_backend_name() != 'postgresql' or not (parsed.database or '').startswith('replay_test'):
        pytest.fail('Restore drill requires an explicit disposable replay_test* PostgreSQL URL')
    suffix = uuid.uuid4().hex[:12]
    names = [f'replay_test_budget_source_{suffix}', f'replay_test_budget_restore_{suffix}']
    owner = create_engine(parsed)
    created, instances = [], []
    evidence = {'executed_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'synthetic PostgreSQL output-budget backup/restore; no object storage or publishing',
        'databases_created': 0, 'databases_dropped': 0, 'passed': False}
    try:
        with owner.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            evidence['postgres_version'] = connection.execute(text('SHOW server_version')).scalar_one()
            for name in names:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
                evidence['databases_created'] += 1
        source_url, destination_url = [parsed.set(database=name).render_as_string(hide_password=False) for name in names]
        evidence['migration_versions'] = migrate(source_url)['versions']
        source = Repository(source_url)
        instances.append(source)

        def add_item(tenant):
            return source.add_media(tenant, name='synthetic.mp4', object_key=f'replay/{tenant}/media/synthetic.mp4',
                bytes=1000, duration=15, width=1280, height=720, fps=30)

        def create(item, key, scheduled=None):
            return source.create_job(item['tenant_id'], media_id=item['id'], title='Synthetic restore budget',
                target='local', idempotency_key=key, scheduled=scheduled)

        first_item = add_item('budget-tenant-a')
        active = create(first_item, 'active')
        queued = create(first_item, 'future-queued', scheduled=time.time() + 86400)
        upload_active = create(add_item('budget-tenant-b'), 'active-with-put')
        claims = {claim['id']: claim for claim in [source.claim('synthetic-worker-1', lease_seconds=900),
                                                  source.claim('synthetic-worker-2', lease_seconds=900)]}
        assert set(claims) == {active['id'], upload_active['id']}
        pending = source.reserve_output(upload_active['id'], claims[upload_active['id']]['lease_token'],
            object_key='replay/budget-tenant-b/outputs/synthetic.flv', bytes=2000, sha256='a' * 64)
        source.tenant_concurrency = 4
        batch_item = add_item('budget-tenant-c')
        batch_request = dict(media_id=batch_item['id'], title='Queued group survives restore',
            idempotency_key='restore-batch-key', scheduled=time.time() + 2 * 86400,
            destinations=[{'target': 'local'}, {'target': 'youtube', 'secret_ciphertext': 'synthetic-opaque-cipher'}])
        original_batch = source.create_jobs_batch('budget-tenant-c', **batch_request)
        budget = estimate_output_bytes(15)
        source_usage = {tenant: source.usage(tenant) for tenant in ('budget-tenant-a', 'budget-tenant-b', 'budget-tenant-c')}
        assert source_usage['budget-tenant-a']['storage_bytes'] == 1000 + 2 * budget
        assert source_usage['budget-tenant-b']['storage_bytes'] == 1000 + budget
        dump = tmp_path / 'output-budget.dump'
        result = backup(source_url, dump)
        evidence['backup'] = {'bytes': result['bytes'], 'sha256': result['sha256']}
        restored = restore(destination_url, dump, names[1])
        assert restored['interrupted_jobs_failed'] == 2 and restored['queued_jobs_preserved'] is True
        destination = Repository(destination_url)
        instances.append(destination)
        rows = {'active_without_put': destination.get_job('budget-tenant-a', active['id']),
                'queued_local': destination.get_job('budget-tenant-a', queued['id']),
                'active_with_put': destination.get_job('budget-tenant-b', upload_active['id'])}
        for label in ('active_without_put', 'active_with_put'):
            row = rows[label]
            assert row['state'] == 'failed' and row['error_code'] == 'RESTORE_INTERRUPTED'
            assert row['output_budget_bytes'] == budget and row['output_reserved_bytes'] == 0
        preserved = rows['queued_local']
        assert preserved['state'] == 'scheduled'
        assert preserved['output_budget_bytes'] == preserved['output_reserved_bytes'] == budget
        assert preserved['scheduled'] == queued['scheduled'] and preserved['attempt'] == 0
        assert destination.usage('budget-tenant-a')['storage_bytes'] == 1000 + budget
        assert destination.usage('budget-tenant-b')['storage_bytes'] == 3000
        assert destination.usage('budget-tenant-b')['storage_reserved_bytes'] == 2000
        restored_batch = destination.create_jobs_batch('budget-tenant-c', **batch_request)
        assert restored_batch['replayed'] is True
        assert [row['id'] for row in restored_batch['jobs']] == [row['id'] for row in original_batch['jobs']]
        assert all(row['state'] == 'scheduled' for row in restored_batch['jobs'])
        assert destination.usage('budget-tenant-c')['storage_reserved_bytes'] == budget
        with destination.engine.connect() as connection:
            liability = connection.execute(select(upload_reservations)).mappings().one()
            deletion = connection.execute(select(object_deletions)).mappings().one()
        assert liability['bytes'] == 2000 and liability['status'] == 'pending'
        assert deletion['object_key'] == pending['object_key']
        assert deletion['not_before'] >= pending['expires_at']
        assert destination.pending_deletions() == []
        for original in (active, upload_active):
            with pytest.raises(LeaseLost):
                destination.finish(original['id'], claims[original['id']]['lease_token'], state='failed')
        assert {tenant: source.usage(tenant) for tenant in source_usage} == source_usage
        assert source.get_job('budget-tenant-a', active['id'])['state'] == 'starting'
        assert source.get_job('budget-tenant-b', upload_active['id'])['state'] == 'starting'
        evidence.update(passed=True, estimated_output_bytes=budget, restored=restored,
            restored_jobs={label: {key: row[key] for key in ('state', 'error_code', 'output_budget_bytes', 'output_reserved_bytes')}
                           for label, row in rows.items()},
            pending_put={'retained_bytes': 2000, 'deletion_waits_for_authorization_expiry': True},
            broadcast_batch={'jobs_preserved': 2, 'idempotent_replay': True, 'local_output_budget_preserved': True},
            source_unchanged=True, stale_callbacks_rejected=True)
    finally:
        for instance in instances:
            instance.close()
        try:
            with owner.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
                for name in reversed(created):
                    connection.execute(text(f'DROP DATABASE "{name}"'))
                    evidence['databases_dropped'] += 1
        finally:
            owner.dispose()
        output = os.environ.get('REPLAY_POSTGRES_BUDGET_EVIDENCE')
        if output:
            Path(output).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
