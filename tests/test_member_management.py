"""Real account routes/transactions with synthetic identities, AES and no cloud calls."""
import asyncio
import hashlib
import json
import logging
from dataclasses import replace

from fastapi import HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, insert, select, update

from deploy.commercial import ops
from server.auth import Principal
from server.google_auth import GoogleAuthConfig, GoogleAuthenticator, identities, member_audit, discard_restored_sessions
from server.production_app import create_production_app
from server.repository import Repository
from server.secrets import EnvironmentAESGCMKeyProvider
from server.settings import Settings
from server.storage import LocalStorage
from server.stream_connections import connections


CLIENT_ID = '1234567890-membertest.apps.googleusercontent.com'
ORIGIN = 'http://member-ui.test'
ADMIN_EMAILS = ('synthetic.admin@gmail.com', 'synthetic.second@gmail.com', 'synthetic.untrusted@example.com')


@pytest.fixture
def members_api(tmp_path):
    cfg = Settings(mode='development', database_url=f'sqlite:///{tmp_path}/members.sqlite3',
        public_url='http://testserver', origins=(ORIGIN,), control_token='c' * 32,
        callback_key='k' * 32, local_root=str(tmp_path), version='members-test', tenant_concurrency=4)
    repo = Repository(cfg.database_url, create_schema=True, tenant_concurrency=4, global_concurrency=8)
    storage = LocalStorage(tmp_path / 'objects', signing_key=cfg.callback_key,
        base_url=cfg.public_url, allow_development=True)
    keys = EnvironmentAESGCMKeyProvider({'v1': b'\x12' * 32}, 'v1')

    def unexpected_network(request):
        raise AssertionError('No Google/network requests are permitted')
    http = httpx.AsyncClient(transport=httpx.MockTransport(unexpected_network))
    auth = GoogleAuthenticator(repo.engine, GoogleAuthConfig(CLIENT_ID, allow_signups=True,
        site_admin_emails=ADMIN_EMAILS), mode='test', http_client=http)
    auth.migrate()
    accounts = {}
    for index, (name, email, roles, authoritative) in enumerate([
        ('admin', ADMIN_EMAILS[0], ['operator'], True),
        ('second', ADMIN_EMAILS[1], ['operator'], True),
        ('member', 'synthetic.member@gmail.com', ['operator'], True),
        ('tenant_admin', 'synthetic.tenant@gmail.com', ['admin'], True),
        ('untrusted', ADMIN_EMAILS[2], ['admin'], False),
    ]):
        subject = 'synthetic-' + name
        tenant = 'google-' + hashlib.sha256(subject.encode()).hexdigest()[:40]
        with auth._transaction() as connection:
            now = auth._now(connection)
            connection.execute(insert(identities).values(subject=subject, tenant_id=tenant, email=email,
                email_authoritative=authoritative, roles=json.dumps(roles), enabled=True,
                created_at=now + index, updated_at=now + index))
            session = auth._mint(connection, subject, now, now + 86400)
        accounts[name] = {'id': tenant, 'subject': subject, 'email': email,
                          'headers': {'Authorization': 'Bearer ' + session['token']}, 'token': session['token']}
    app = create_production_app(cfg, repository=repo, storage=storage, keys=keys, authenticator=auth)
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    app.state.stream_connections.migrate()
    with TestClient(app, headers={'Origin': ORIGIN, 'X-Replay-Client': '1'}) as client:
        yield client, app, auth, accounts, keys
    asyncio.run(http.aclose())


def member_path(accounts, name='member'):
    return '/api/admin/members/' + accounts[name]['id']


def save(client, accounts, name='member', target='twitch', key='synthetic-stream-key'):
    response = client.put('/api/stream-connections/' + target, headers=accounts[name]['headers'],
        json={'stream_key': key, 'server_url': 'rtmps://live.twitch.tv:443/app'})
    assert response.status_code == 200, response.text
    return response


def add_job(repo, account, *, idem, target='local'):
    item = repo.add_media(account['id'], name='synthetic.mp4',
        object_key='replay/' + account['id'] + '/' + idem + '.mp4', bytes=100,
        duration=3, width=320, height=180, fps=30)
    return repo.create_job(account['id'], media_id=item['id'], title='Synthetic member work',
        target=target, idempotency_key=idem, secret_ciphertext='synthetic-ciphertext' if target == 'twitch' else None)


