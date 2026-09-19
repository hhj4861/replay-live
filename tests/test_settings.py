"""Synthetic DB configuration checks; no connections or credentials are loaded."""
import os

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
from sqlalchemy.engine import make_url

from server.settings import Settings, database_url_from_env, normalize_database_url


SUFFIX = ('synthetic-user:p%40ss%2Fword@db.synthetic.invalid:5432/replay'
          '?sslmode=require&channel_binding=require&connect_timeout=10'
          '&options=-c%20statement_timeout%3D30000')


@pytest.fixture
def environment(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith('REPLAY_') or name == 'DATABASE_URL':
            monkeypatch.delenv(name)
    values = {'REPLAY_MODE': 'production', 'REPLAY_VERSION': 'synthetic-release',
              'REPLAY_PUBLIC_URL': 'https://api.synthetic.invalid', 'REPLAY_ORIGINS': 'https://web.synthetic.invalid',
              'REPLAY_CONTROL_TOKEN': 'synthetic-control-' + 'a' * 32,
              'REPLAY_CALLBACK_KEY': 'synthetic-callback-' + 'b' * 32,
              'REPLAY_BUCKET': 'synthetic-private', 'REPLAY_KMS_KEY_ID': 'synthetic-key'}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.mark.parametrize('scheme', ['postgres', 'postgresql', 'postgresql+psycopg'])
@pytest.mark.parametrize('name', ['DATABASE_URL', 'REPLAY_DATABASE_URL'])
def test_database_environment_normalization_preserves_all_connection_data(environment, monkeypatch, scheme, name):
    source = scheme + '://' + SUFFIX
    monkeypatch.setenv(name, source)
    cfg = Settings.from_env()
    assert cfg.database_url == 'postgresql+psycopg://' + SUFFIX
    assert source not in repr(cfg) and 'p%40ss%2Fword' not in repr(cfg)
    assert os.environ[name] == source  # No process-wide rewriting.


def test_explicit_replay_database_wins_over_auto_provisioned_url(environment, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://wrong-db.synthetic.invalid/other')
    monkeypatch.setenv('REPLAY_DATABASE_URL', 'postgres://' + SUFFIX)
    assert Settings.from_env().database_url == 'postgresql+psycopg://' + SUFFIX


@pytest.mark.parametrize('explicit', ['', 'sqlite:///:memory:', 'mysql://synthetic.invalid/replay'])
def test_invalid_explicit_database_never_silently_falls_back(environment, monkeypatch, explicit):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://' + SUFFIX)
    monkeypatch.setenv('REPLAY_DATABASE_URL', explicit)
    with pytest.raises(ValueError, match='Production requires PostgreSQL'):
        Settings.from_env()


def test_direct_settings_and_development_sqlite_remain_compatible():
    common = dict(mode='development', public_url='http://127.0.0.1:8000', origins=('http://127.0.0.1:3000',),
                  control_token='synthetic-control-' + 'a' * 32, callback_key='synthetic-callback-' + 'b' * 32)
    assert Settings(**common, database_url='postgres://' + SUFFIX).database_url == 'postgresql+psycopg://' + SUFFIX
    assert Settings(**common, database_url='sqlite:///:memory:').database_url == 'sqlite:///:memory:'
    assert normalize_database_url('postgresql+psycopg://' + SUFFIX) == 'postgresql+psycopg://' + SUFFIX
    assert database_url_from_env({}) == ''


def test_neon_tls_parameters_reach_psycopg_libpq_without_connecting():
    normalized = normalize_database_url('postgresql://' + SUFFIX)
    args, kwargs = PGDialect_psycopg().create_connect_args(make_url(normalized))
    assert args == []
    assert kwargs['password'] == 'p@ss/word'
    assert kwargs['sslmode'] == 'require' and kwargs['channel_binding'] == 'require'
    parsed = conninfo_to_dict(make_conninfo(**kwargs))
    assert parsed['sslmode'] == 'require' and parsed['channel_binding'] == 'require'
    assert parsed['options'] == '-c statement_timeout=30000'


def test_missing_database_does_not_select_an_implicit_local_database(environment):
    with pytest.raises(ValueError, match='Production requires PostgreSQL'):
        Settings.from_env()
