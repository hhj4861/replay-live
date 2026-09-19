"""Production SQL and PostgreSQL upsert behavior in an explicitly disposable schema."""
import os
import uuid

from cryptography.fernet import Fernet
from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, make_url, select, text

from deploy.commercial.ops import backup, migrate, restore
from server.secrets import LocalKeyringProvider
from server.stream_connections import StreamConnections, connections
from test_postgres import postgres  # noqa: F401: shared isolated-schema fixture
from test_stream_connections import SECRET, principal


pytestmark = pytest.mark.skipif(not os.environ.get('REPLAY_TEST_POSTGRES_URL'),
                                reason='Explicit disposable PostgreSQL URL is required')


def test_production_migration_persistence_and_account_scoped_upsert(postgres):
    repo, _, url = postgres
    keys = LocalKeyringProvider({'test': Fernet.generate_key()}, 'test', mode='test')
    service = StreamConnections(repo.engine, keys, mode='production')
    service.save(principal(), 'youtube', server_url='', stream_key=SECRET)
    service.save(principal(), 'youtube', server_url='', stream_key=SECRET + '-updated')
    service.save(principal('bob'), 'youtube', server_url='', stream_key='bobs-synthetic-key')
    assert len(service.list(principal())) == 1
    other_engine = create_engine(url)
    try:
        reopened = StreamConnections(other_engine, keys, mode='production')
        assert reopened.use(principal(), 'youtube')['stream_key'] == SECRET + '-updated'
        reopened.delete(principal('bob'), 'youtube')
        assert reopened.list(principal('bob')) == []
        assert len(reopened.list(principal())) == 1
        with other_engine.connect() as db:
            stored = db.execute(select(connections.c.secret_ciphertext)).scalar_one()
            assert SECRET not in stored
    finally:
        other_engine.dispose()


def test_postgres_backup_restore_preserves_encrypted_connections_and_owner(tmp_path):
    parsed = make_url(os.environ['REPLAY_TEST_POSTGRES_URL'])
    if parsed.get_backend_name() != 'postgresql' or not (parsed.database or '').startswith('replay_test'):
        pytest.fail('Restore requires an explicit disposable replay_test* PostgreSQL URL')
    suffix = uuid.uuid4().hex[:12]
    names = [f'replay_test_connection_source_{suffix}', f'replay_test_connection_restore_{suffix}']
    owner = create_engine(parsed)
    created, engines = [], []
    key = Fernet.generate_key()
    try:
        with owner.connect().execution_options(isolation_level='AUTOCOMMIT') as db:
            for name in names:
                db.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
        source_url, restored_url = [parsed.set(database=name).render_as_string(hide_password=False) for name in names]
        migrate(source_url)
        source_engine = create_engine(source_url)
        engines.append(source_engine)
        source = StreamConnections(source_engine, LocalKeyringProvider({'test': key}, 'test', mode='test'))
        source.save(principal(), 'youtube', server_url='', stream_key=SECRET)
        dump = tmp_path / 'connections.dump'
        backup(source_url, dump)
        result = restore(restored_url, dump, names[1])
        assert result['restored'] and result['scheduler_started'] is False
        restored_engine = create_engine(restored_url)
        engines.append(restored_engine)
        restored = StreamConnections(restored_engine, LocalKeyringProvider({'test': key}, 'test', mode='test'))
        assert restored.use(principal(), 'youtube')['stream_key'] == SECRET
        assert 'stream_key' not in restored.list(principal())[0]
        for user in (principal('bob'), principal(tenant='team-b')):
            assert restored.list(user) == []
            with pytest.raises(HTTPException) as caught:
                restored.use(user, 'youtube')
            assert caught.value.status_code == 404
    finally:
        for engine in engines:
            engine.dispose()
        try:
            with owner.connect().execution_options(isolation_level='AUTOCOMMIT') as db:
                for name in reversed(created):
                    db.execute(text(f'DROP DATABASE "{name}"'))
        finally:
            owner.dispose()
