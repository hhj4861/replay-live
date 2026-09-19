import hashlib
import json
import time

from fastapi import HTTPException
import pytest

from server.public_access import PublicAccess

CODE = 'synthetic-invitation-for-tests'


@pytest.fixture
def access(tmp_path):
    instance = PublicAccess(tmp_path / 'public', invite_code=CODE, cleanup_interval=3600)
    try:
        yield instance
    finally:
        instance.close()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def test_failed_login_scope_does_not_lock_out_other_clients(access):
    for _ in range(20):
        with pytest.raises(HTTPException) as rejected:
            access.login('incorrect', client_key='attacker')
        assert rejected.value.status_code == 401
    with pytest.raises(HTTPException) as throttled:
        access.login(CODE, client_key='attacker')
    assert throttled.value.status_code == 429
    assert access.login(CODE, client_key='customer')['token']


def test_resume_preserves_namespace_and_does_not_consume_capacity(access, monkeypatch):
    monkeypatch.setattr('server.public_access.MAX_ACTIVE_SESSIONS', 2)
    first, second = access.login(CODE), access.login(CODE)
    service = access.authenticate('Bearer ' + first['token'])
    resumed = access.login(CODE, resume_token=first['token'])
    assert resumed == first and len(access.sessions) == 2
    assert access.authenticate('Bearer ' + resumed['token']) is service
    with pytest.raises(HTTPException) as capacity:
        access.login(CODE)
    assert capacity.value.status_code == 429
    with pytest.raises(HTTPException) as denied:
        access.login('incorrect', resume_token=second['token'])
    assert denied.value.status_code == 401
    with pytest.raises(HTTPException) as missing:
        access.login(CODE, resume_token='unknown-synthetic-token')
    assert missing.value.status_code == 401


def test_expired_idle_worker_stops_and_is_not_restored_but_data_remains(tmp_path):
    root = tmp_path / 'public'
    access = PublicAccess(root, invite_code=CODE, cleanup_interval=3600)
    session = access.login(CODE)
    owner = digest(session['token'])
    service = access.services[owner]
    access.sessions[owner] = time.time() - 1
    access.session_path.write_text(json.dumps(access.sessions))
    assert access.cleanup_expired() == 1
    assert not service.scheduler.is_alive() and owner not in access.services
    assert (root / owner / 'replay.sqlite3').is_file()
    access.close()
    restored = PublicAccess(root, invite_code=CODE, cleanup_interval=3600)
    try:
        assert owner in restored.sessions and owner not in restored.services
        with pytest.raises(HTTPException):
            restored.login(CODE, resume_token=session['token'])
    finally:
        restored.close()


def test_expiry_cleanup_preserves_active_and_queued_jobs(access, monkeypatch):
    session = access.login(CODE)
    owner = digest(session['token'])
    service = access.services[owner]
    access.sessions[owner] = time.time() - 1
    for state in ('starting', 'streaming', 'stopping', 'scheduled', 'retry_wait'):
        monkeypatch.setattr(service, 'list_jobs', lambda state=state: [{'state': state}])
        assert access.cleanup_expired() == 0
        assert owner in access.services and service.scheduler.is_alive()
    monkeypatch.setattr(service, 'list_jobs', lambda: [{'state': 'completed'}])
    assert access.cleanup_expired() == 1
