"""Real ASGI routes/DB and synthetic Google RSA/JWKS; no live Google calls."""
import asyncio
from dataclasses import replace
import json
import logging
import time

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest
from sqlalchemy import func, select, update

from server.auth import AuthConfig, JWTAuthenticator
from server.google_auth import GoogleAuthConfig, GoogleAuthenticator, challenges, families, identities, sessions
from server.production_app import create_production_app
from server.repository import Repository
from server.secrets import LocalKeyringProvider
from server.settings import Settings
from server.storage import LocalStorage

CLIENT_ID = '1234567890-syntheticapi.apps.googleusercontent.com'
EMAIL = 'synthetic.api@gmail.com'
ORIGIN = 'http://ui.test'


@pytest.fixture(scope='module')
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def components(tmp_path):
    cfg = Settings(mode='development', database_url='sqlite:///' + str(tmp_path / 'api.db'),
        public_url='http://testserver', origins=(ORIGIN,), control_token='c' * 32,
        callback_key='s' * 32, local_root=str(tmp_path), version='google-api-test')
    repo = Repository(cfg.database_url, create_schema=True)
    storage = LocalStorage(tmp_path / 'objects', signing_key=cfg.callback_key,
        base_url=cfg.public_url, allow_development=True)
    keys = LocalKeyringProvider({'test': Fernet.generate_key().decode()}, 'test', mode='development')
    yield cfg, repo, storage, keys
    repo.close()


