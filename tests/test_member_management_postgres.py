"""Cross-service member races; only an explicit disposable replay_test* PG DB."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import threading
import uuid

from fastapi import HTTPException
import httpx
import pytest
from sqlalchemy import create_engine, func, make_url, select, text

from deploy.commercial import ops
from server.google_auth import GoogleAuthConfig, GoogleAuthenticator, identities, member_audit
from server.member_management import MemberManagement
from server.repository import Repository, jobs


URL = os.environ.get('REPLAY_TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not URL, reason='Explicit disposable PostgreSQL URL is required')


@pytest.fixture
def members_pg():
    parsed = make_url(URL)
    if parsed.get_backend_name() != 'postgresql' or not (parsed.database or '').startswith('replay_test'):
        pytest.fail('Member PostgreSQL tests require a disposable replay_test* database')
    schema = 'members_test_' + uuid.uuid4().hex
    owner = create_engine(parsed)
    services, repositories, clients = [], [], []
    with owner.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        scoped = parsed.update_query_dict({'options': f'-csearch_path={schema} -clock_timeout=10000 -cstatement_timeout=15000'})
        value = scoped.render_as_string(hide_password=False)
        ops.migrate(value)
        for _ in range(2):
            repo = Repository(value, tenant_concurrency=4, global_concurrency=8)
            repositories.append(repo)
            def forbidden(request):
                raise AssertionError('Synthetic member tests must not contact Google')
            client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
            clients.append(client)
            auth = GoogleAuthenticator(repo.engine, GoogleAuthConfig(
                '1234567890-memberpg.apps.googleusercontent.com', allow_signups=True,
                site_admin_emails=('synthetic.admin@gmail.com', 'synthetic.second@gmail.com')), http_client=client)
            manager = MemberManagement(auth, repo)
            repo.admission_guard = manager.check_admission
            services.append((auth, manager))
        auth, _ = services[0]
        accounts = {}
        for name in ('admin', 'second', 'member'):
            subject = 'synthetic-pg-' + name
            tenant = 'google-' + hashlib.sha256(subject.encode()).hexdigest()[:40]
            with auth._transaction() as connection:
                now = auth._now(connection)
                connection.execute(identities.insert().values(subject=subject, tenant_id=tenant,
                    email='synthetic.' + name + '@gmail.com', email_authoritative=True,
                    roles='["operator"]', enabled=True, created_at=now, updated_at=now))
                session = auth._mint(connection, subject, now, now + 86400)
            authorization = 'Bearer ' + session['token']
            accounts[name] = {'id': tenant, 'subject': subject, 'authorization': authorization,
                              'principal': asyncio.run(auth.authenticate(authorization))}
        yield repositories, services, accounts
    finally:
        for client in clients:
            asyncio.run(client.aclose())
        for repo in repositories:
            repo.close()
        with owner.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        owner.dispose()


def ready_media(repo, account):
    return repo.add_media(account['id'], name='synthetic.mp4', object_key='replay/synthetic/' + uuid.uuid4().hex + '.mp4',
                          bytes=100, duration=3, width=320, height=180, fps=30)


def test_suspension_races_refresh_without_deadlock_or_restoring_any_session(members_pg):
    repos, services, accounts = members_pg
    first, second = services
    barrier = threading.Barrier(2)
    def suspend():
        barrier.wait(timeout=5)
        return first[1].set_enabled(accounts['admin']['principal'], accounts['member']['id'], False)
    def rotate():
        barrier.wait(timeout=5)
        try:
            return second[0].refresh(accounts['member']['authorization'])
        except HTTPException as error:
            assert error.status_code in (401, 403)
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        stopping, refreshing = pool.submit(suspend), pool.submit(rotate)
        assert stopping.result(timeout=15)['enabled'] is False
        rotated = refreshing.result(timeout=15)
    candidates = [accounts['member']['authorization']] + (['Bearer ' + rotated['token']] if rotated else [])
    for authorization in candidates:
        with pytest.raises(HTTPException) as error:
            asyncio.run(second[0].authenticate(authorization))
        assert error.value.status_code in (401, 403)
    assert asyncio.run(second[0].authenticate(accounts['admin']['authorization'])).roles == ('operator', 'site_admin')
    with repos[0].engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(member_audit)).scalar_one() == 1


@pytest.mark.parametrize('creation_first', [True, False])
def test_suspend_and_admission_serialize_in_both_orders(members_pg, creation_first, monkeypatch):
    repos, services, accounts = members_pg
    item = ready_media(repos[0], accounts['member'])
    holding, release = threading.Event(), threading.Event()
    original_guard, original_audit = repos[1].admission_guard, services[0][1]._audit
    def hold_guard(connection, tenant_id):
        original_guard(connection, tenant_id)
        holding.set()
        assert release.wait(timeout=5)
    def hold_audit(*args, **kwargs):
        original_audit(*args, **kwargs)
        holding.set()
        assert release.wait(timeout=5)
    if creation_first:
        repos[1].admission_guard = hold_guard
    else:
        monkeypatch.setattr(services[0][1], '_audit', hold_audit)
    def create():
        try:
            return repos[1].create_job(accounts['member']['id'], media_id=item['id'],
                title='Synthetic admission race', target='local', idempotency_key='race')
        except HTTPException as error:
            return error.status_code
    def suspend():
        return services[0][1].set_enabled(accounts['admin']['principal'], accounts['member']['id'], False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(create if creation_first else suspend)
        assert holding.wait(timeout=5)
        second = pool.submit(suspend if creation_first else create)
        release.set()
        first_result, second_result = first.result(timeout=15), second.result(timeout=15)
    created = first_result if creation_first else second_result
    if creation_first:
        assert repos[0].get_job(accounts['member']['id'], created['id'])['state'] == 'stopped'
    else:
        assert created == 403
        with repos[0].engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(jobs)).scalar_one() == 0


def test_two_admins_cannot_suspend_each_other_and_leave_no_admin(members_pg):
    _, services, accounts = members_pg
    barrier = threading.Barrier(2)
    def suspend(index):
        actor, target = ('admin', 'second') if index == 0 else ('second', 'admin')
        barrier.wait(timeout=5)
        try:
            return services[index][1].set_enabled(accounts[actor]['principal'], accounts[target]['id'], False)
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(suspend, range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1 and results.count(403) == 1
    auth, _ = services[0]
    with auth.engine.connect() as connection:
        admins = connection.execute(select(identities).where(identities.c.enabled.is_(True))).mappings().all()
        assert sum(auth.is_site_admin(row) for row in admins) == 1
        assert ops.check_schema(connection)
        summary = ops.summary(connection)
        assert summary['table_counts']['replay_member_audit'] == 1
        assert any(row['version'] == '009_member_management.sql' for row in summary['migrations'])
        assert 'synthetic.admin@gmail.com' not in json.dumps(summary)