def test_me_bootstraps_only_authoritative_explicit_admins(members_api):
    client, _, auth, accounts, _ = members_api
    for name in accounts:
        response = client.get('/api/me', headers=accounts[name]['headers'])
        assert response.status_code == 200
        body = response.json()
        expected = name in ('admin', 'second')
        assert body['permissions']['manage_members'] is expected
        assert ('site_admin' in body['roles']) is expected
        assert body['profile']['id'] == accounts[name]['id'] == body['tenant_id']
        assert body['profile']['email'] == accounts[name]['email']
    auth.config = replace(auth.config, site_admin_emails=())
    assert client.get('/api/me', headers=accounts['admin']['headers']).json()['permissions']['manage_members'] is False
    assert client.get('/api/admin/members', headers=accounts['admin']['headers']).status_code == 403


@pytest.mark.parametrize('name', ['member', 'tenant_admin', 'untrusted'])
def test_non_site_admin_cannot_read_or_mutate_other_members(members_api, name):
    client, _, auth, accounts, _ = members_api
    headers = accounts[name]['headers']
    path = member_path(accounts)
    for method, url, payload in [('GET', '/api/admin/members', None), ('GET', path, None),
        ('PUT', path + '/status', {'enabled': False}), ('POST', path + '/revoke-sessions', {}),
        ('DELETE', path + '/stream-connections/twitch', None)]:
        response = client.request(method, url, headers=headers, json=payload)
        assert response.status_code == 403
        assert accounts['member']['email'] not in response.text
    with auth.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(member_audit)).scalar_one() == 0


def test_pagination_search_and_detail_never_load_connection_plaintext(members_api, monkeypatch):
    client, _, auth, accounts, keys = members_api
    save(client, accounts)
    headers = accounts['admin']['headers']
    def forbidden_decrypt(*args, **kwargs):
        raise AssertionError('Administrators must never decrypt member connections')
    monkeypatch.setattr(keys, 'decrypt', forbidden_decrypt)
    listing = client.get('/api/admin/members?offset=1&limit=2', headers=headers)
    assert listing.status_code == 200
    body = listing.json()
    assert body['total'] == 5 and body['offset'] == 1 and body['limit'] == 2 and len(body['items']) == 2
    result = client.get('/api/admin/members', params={'q': 'MEMBER@GMAIL'}, headers=headers).json()
    assert len(result['items']) == 1 and result['items'][0]['id'] == accounts['member']['id']
    assert result['items'][0]['stream_connection_count'] == 1
    assert result['items'][0]['active_session_count'] == 1
    assert client.get('/api/admin/members', params={'q': '%'}, headers=headers).json()['total'] == 0
    detail = client.get(member_path(accounts), headers=headers)
    assert detail.status_code == 200
    assert detail.json()['connections'] == [{'target': 'twitch', 'updated_at': detail.json()['connections'][0]['updated_at'], 'has_stream_key': True}]
    for forbidden in ('synthetic-stream-key', 'secret_ciphertext', 'server_url', 'subject_hash', 'token_hash', 'family_id'):
        assert forbidden not in detail.text and forbidden not in listing.text
    with auth.engine.connect() as connection:
        ciphertext = connection.execute(select(connections.c.secret_ciphertext)).scalar_one()
        assert json.loads(ciphertext)['provider'] == 'env-aesgcm'
        assert 'synthetic-stream-key' not in ciphertext


def test_only_owner_use_decrypts_and_admin_can_only_delete_exact_owner_target(members_api):
    client, app, auth, accounts, _ = members_api
    save(client, accounts)
    save(client, accounts, 'tenant_admin', key='synthetic-other-key')
    own = client.post('/api/stream-connections/twitch/use', headers=accounts['member']['headers'])
    assert own.status_code == 200 and own.json()['stream_key'] == 'synthetic-stream-key'
    assert client.post(member_path(accounts) + '/stream-connections/twitch/use', headers=accounts['admin']['headers']).status_code == 404
    # An unrelated owner within the same tenant must not be included or deleted.
    alternate = Principal('synthetic-unrelated-subject', accounts['member']['id'], ('operator',))
    app.state.stream_connections.save(alternate, 'twitch', server_url='', stream_key='synthetic-hidden-owner')
    response = client.delete(member_path(accounts) + '/stream-connections/twitch', headers=accounts['admin']['headers'])
    assert response.status_code == 204 and response.headers['cache-control'] == 'no-store'
    assert client.post('/api/stream-connections/twitch/use', headers=accounts['member']['headers']).status_code == 404
    assert client.post('/api/stream-connections/twitch/use', headers=accounts['tenant_admin']['headers']).json()['stream_key'] == 'synthetic-other-key'
    assert app.state.stream_connections.use(alternate, 'twitch')['stream_key'] == 'synthetic-hidden-owner'
    with auth.engine.connect() as connection:
        audit = dict(connection.execute(select(member_audit)).mappings().one())
        assert audit['action'] == 'connection_deleted' and audit['target'] == 'twitch'
        assert audit['actor_hash'] == hashlib.sha256(('google:' + accounts['admin']['subject']).encode()).hexdigest()