@pytest.fixture
def google_api(components, signing_key):
    cfg, repo, storage, keys = components
    jwk = dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key())), kid='google-api-key', alg='RS256', use='sig')
    requests = []

    def transport(request):
        assert str(request.url) == 'https://www.googleapis.com/oauth2/v3/certs'
        requests.append(request)
        return httpx.Response(200, json={'keys': [jwk]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    auth = GoogleAuthenticator(repo.engine, GoogleAuthConfig(CLIENT_ID, allowed_emails=(EMAIL,)), mode='test', http_client=http)
    auth.migrate()
    app = create_production_app(cfg, repository=repo, storage=storage, keys=keys, authenticator=auth)
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    with TestClient(app, headers={'Origin': ORIGIN, 'X-Replay-Client': '1'}) as client:
        yield client, auth, requests
    asyncio.run(http.aclose())


def exchange(client, key, *, subject='google-api-subject'):
    challenge = client.post('/api/auth/google/challenge', json={})
    assert challenge.status_code == 200, challenge.text
    nonce = challenge.json()
    now = int(time.time())
    credential = jwt.encode(dict(iss='https://accounts.google.com', aud=CLIENT_ID, sub=subject,
        email=EMAIL, email_verified=True, iat=now, exp=now + 3600, nonce=nonce['nonce'],
        tenant_id='attacker-tenant', roles=['admin']), key, algorithm='RS256', headers={'kid': 'google-api-key'})
    response = client.post('/api/auth/google/exchange', json=dict(credential=credential,
        challenge_id=nonce['challenge_id'], challenge_secret=nonce['challenge_secret']))
    assert response.status_code == 200, response.text
    return response, credential, nonce


@pytest.mark.parametrize('path', ['/api/auth/google/challenge', '/api/auth/google/exchange', '/api/auth/google/refresh', '/api/logout'])
@pytest.mark.parametrize('headers', [{'Origin': ''}, {'Origin': 'http://ui.test.attacker.example'}, {'X-Replay-Client': ''}, {'X-Replay-Client': '2'}])
def test_signin_and_logout_require_exact_origin_and_client_header(google_api, path, headers):
    client, auth, requests = google_api
    payload = {'credential': 'synthetic-placeholder', 'challenge_id': 'a' * 32, 'challenge_secret': 'b' * 43}
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 403
    assert requests == []
    with auth.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(challenges)).scalar_one() == 0


@pytest.mark.parametrize('path', ['/api/auth/google/challenge', '/api/auth/google/exchange', '/api/auth/google/refresh', '/api/logout'])
def test_login_rejects_non_json_and_oversized_body_without_echo(google_api, path):
    client, _, requests = google_api
    secret = 'synthetic-credential-never-echo'
    response = client.post(path, content=secret, headers={'Content-Type': 'text/plain'})
    assert response.status_code in (415, 422)
    assert secret not in response.text
    oversized = client.post(path, content=b'{"credential":"' + b'x' * 17000 + b'"}', headers={'Content-Type': 'application/json'})
    assert oversized.status_code == 413
    assert requests == []


def test_real_exchange_me_refresh_old_logout_and_other_device_survives(google_api, signing_key):
    client, _, requests = google_api
    first, _, _ = exchange(client, signing_key)
    original = first.json()['token']
    original_headers = {'Authorization': 'Bearer ' + original}
    me = client.get('/api/me', headers=original_headers)
    assert me.status_code == 200
    assert me.json()['tenant_id'].startswith('google-') and me.json()['tenant_id'] != 'attacker-tenant'
    assert me.json()['roles'] == ['operator']
    assert client.get('/api/me', headers={**original_headers, 'X-Replay-Tenant': 'attacker-tenant'}).status_code == 403
    other, _, _ = exchange(client, signing_key)
    other_headers = {'Authorization': 'Bearer ' + other.json()['token']}
    assert client.get('/api/me', headers=other_headers).json()['tenant_id'] == me.json()['tenant_id']
    refreshed = client.post('/api/auth/google/refresh', json={}, headers=original_headers)
    assert refreshed.status_code == 200
    new_token = refreshed.json()['token']
    assert new_token != original
    assert refreshed.json()['absolute_expires_at'] == first.json()['absolute_expires_at']
    current_headers = {'Authorization': 'Bearer ' + new_token}
    assert client.get('/api/me', headers=original_headers).status_code == 401
    assert client.get('/api/me', headers=current_headers).status_code == 200
    assert client.post('/api/auth/google/refresh', json={}, headers=original_headers).status_code == 401
    # Route must not authenticate the retired token before family logout.
    assert client.post('/api/logout', json={}, headers=original_headers).status_code == 200
    assert client.get('/api/me', headers=current_headers).status_code == 401
    assert client.get('/api/me', headers=other_headers).status_code == 200
    assert client.post('/api/logout', json={}, headers=original_headers).status_code == 200
    assert len(requests) == 1


def test_auth_responses_are_uncacheable_and_logs_never_contain_credentials(google_api, signing_key, caplog):
    client, _, _ = google_api
    caplog.set_level(logging.INFO, logger='replay.requests')
    session, credential, challenge = exchange(client, signing_key)
    headers = {'Authorization': 'Bearer ' + session.json()['token']}
    refreshed = client.post('/api/auth/google/refresh', json={}, headers=headers)
    assert refreshed.status_code == 200
    rejected = client.post('/api/auth/google/exchange', json={'credential': credential, 'challenge_id': 'bad', 'challenge_secret': 'bad'})
    assert rejected.status_code == 422
    for response in (session, refreshed, rejected):
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['x-request-id']
        assert response.headers['access-control-allow-origin'] == ORIGIN
    messages = '\n'.join(record.getMessage() for record in caplog.records if record.name.startswith('replay.'))
    assert messages
    for value in (credential, session.json()['token'], refreshed.json()['token'], challenge['challenge_secret'], challenge['nonce'], EMAIL):
        assert value not in messages and value not in rejected.text


def test_control_maintenance_expires_google_transients_and_preserves_accounts(google_api, signing_key):
    client, auth, _ = google_api
    exchange(client, signing_key)
    assert client.post('/api/auth/google/challenge', json={}).status_code == 200
    with auth.engine.begin() as connection:
        connection.execute(update(challenges).values(expires_at=1))
        connection.execute(update(sessions).values(absolute_expires_at=1))
        connection.execute(update(families).values(absolute_expires_at=1))
    assert client.post('/internal/maintenance', json={}).status_code == 401
    response = client.post('/internal/maintenance', json={}, headers={'Authorization': 'Bearer ' + 'c' * 32})
    assert response.status_code == 200
    with auth.engine.connect() as connection:
        for table in (sessions, families, challenges):
            assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(identities)).scalar_one() == 1


