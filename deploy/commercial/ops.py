"""Offline-first release helpers. Database credentials never enter command arguments."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from server.access_policy import AccessPolicy
from server.repository import Repository, metadata as repository_metadata
from server.access_policy import metadata as access_metadata
from server.operations import Operations, metadata as operations_metadata
from server.google_auth import discard_restored_sessions, metadata as google_metadata
from server.stream_connections import metadata as stream_connections_metadata
from server.device_imports import metadata as device_imports_metadata
from server.settings import database_url_from_env, normalize_database_url

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / 'migrations'
LOCK_ID = 749_302_116


def database_url(name='REPLAY_DATABASE_URL', *, development=False):
    value = database_url_from_env(os.environ) if name == 'REPLAY_DATABASE_URL' else normalize_database_url(os.environ.get(name, ''))
    if not value:
        raise ValueError(f'{name} must be set in the environment')
    try:
        parsed = make_url(value)
    except (ArgumentError, ValueError, TypeError):
        # SQLAlchemy parse failures must not retain a credential-bearing input.
        raise ValueError('Invalid database URL') from None
    if parsed.get_backend_name() != 'postgresql' and not (development and parsed.get_backend_name() == 'sqlite'):
        raise ValueError('PostgreSQL is required; SQLite needs explicit --development')
    return value


def pg_environment(value):
    parsed = make_url(value)
    if parsed.get_backend_name() != 'postgresql' or not parsed.database:
        raise ValueError('A PostgreSQL database URL is required')
    env = {key: val for key, val in os.environ.items() if not key.startswith('PG')}
    for key, val in {'PGHOST': parsed.host, 'PGPORT': parsed.port, 'PGDATABASE': parsed.database,
                     'PGUSER': parsed.username, 'PGPASSWORD': parsed.password}.items():
        if val is not None:
            env[key] = str(val)
    for key, target in {'sslmode': 'PGSSLMODE', 'sslrootcert': 'PGSSLROOTCERT',
                        'sslcert': 'PGSSLCERT', 'sslkey': 'PGSSLKEY', 'connect_timeout': 'PGCONNECT_TIMEOUT',
                        'channel_binding': 'PGCHANNELBINDING', 'target_session_attrs': 'PGTARGETSESSIONATTRS',
                        'application_name': 'PGAPPNAME', 'options': 'PGOPTIONS'}.items():
        if key in parsed.query:
            env[target] = parsed.query[key]
    env.setdefault('PGCONNECT_TIMEOUT', '10')
    return env


def private_json(path, value):
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')


def file_hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def database_identity(value):
    parsed = make_url(value)
    return hashlib.sha256(json.dumps([parsed.host, parsed.port or 5432, parsed.database]).encode()).hexdigest()


def summary(connection):
    """Only counts and hashed tenant identifiers, never media names or credentials."""
    names = set(inspect(connection).get_table_names())
    tables = sorted(set(repository_metadata.tables) | set(access_metadata.tables) | set(operations_metadata.tables) | set(google_metadata.tables) | set(stream_connections_metadata.tables) | set(device_imports_metadata.tables))
    missing = sorted(set(tables) - names)
    if missing:
        raise ValueError('Database migration is incomplete')
    counts, tenants = {}, {}
    for name in tables:
        counts[name] = connection.execute(text(f'SELECT COUNT(*) FROM {name}')).scalar_one()
        columns = {column['name'] for column in inspect(connection).get_columns(name)}
        if 'tenant_id' in columns:
            rows = connection.execute(text(f'SELECT tenant_id, COUNT(*) AS n FROM {name} GROUP BY tenant_id'))
            tenants[name] = {hashlib.sha256(row[0].encode()).hexdigest(): row[1] for row in rows}
    versions = [dict(row) for row in connection.execute(text('SELECT version,sha256 FROM replay_schema_migrations ORDER BY version')).mappings()]
    states = dict(connection.execute(text('SELECT state, COUNT(*) FROM replay_jobs GROUP BY state')).all())
    return {'table_counts': counts, 'tenant_counts': tenants, 'migrations': versions, 'job_states': states}


def check_schema(connection):
    inspector = inspect(connection)
    for meta in (repository_metadata, access_metadata, operations_metadata, google_metadata, stream_connections_metadata, device_imports_metadata):
        for table in meta.sorted_tables:
            actual = {column['name'] for column in inspector.get_columns(table.name)}
            if actual != set(table.columns.keys()):
                raise ValueError(f'Schema differs from release metadata: {table.name}')
    applied = {row[0]: row[1] for row in connection.execute(text('SELECT version,sha256 FROM replay_schema_migrations'))}
    expected = {path.name: file_hash(path) for path in sorted(MIGRATIONS.glob('*.sql'))}
    if applied != expected:
        raise ValueError('Migration version/checksum differs from this release')
    return True


def migrate(value, *, development=False):
    repo = Repository(value)
    try:
        if repo.sqlite:
            if not development:
                raise ValueError('SQLite needs explicit --development')
            repo.create_schema()
            repo.migrate_watch_links()
            # Explicit development migration only; create_all does not add
            # columns to an existing SQLite database from an earlier release.
            with repo.engine.begin() as connection:
                columns = {column['name'] for column in inspect(connection).get_columns('replay_jobs')}
                for name in ('output_budget_bytes', 'output_reserved_bytes'):
                    if name not in columns:
                        constraint = f'{name} >= 0' + (' AND output_reserved_bytes <= output_budget_bytes' if name == 'output_reserved_bytes' else '')
                        connection.exec_driver_sql(f'ALTER TABLE replay_jobs ADD COLUMN {name} BIGINT NOT NULL DEFAULT 0 CHECK ({constraint})')
            AccessPolicy(repo.engine, mode='development').migrate()
            Operations(repo.engine).migrate()
            google_metadata.create_all(repo.engine)
            stream_connections_metadata.create_all(repo.engine)
            device_imports_metadata.create_all(repo.engine)
        with repo.engine.begin() as connection:
            if not repo.sqlite:
                connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': LOCK_ID})
            connection.execute(text('CREATE TABLE IF NOT EXISTS replay_schema_migrations '
                                    '(version VARCHAR(200) PRIMARY KEY, sha256 VARCHAR(64) NOT NULL, applied_at DOUBLE PRECISION NOT NULL)'))
            applied = dict(connection.execute(text('SELECT version,sha256 FROM replay_schema_migrations')).all())
            for path in sorted(MIGRATIONS.glob('*.sql')):
                checksum = file_hash(path)
                if path.name in applied:
                    if applied[path.name] != checksum:
                        raise ValueError('An applied migration has changed; create a new version')
                    continue
                if not repo.sqlite:
                    connection.exec_driver_sql(path.read_text())
                connection.execute(text('INSERT INTO replay_schema_migrations(version,sha256,applied_at) VALUES (:version,:sha,:at)'),
                                   {'version': path.name, 'sha': checksum, 'at': time.time()})
            check_schema(connection)
        return {'migrated': True, 'versions': sorted(path.name for path in MIGRATIONS.glob('*.sql'))}
    finally:
        repo.close()


def run_pg(command, *, env, stdout=None):
    # Do not echo stderr: libpq errors can reveal connection or certificate details.
    result = subprocess.run(command, env=env, stdout=stdout or subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=1800)
    if result.returncode:
        raise RuntimeError(f'{command[0]} failed (exit {result.returncode}); inspect the database service privately')


def backup(value, output):
    output = Path(output).resolve()
    manifest_path = Path(str(output) + '.manifest.json')
    if output.exists() or manifest_path.exists():
        raise ValueError('Backup output and manifest must not already exist')
    repo = Repository(value)
    try:
        with repo.engine.connect().execution_options(isolation_level='REPEATABLE READ') as connection:
            with connection.begin():
                connection.execute(text('SET TRANSACTION READ ONLY'))
                check_schema(connection)
                snapshot = connection.execute(text('SELECT pg_export_snapshot()')).scalar_one()
                before = summary(connection)
                fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, 'wb') as target:
                        run_pg(['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--snapshot', snapshot],
                               env=pg_environment(value), stdout=target)
                except BaseException:
                    output.unlink(missing_ok=True)
                    raise
        manifest = {'format': 1, 'created_at': time.time(), 'source_database': database_identity(value),
                    'sha256': file_hash(output), 'bytes': output.stat().st_size, 'summary': before}
        private_json(manifest_path, manifest)
        return {'backup': str(output), 'manifest': str(manifest_path), 'sha256': manifest['sha256'], 'bytes': manifest['bytes']}
    finally:
        repo.close()


def restore(value, source, confirm_database):
    source = Path(source).resolve()
    manifest = json.loads(Path(str(source) + '.manifest.json').read_text())
    if manifest.get('format') != 1 or file_hash(source) != manifest.get('sha256'):
        raise ValueError('Backup checksum/manifest is invalid')
    if make_url(value).database != confirm_database or not confirm_database:
        raise ValueError('--confirm-database must exactly match the isolated destination database')
    if manifest['source_database'] == database_identity(value):
        raise ValueError('Restore cannot target the backup source database')
    repo = Repository(value)
    try:
        with repo.engine.connect() as guard:
            guard.execute(text('SELECT pg_advisory_lock(:key)'), {'key': LOCK_ID})
            try:
                occupied = guard.execute(text("SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
                    "AND c.relkind IN ('r','p','v','m','S','f')")).scalar_one()
                if occupied:
                    raise ValueError('Restore requires a fresh empty database; existing objects will never be overwritten')
                run_pg(['pg_restore', '--single-transaction', '--exit-on-error', '--no-owner', '--no-acl', '--dbname', '', str(source)],
                       env=pg_environment(value))
                with repo.engine.begin() as connection:
                    check_schema(connection)
                    if summary(connection) != manifest['summary']:
                        raise ValueError('Restored tenant/table counts or migrations differ from the backup')
                    interrupted = connection.execute(text("UPDATE replay_jobs SET state='failed',error_code='RESTORE_INTERRUPTED',"
                        "secret_ciphertext=NULL,lease_token=NULL,lease_expires=NULL,worker_id=NULL,updated=:at "
                        "WHERE state IN ('starting','streaming','stopping') RETURNING tenant_id,id,media_id,target,lease_version"), {'at': time.time()}).mappings().all()
                    for row in interrupted:
                        connection.execute(text('INSERT INTO replay_events(tenant_id,job_id,at,code) VALUES (:tenant,:job,:at,:code)'),
                                           {'tenant': row['tenant_id'], 'job': row['id'], 'at': time.time(), 'code': 'RESTORE_INTERRUPTED'})
                        # Match release-local terminal transitions: fail interrupted
                        # validation metadata and queue incomplete output deletion.
                        repo._finish_side_effects(connection, row, 'failed', 'RESTORE_INTERRUPTED', time.time())
                    connection.execute(text("UPDATE replay_runtimes SET state='failed',cleaned=TRUE,updated=:at"), {'at': time.time()})
                    connection.execute(text('DELETE FROM replay_workers'))
                    # A restored database must never resurrect browser sessions
                    # or unfinished sign-in challenges. Stable identities remain.
                    discarded_auth = discard_restored_sessions(connection)
                    connection.execute(text("UPDATE replay_device_imports SET token_hash='',source_ciphertext='',"
                        "state=CASE WHEN state='completed' THEN state ELSE 'cancelled' END"))
                return {'restored': True, 'verified_tenant_counts': True, 'interrupted_jobs_failed': len(interrupted),
                        'queued_jobs_preserved': True, 'scheduler_started': False, **discarded_auth}
            finally:
                guard.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK_ID})
    finally:
        repo.close()