def test_suspend_revokes_existing_sessions_cancels_work_and_reenable_requires_login(members_api):
    client, app, auth, accounts, _ = members_api
    repo = app.state.repository
    active = add_job(repo, accounts['member'], idem='active', target='twitch')
    claimed = repo.claim('synthetic-worker', 60)
    queued = add_job(repo, accounts['member'], idem='queued')
    other = add_job(repo, accounts['tenant_admin'], idem='unaffected')
    response = client.put(member_path(accounts) + '/status', headers=accounts['admin']['headers'], json={'enabled': False})
    assert response.status_code == 200 and response.json()['enabled'] is False
    assert repo.get_job(accounts['member']['id'], active['id'])['state'] == 'stopping'
    assert repo.get_job(accounts['member']['id'], queued['id'])['state'] == 'stopped'
    assert repo.get_job(accounts['tenant_admin']['id'], other['id'])['state'] == 'scheduled'
    assert repo.heartbeat(active['id'], claimed['lease_token'])['cancel_requested'] is True
    for method, path, body in [('GET', '/api/me', None), ('GET', '/api/media', None),
        ('POST', '/api/auth/google/refresh', {}), ('POST', '/api/broadcasts', {'media_id': queued['media_id'], 'title': 'new'})]:
        assert client.request(method, path, headers=accounts['member']['headers'], json=body).status_code in (401, 403)
    with pytest.raises(HTTPException) as denied:
        add_job(repo, accounts['member'], idem='after-suspend')
    assert denied.value.status_code == 403
    challenge = auth.challenge('synthetic-disabled-login')
    with pytest.raises(HTTPException) as disabled:
        auth._exchange({'sub': accounts['member']['subject'], 'email': accounts['member']['email'], '_authoritative': True},
                       auth._challenge(challenge['challenge_id'], challenge['challenge_secret']))
    assert disabled.value.status_code == 403
    assert client.put(member_path(accounts) + '/status', headers=accounts['admin']['headers'], json={'enabled': True}).status_code == 200
    assert client.get('/api/me', headers=accounts['member']['headers']).status_code == 401
    with auth._transaction() as connection:
        now = auth._now(connection)
        fresh = auth._mint(connection, accounts['member']['subject'], now, now + 86400)
    assert client.get('/api/me', headers={'Authorization': 'Bearer ' + fresh['token']}).status_code == 200


def test_session_revocation_covers_all_devices_but_not_jobs_or_other_members(members_api):
    client, app, auth, accounts, _ = members_api
    job = add_job(app.state.repository, accounts['member'], idem='keep-work')
    with auth._transaction() as connection:
        now = auth._now(connection)
        second_device = auth._mint(connection, accounts['member']['subject'], now, now + 86400)
    response = client.post(member_path(accounts) + '/revoke-sessions', headers=accounts['admin']['headers'], json={})
    assert response.status_code == 200 and response.json() == {'revoked': True}
    for token in (accounts['member']['token'], second_device['token']):
        assert client.get('/api/me', headers={'Authorization': 'Bearer ' + token}).status_code == 401
    assert app.state.repository.get_job(accounts['member']['id'], job['id'])['state'] == 'scheduled'
    assert client.get('/api/me', headers=accounts['tenant_admin']['headers']).status_code == 200
    assert client.post(member_path(accounts, 'admin') + '/revoke-sessions', headers=accounts['admin']['headers'], json={}).status_code == 200
    assert client.get('/api/me', headers=accounts['admin']['headers']).status_code == 401
    assert client.get('/api/me', headers=accounts['second']['headers']).status_code == 200


def test_self_and_remaining_admin_cannot_be_suspended(members_api):
    client, _, _, accounts, _ = members_api
    assert client.put(member_path(accounts, 'admin') + '/status', headers=accounts['admin']['headers'], json={'enabled': False}).status_code == 409
    assert client.put(member_path(accounts, 'second') + '/status', headers=accounts['admin']['headers'], json={'enabled': False}).status_code == 200
    assert client.put(member_path(accounts, 'admin') + '/status', headers=accounts['admin']['headers'], json={'enabled': False}).status_code == 409
    assert client.put(member_path(accounts, 'admin') + '/status', headers=accounts['second']['headers'], json={'enabled': False}).status_code == 401