def test_provider_disabled_routes_and_existing_oidc_auth_remain_separate(components):
    cfg, repo, storage, keys = components
    auth = JWTAuthenticator(AuthConfig(mode='development', dev_token='synthetic-development-auth'))
    app = create_production_app(cfg, repository=repo, storage=storage, keys=keys, authenticator=auth)
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    with TestClient(app, headers={'Origin': ORIGIN, 'X-Replay-Client': '1'}) as client:
        for path in ('challenge', 'exchange', 'refresh'):
            response = client.post('/api/auth/google/' + path, json={'credential': 'x', 'challenge_id': 'a' * 32, 'challenge_secret': 'b' * 43})
            assert response.status_code == 404
        assert client.get('/api/me', headers={'Authorization': 'Bearer synthetic-development-auth'}).status_code == 200


def test_factory_google_selection_default_deny_and_invalid_config(components, monkeypatch):
    cfg, repo, storage, keys = components
    monkeypatch.setenv('REPLAY_LOGIN_PROVIDER', 'google')
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', CLIENT_ID)
    monkeypatch.delenv('REPLAY_GOOGLE_ALLOWED_EMAILS', raising=False)
    monkeypatch.delenv('REPLAY_GOOGLE_ALLOWED_SUBJECTS', raising=False)
    monkeypatch.delenv('REPLAY_GOOGLE_ALLOW_SIGNUPS', raising=False)
    from server.google_auth import metadata
    metadata.create_all(repo.engine)
    app = create_production_app(cfg, repository=repo, storage=storage, keys=keys)
    with TestClient(app, headers={'Origin': ORIGIN, 'X-Replay-Client': '1'}) as client:
        assert client.post('/api/auth/google/challenge', json={}).status_code == 200
    assert GoogleAuthConfig.from_env().allowed_emails == () and not GoogleAuthConfig.from_env().allow_signups
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', 'invalid-client-secret-sentinel')
    with pytest.raises(ValueError) as invalid:
        create_production_app(cfg, repository=repo, storage=storage, keys=keys)
    assert 'invalid-client-secret-sentinel' not in str(invalid.value)
    monkeypatch.setenv('REPLAY_LOGIN_PROVIDER', 'unknown')
    with pytest.raises(ValueError):
        create_production_app(cfg, repository=repo, storage=storage, keys=keys)


def signup_response(client, key, *, subject, email, verified=True, hosted=None):
    """Exercise Google signature/nonce validation with synthetic identities only."""
    challenge = client.post('/api/auth/google/challenge', json={}).json()
    now = int(time.time())
    claims = dict(iss='https://accounts.google.com', aud=CLIENT_ID, sub=subject,
        email=email, email_verified=verified, iat=now, exp=now + 3600, nonce=challenge['nonce'],
        tenant_id='attacker-tenant', roles=['admin', 'site_admin'])
    if hosted is not None:
        claims['hd'] = hosted
    credential = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'google-api-key'})
    return client.post('/api/auth/google/exchange', json={'credential': credential,
        'challenge_id': challenge['challenge_id'], 'challenge_secret': challenge['challenge_secret']})


