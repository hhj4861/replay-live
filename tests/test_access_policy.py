from concurrent.futures import ThreadPoolExecutor
import hashlib
import time

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, select, update

from server.access_policy import AccessPolicy, counters, revocations
from server.auth import Principal


def user(subject='user-a', tenant='tenant-a', token='token-a'):
    return Principal(subject, tenant, ('operator',), hashlib.sha256(token.encode()).hexdigest(), time.time() + 300)


@pytest.fixture
def policies(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'access.sqlite3'), connect_args={'timeout': 5})
    first = AccessPolicy(engine, mode='test', user_limit=2, tenant_limit=3)
    first.migrate()
    try:
        yield first, AccessPolicy(engine, mode='test', user_limit=2, tenant_limit=3)
    finally:
        engine.dispose()


def test_limits_are_shared_between_instances_and_isolate_users_and_tenants(policies):
    first, second = policies
    first.check(user())
    second.check(user())
    with pytest.raises(HTTPException) as limited:
        first.check(user())
    assert limited.value.status_code == 429 and int(limited.value.headers['Retry-After']) > 0
    second.check(user(subject='user-b', token='token-b'))
    with pytest.raises(HTTPException) as tenant_limit:
        second.check(user(subject='user-c', token='token-c'))
    assert tenant_limit.value.status_code == 429
    first.check(user(tenant='unrelated-tenant'))


def test_concurrent_admission_is_atomic(policies):
    first, second = policies
    def check(index):
        try:
            (first if index % 2 else second).check(user())
            return 200
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(check, range(12)))
    assert statuses.count(200) == 2 and statuses.count(429) == 10


def test_revocation_survives_new_policy_instances_and_is_token_scoped(policies):
    first, second = policies
    principal = user()
    first.revoke(principal)
    with pytest.raises(HTTPException) as denied:
        second.check(principal)
    assert denied.value.status_code == 401
    second.check(user(token='new-token-same-user'))
    with first.engine.connect() as connection:
        stored = connection.execute(select(revocations.c.token_hash)).scalar_one()
    assert stored != principal.token_id and len(stored) == 64


def test_expiration_cleanup_and_fail_closed_without_schema(policies):
    first, _ = policies
    first.check(user())
    first.revoke(user())
    with first.engine.begin() as connection:
        connection.execute(update(counters).values(reset_at=1))
        connection.execute(update(revocations).values(expires_at=1))
    assert first.purge_expired() == {'expired_revocations': 1, 'expired_access_counters': 2}
    fresh = create_engine('sqlite://')
    try:
        with pytest.raises(HTTPException) as unavailable:
            AccessPolicy(fresh, mode='test').check(user())
        assert unavailable.value.status_code == 503
        with pytest.raises(ValueError):
            AccessPolicy(fresh, mode='production')
    finally:
        fresh.dispose()