def test_transaction_rechecks_admin_and_rolls_back_status_on_audit_failure(members_api, monkeypatch):
    client, app, auth, accounts, _ = members_api
    actor = asyncio.run(auth.authenticate(accounts['admin']['headers']['Authorization']))
    with auth.engine.begin() as connection:
        connection.execute(update(identities).where(identities.c.subject == accounts['admin']['subject']).values(enabled=False))
    with pytest.raises(HTTPException) as denied:
        app.state.members.set_enabled(actor, accounts['member']['id'], False)
    assert denied.value.status_code == 403
    with auth.engine.begin() as connection:
        connection.execute(update(identities).where(identities.c.subject == accounts['admin']['subject']).values(enabled=True))
    job = add_job(app.state.repository, accounts['member'], idem='rollback')
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic-audit-failure')
    monkeypatch.setattr(app.state.members, '_audit', fail)
    with pytest.raises(RuntimeError, match='synthetic-audit-failure'):
        app.state.members.set_enabled(actor, accounts['member']['id'], False)
    assert client.get('/api/me', headers=accounts['member']['headers']).status_code == 200
    assert app.state.repository.get_job(accounts['member']['id'], job['id'])['state'] == 'scheduled'


def test_admin_input_errors_and_application_logs_exclude_pii_and_keys(members_api, caplog):
    client, _, auth, accounts, _ = members_api
    caplog.set_level(logging.INFO, logger='replay.requests')
    save(client, accounts)
    headers = accounts['admin']['headers']
    for query in ('limit=101', 'limit=0', 'offset=-1'):
        assert client.get('/api/admin/members?' + query, headers=headers).status_code == 422
    for payload in ({'enabled': 'false'}, {'enabled': False, 'roles': ['site_admin']}, {'enabled': None}):
        assert client.put(member_path(accounts) + '/status', headers=headers, json=payload).status_code == 422
    assert client.get('/api/admin/members', headers=headers, params={'q': accounts['member']['email']}).status_code == 200
    assert client.delete(member_path(accounts) + '/stream-connections/twitch', headers=headers).status_code == 204
    messages = '\n'.join(record.getMessage() for record in caplog.records if record.name.startswith('replay.'))
    with auth.engine.connect() as connection:
        audit = json.dumps([dict(row) for row in connection.execute(select(member_audit)).mappings()])
    for secret in ['synthetic-stream-key', *(account['email'] for account in accounts.values()),
                   *(account['token'] for account in accounts.values()), *(account['subject'] for account in accounts.values())]:
        assert secret not in messages and secret not in audit


def test_untrusted_stored_site_admin_role_does_not_grant_authority(members_api):
    client, _, auth, accounts, _ = members_api
    with auth.engine.begin() as connection:
        connection.execute(update(identities).where(identities.c.subject == accounts['member']['subject']).values(roles='["operator","site_admin"]'))
    assert client.get('/api/admin/members', headers=accounts['member']['headers']).status_code == 403


def test_site_admin_configuration_is_explicit_validated_and_does_not_allow_signup(monkeypatch):
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', CLIENT_ID)
    monkeypatch.setenv('REPLAY_SITE_ADMIN_EMAILS', ADMIN_EMAILS[0].upper())
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOW_SIGNUPS', '0')
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOWED_EMAILS', '')
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOWED_SUBJECTS', '')
    config = GoogleAuthConfig.from_env()
    assert config.site_admin_emails == (ADMIN_EMAILS[0],) and config.allowed_emails == () and not config.allow_signups
    assert ADMIN_EMAILS[0] not in repr(config)
    monkeypatch.setenv('REPLAY_SITE_ADMIN_EMAILS', 'invalid-admin')
    with pytest.raises(ValueError):
        GoogleAuthConfig.from_env()


def test_member_audit_uses_explicit_migration_and_is_in_backup_schema_summary(tmp_path):
    url = f'sqlite:///{tmp_path}/explicit-migration.db'
    first = ops.migrate(url, development=True)
    assert '009_member_management.sql' in first['versions']
    assert ops.migrate(url, development=True)['versions'] == first['versions']
    repo = Repository(url)
    try:
        with repo.engine.begin() as connection:
            connection.execute(insert(member_audit).values(id='a' * 32, actor_hash='b' * 64,
                member_id='google-' + 'c' * 40, action='sessions_revoked', target='', at=1))
            assert ops.check_schema(connection)
            summary = ops.summary(connection)
            assert summary['table_counts']['replay_member_audit'] == 1
            discard_restored_sessions(connection)
            assert connection.execute(select(func.count()).select_from(member_audit)).scalar_one() == 1
            migration = next(row for row in summary['migrations'] if row['version'] == '009_member_management.sql')
            assert len(migration['sha256']) == 64
    finally:
        repo.close()