@pytest.mark.parametrize('email,hosted', [
    ('new.synthetic.member@gmail.com', None),
    ('new.synthetic.member@workspace.example', 'workspace.example'),
    ('new.synthetic.member@external.example', None),
])
def test_public_signup_ignores_existing_email_allowlist_and_isolates_member_data(google_api, signing_key, email, hosted):
    client, auth, _ = google_api
    auth.config = replace(auth.config, allow_signups=True, site_admin_emails=())
    assert auth.config.allowed_emails == (EMAIL,)
    app = client.app
    app.state.stream_connections.migrate()
    owner, _, _ = exchange(client, signing_key)
    owner_headers = {'Authorization': 'Bearer ' + owner.json()['token']}
    first = client.get('/api/me', headers=owner_headers).json()
    response = signup_response(client, signing_key, subject='new-synthetic-public-member', email=email, hosted=hosted)
    assert response.status_code == 200
    headers = {'Authorization': 'Bearer ' + response.json()['token']}
    second_response = client.get('/api/me', headers=headers)
    assert second_response.status_code == 200
    second = second_response.json()
    assert first['tenant_id'] != second['tenant_id'] != 'attacker-tenant'
    assert second['profile']['email'] == email
    for me, own_headers in ((first, owner_headers), (second, headers)):
        assert me['roles'] == ['operator'] and me['permissions']['manage_members'] is False
        assert client.get('/api/admin/members', headers=own_headers).status_code == 403
    assert client.get('/api/me', headers={**headers, 'X-Replay-Tenant': first['tenant_id']}).status_code == 403
    save = client.put('/api/stream-connections/twitch', headers=owner_headers,
        json={'server_url': '', 'stream_key': 'synthetic-public-signup-owner-key'})
    assert save.status_code == 200
    assert client.get('/api/stream-connections', headers=headers).json() == []
    assert client.post('/api/stream-connections/twitch/use', headers=headers).status_code == 404
    assert client.delete('/api/stream-connections/twitch', headers=headers).status_code == 204
    assert client.post('/api/stream-connections/twitch/use', headers=owner_headers).json()['stream_key'] == 'synthetic-public-signup-owner-key'
    media = app.state.repository.add_media(first['tenant_id'], name='Synthetic private member media.mp4',
        object_key='synthetic-owner-media.mp4', bytes=100, duration=3, width=320, height=180, fps=30)
    assert client.get('/api/media', headers=headers).json() == []
    assert client.get('/api/media', headers=owner_headers).json()[0]['id'] == media['id']
    forbidden = client.post('/api/broadcasts', headers={**headers, 'Idempotency-Key': 'synthetic-cross-member'},
        json={'media_id': media['id'], 'title': 'Must not use another member video', 'target': 'local'})
    assert forbidden.status_code == 404


def test_public_signup_still_rejects_unverified_and_disabled_google_accounts(google_api, signing_key):
    client, auth, _ = google_api
    auth.config = replace(auth.config, allow_signups=True, site_admin_emails=())
    rejected = signup_response(client, signing_key, subject='synthetic-unverified-public-member',
        email='synthetic.unverified@gmail.com', verified=False)
    assert rejected.status_code == 401
    subject, email = 'synthetic-disabled-public-member', 'synthetic.disabled@gmail.com'
    accepted = signup_response(client, signing_key, subject=subject, email=email)
    assert accepted.status_code == 200
    headers = {'Authorization': 'Bearer ' + accepted.json()['token']}
    with auth.engine.begin() as connection:
        connection.execute(update(identities).where(identities.c.subject == subject).values(enabled=False))
    assert client.get('/api/me', headers=headers).status_code == 403
    assert client.post('/api/auth/google/refresh', headers=headers, json={}).status_code == 403
    assert signup_response(client, signing_key, subject=subject, email=email).status_code == 403
    with auth.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(identities)).scalar_one() == 1
        assert connection.execute(select(identities.c.enabled).where(identities.c.subject == subject)).scalar_one() is False


def test_public_signup_does_not_inherit_an_explicit_site_admin_assignment(google_api, signing_key):
    client, auth, _ = google_api
    auth.config = replace(auth.config, allow_signups=True, site_admin_emails=(EMAIL,))
    admin, _, _ = exchange(client, signing_key)
    admin_headers = {'Authorization': 'Bearer ' + admin.json()['token']}
    assert client.get('/api/me', headers=admin_headers).json()['permissions']['manage_members'] is True
    newcomer = signup_response(client, signing_key, subject='synthetic-no-inherited-authority', email='synthetic.newcomer@gmail.com')
    assert newcomer.status_code == 200
    headers = {'Authorization': 'Bearer ' + newcomer.json()['token']}
    me = client.get('/api/me', headers=headers).json()
    assert me['roles'] == ['operator'] and me['permissions']['manage_members'] is False
    assert client.get('/api/admin/members', headers=headers).status_code == 403
