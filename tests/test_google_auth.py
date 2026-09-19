"""Synthetic RSA/JWKS plus temporary real DB; never contacts Google or uses OAuth credentials."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
import httpx
import jwt
import pytest
from sqlalchemy import create_engine, func, inspect, select, text, update

from deploy.commercial import ops
from server.google_auth import (
    GoogleAuthConfig, GoogleAuthenticator, challenges, discard_restored_sessions,
    families, identities, sessions,
)

CLIENT_ID = '1234567890-syntheticwebclient.apps.googleusercontent.com'
EMAIL = 'synthetic.user@gmail.com'
SUBJECT = '123456789012345678901'


@pytest.fixture(scope='module')
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def harness(tmp_path, key):
    engine = create_engine('sqlite:///' + str(tmp_path / 'auth.sqlite3'), connect_args={'check_same_thread': False})
    public = dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), kid='google-test-key', alg='RS256', use='sig')
    requests = []

    def transport(request):
        assert str(request.url) == 'https://www.googleapis.com/oauth2/v3/certs'
        requests.append(request)
        return httpx.Response(200, json={'keys': [public]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    now = [time.time()]
    auth = GoogleAuthenticator(engine, GoogleAuthConfig(CLIENT_ID, allowed_emails=(EMAIL,)),
        mode='test', http_client=client, clock=lambda: now[0])
    auth.migrate()
    yield auth, now, requests
    asyncio.run(auth.close())
    asyncio.run(client.aclose())
    engine.dispose()


def token(key, challenge, **changes):
    now = int(time.time())
    claims = dict(iss='https://accounts.google.com', aud=CLIENT_ID, sub=SUBJECT,
        iat=now, exp=now + 3600, nonce=challenge['nonce'], email=EMAIL, email_verified=True,
        tenant_id='attacker-controlled-tenant', roles=['admin'])
    claims.update(changes)
    return jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'google-test-key'})


async def login(auth, key, client='synthetic-client', **changes):
    challenge = auth.challenge(client)
    return await auth.exchange(token(key, challenge, **changes), challenge['challenge_id'], challenge['challenge_secret'])


def rejected(status, fn, *args):
    with pytest.raises(HTTPException) as failure:
        fn(*args)
    assert failure.value.status_code == status


def test_real_signature_stable_tenant_server_roles_and_hash_only_storage(harness, key):
    auth, _, requests = harness

    async def scenario():
        challenge = auth.challenge('sensitive-client-address')
        credential = token(key, challenge)
        session = await auth.exchange(credential, challenge['challenge_id'], challenge['challenge_secret'])
        principal = await auth.authenticate('Bearer ' + session['token'])
        assert principal.subject == 'google:' + SUBJECT
        assert principal.tenant_id == 'google-' + hashlib.sha256(SUBJECT.encode()).hexdigest()[:40]
        assert principal.roles == ('operator',) and not principal.has_role('admin')
        with pytest.raises(HTTPException) as denied:
            await auth.authenticate('Bearer ' + session['token'], requested_tenant='attacker-controlled-tenant')
        assert denied.value.status_code == 403
        with auth.engine.connect() as connection:
            row = dict(connection.execute(select(sessions)).mappings().one())
            assert row['token_hash'] == hashlib.sha256(session['token'].encode()).hexdigest()
            assert connection.execute(select(func.count()).select_from(challenges)).scalar_one() == 0
        auth.challenge('sensitive-client-address')
        with auth.engine.connect() as connection:
            records = [dict(row) for table in (sessions, challenges, identities)
                for row in connection.execute(select(table)).mappings()]
        persisted = json.dumps(records)
        for secret in (credential, session['token'], challenge['nonce'], challenge['challenge_secret'], 'sensitive-client-address'):
            assert secret not in persisted
        second = await login(auth, key, iss='accounts.google.com')
        assert (await auth.authenticate('Bearer ' + second['token'])).tenant_id == principal.tenant_id
        assert len(requests) == 1  # Fixed Google JWKS cache reused.

    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'iss': 'https://attacker.example'}, {'aud': 'other-client'}, {'aud': [CLIENT_ID, 'other-client']},
    {'azp': 'other-client'}, {'exp': 1}, {'iat': time.time() + 600}, {'iat': True}, {'exp': True},
    {'nonce': 'wrong-nonce'}, {'nonce': []}, {'email_verified': False}, {'email_verified': 'true'},
    {'email': 'invalid'}, {'sub': ''}, {'sub': '../account'}, {'hd': []}, {'exp': time.time() + 90000},
])
def test_google_claim_boundary_rejects_invalid_claims(harness, key, changes):
    auth, _, _ = harness

    async def scenario():
        with pytest.raises(HTTPException) as failure:
            await login(auth, key, **changes)
        assert failure.value.status_code == 401
        with auth.engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(sessions)).scalar_one() == 0

    asyncio.run(scenario())


def test_untrusted_algorithm_token_keys_and_bad_signature_rejected(harness, key):
    auth, _, requests = harness

    async def scenario():
        challenge = auth.challenge('client')
        payload = jwt.decode(token(key, challenge), options={'verify_signature': False})
        candidates = [jwt.encode(payload, 'synthetic-hmac-key-which-is-long-enough', algorithm='HS256', headers={'kid': 'google-test-key'}),
            jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'google-test-key', 'jku': 'https://attacker.example/keys'}),
            jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'google-test-key', 'jwk': {}})]
        for credential in candidates:
            with pytest.raises(HTTPException) as failure:
                await auth.exchange(credential, challenge['challenge_id'], challenge['challenge_secret'])
            assert failure.value.status_code == 401
        assert requests == []
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(HTTPException) as failure:
            await auth.exchange(token(other, challenge), challenge['challenge_id'], challenge['challenge_secret'])
        assert failure.value.status_code == 401

    asyncio.run(scenario())


def test_challenge_expiry_secret_and_atomic_concurrent_replay(harness, key):
    auth, now, requests = harness

    async def scenario():
        challenge = auth.challenge('client')
        credential = token(key, challenge)
        with pytest.raises(HTTPException) as failure:
            await auth.exchange(credential, challenge['challenge_id'], 'a' * 43)
        assert failure.value.status_code == 401 and requests == []
        results = await asyncio.gather(*[auth.exchange(credential, challenge['challenge_id'], challenge['challenge_secret'])
                                        for _ in range(8)], return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        assert all(isinstance(result, dict) or isinstance(result, HTTPException) and result.status_code == 401 for result in results)
        another = auth.challenge('client')
        now[0] += 301
        with pytest.raises(HTTPException) as expired:
            await auth.exchange(token(key, another), another['challenge_id'], another['challenge_secret'])
        assert expired.value.status_code == 401

    asyncio.run(scenario())


def test_default_deny_and_google_authoritative_email_or_subject_policy(harness, key):
    auth, _, _ = harness

    async def scenario():
        auth.config = GoogleAuthConfig(CLIENT_ID)
        challenge = auth.challenge('client')
        credential = token(key, challenge)
        with pytest.raises(HTTPException) as denied:
            await auth.exchange(credential, challenge['challenge_id'], challenge['challenge_secret'])
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException) as replay:
            await auth.exchange(credential, challenge['challenge_id'], challenge['challenge_secret'])
        assert replay.value.status_code == 401
        external = 'synthetic@external.example'
        auth.config = replace(auth.config, allowed_emails=(external,))
        with pytest.raises(HTTPException) as denied_external:
            await login(auth, key, email=external)
        assert denied_external.value.status_code == 403
        hosted = await login(auth, key, email=external, hd='external.example')
        assert (await auth.authenticate('Bearer ' + hosted['token'])).has_role('operator')
        auth.config = GoogleAuthConfig(CLIENT_ID, allowed_subjects=(SUBJECT,))
        explicit_subject = await login(auth, key, email=external)
        assert (await auth.authenticate('Bearer ' + explicit_subject['token'])).has_role('operator')
        auth.config = GoogleAuthConfig(CLIENT_ID, allow_signups=True)
        new_account = await login(auth, key, sub='second-subject', email=external)
        principal = await auth.authenticate('Bearer ' + new_account['token'])
        assert principal.tenant_id != (await auth.authenticate('Bearer ' + explicit_subject['token'])).tenant_id

    asyncio.run(scenario())


def test_active_policy_account_disable_and_database_roles_apply_immediately(harness, key):
    auth, _, _ = harness

    async def scenario():
        session = await login(auth, key)
        authorization = 'Bearer ' + session['token']
        with auth.engine.begin() as connection:
            connection.execute(update(identities).values(roles='["viewer"]'))
        assert (await auth.authenticate(authorization)).roles == ('viewer',)
        with auth.engine.begin() as connection:
            connection.execute(update(identities).values(enabled=False))
        with pytest.raises(HTTPException) as disabled:
            await auth.authenticate(authorization)
        assert disabled.value.status_code == 403
        rejected(403, auth.refresh, authorization)
        with auth.engine.begin() as connection:
            connection.execute(update(identities).values(enabled=True))
        auth.config = GoogleAuthConfig(CLIENT_ID)
        with pytest.raises(HTTPException) as removed:
            await auth.authenticate(authorization)
        assert removed.value.status_code == 403
        auth.logout(authorization)  # Disabled/removed accounts can still discard their token.
        with auth.engine.connect() as connection:
            assert connection.execute(select(families.c.revoked)).scalar_one() is True

    asyncio.run(scenario())


def test_rotation_has_single_winner_logout_and_absolute_expiry(harness, key):
    auth, now, _ = harness

    async def scenario():
        session = await login(auth, key)
        original = 'Bearer ' + session['token']
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(auth.refresh, original) for _ in range(6)]
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except HTTPException as error:
                    assert error.status_code == 401
        assert len(results) == 1 and results[0]['token'] != session['token']
        with pytest.raises(HTTPException) as old:
            await auth.authenticate(original)
        assert old.value.status_code == 401
        rotated = results[0]
        absolute = session['absolute_expires_at']
        for _ in range(24):
            now[0] = min(now[0] + 3500, absolute - 1)
            rotated = auth.refresh('Bearer ' + rotated['token'])
            assert rotated['absolute_expires_at'] == absolute
            assert rotated['expires_at'] <= absolute
        now[0] = absolute
        rejected(401, auth.refresh, 'Bearer ' + rotated['token'])
        now[0] = time.time()
        session = await login(auth, key)
        auth.logout('Bearer ' + session['token'])
        rejected(401, auth.refresh, 'Bearer ' + session['token'])
        auth.logout('Bearer ' + session['token'])  # Repeated logout is harmless.

    asyncio.run(scenario())


def test_challenge_client_budget_session_cap_and_expired_cleanup(harness, key):
    auth, now, _ = harness
    for _ in range(20):
        auth.challenge('blocked-client')
    rejected(429, auth.challenge, 'blocked-client')
    assert auth.challenge('different-client')

    async def scenario():
        for _ in range(20):
            await login(auth, key, client='account-client')
        with pytest.raises(HTTPException) as capped:
            await login(auth, key, client='account-client')
        assert capped.value.status_code == 429
        now[0] += 86401
        purged = auth.purge_expired()
        assert purged['google_sessions'] == 20 and purged['google_challenges'] == 21
        with auth.engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(identities)).scalar_one() == 1

    asyncio.run(scenario())


def test_retired_token_logout_revokes_only_its_family_and_wins_refresh_race(harness, key):
    auth, _, _ = harness

    async def scenario():
        first = await login(auth, key)
        other_device = await login(auth, key)
        current = auth.refresh('Bearer ' + first['token'])
        # A logout dispatched before receiving the refresh response has the old
        # bearer. It must still revoke the replacement token on that device.
        auth.logout('Bearer ' + first['token'])
        with pytest.raises(HTTPException) as revoked:
            await auth.authenticate('Bearer ' + current['token'])
        assert revoked.value.status_code == 401
        assert (await auth.authenticate('Bearer ' + other_device['token'])).has_role('operator')
        for _ in range(6):
            session = await login(auth, key)
            authorization = 'Bearer ' + session['token']
            with ThreadPoolExecutor(max_workers=2) as pool:
                rotate = pool.submit(auth.refresh, authorization)
                logout = pool.submit(auth.logout, authorization)
                logout.result()
                try:
                    fresh = rotate.result()
                except HTTPException as failure:
                    assert failure.status_code == 401
                else:
                    with pytest.raises(HTTPException) as race_revoked:
                        await auth.authenticate('Bearer ' + fresh['token'])
                    assert race_revoked.value.status_code == 401

    asyncio.run(scenario())


def test_restore_discards_tokens_and_nonces_but_preserves_identity(harness, key):
    auth, _, _ = harness

    async def scenario():
        session = await login(auth, key)
        before = await auth.authenticate('Bearer ' + session['token'])
        auth.challenge('client')
        with auth.engine.begin() as connection:
            assert discard_restored_sessions(connection) == {'google_sessions_discarded': 1, 'google_challenges_discarded': 1}
        with pytest.raises(HTTPException) as restored_token:
            await auth.authenticate('Bearer ' + session['token'])
        assert restored_token.value.status_code == 401
        fresh = await login(auth, key)
        assert (await auth.authenticate('Bearer ' + fresh['token'])).tenant_id == before.tenant_id

    asyncio.run(scenario())


def test_explicit_migration_schema_and_fail_closed_database(tmp_path, key):
    url = 'sqlite:///' + str(tmp_path / 'migration.sqlite3')
    engine = create_engine(url)
    config = GoogleAuthConfig(CLIENT_ID)
    with pytest.raises(ValueError):
        GoogleAuthenticator(engine, config)
    auth = GoogleAuthenticator(engine, config, mode='test')
    rejected(503, auth.challenge, 'client')
    assert inspect(engine).get_table_names() == []  # No implicit migration on requests.
    result = ops.migrate(url, development=True)
    assert '005_google_auth.sql' in result['versions']
    with engine.connect() as connection:
        assert ops.check_schema(connection)
        assert 'replay_google_sessions' in ops.summary(connection)['table_counts']
    asyncio.run(auth.close())
    engine.dispose()


@pytest.mark.parametrize('config', [
    {'client_id': ''}, {'client_id': 'secret-looking-malformed-client-id'},
    {'client_id': CLIENT_ID, 'allow_signups': '1'},
    {'client_id': CLIENT_ID, 'allowed_emails': ('not-an-email',)},
    {'client_id': CLIENT_ID, 'allowed_subjects': ('../bad-subject',)},
])
def test_invalid_configuration_is_rejected_without_echoing_values(config):
    with pytest.raises(ValueError) as invalid:
        GoogleAuthConfig(**config)
    assert 'secret-looking' not in str(invalid.value)


def test_env_signup_must_be_explicit_and_allowlists_do_not_appear_in_repr(monkeypatch):
    for name in ('REPLAY_GOOGLE_ALLOWED_EMAILS', 'REPLAY_GOOGLE_ALLOWED_SUBJECTS', 'REPLAY_GOOGLE_ALLOW_SIGNUPS'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('REPLAY_GOOGLE_CLIENT_ID', CLIENT_ID)
    assert not GoogleAuthConfig.from_env().allow_signups
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOWED_EMAILS', EMAIL.upper())
    config = GoogleAuthConfig.from_env()
    assert config.allowed_emails == (EMAIL,)
    assert EMAIL not in repr(config)
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOW_SIGNUPS', '1')
    public = GoogleAuthConfig.from_env()
    assert public.allow_signups is True and public.allowed_emails == (EMAIL,)
    assert public.allowed_subjects == ()
    monkeypatch.setenv('REPLAY_GOOGLE_ALLOW_SIGNUPS', 'yes-secret')
    with pytest.raises(ValueError) as invalid:
        GoogleAuthConfig.from_env()
    assert 'yes-secret' not in str(invalid.value)
