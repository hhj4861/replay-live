"""Google auth races using independent services against an explicit disposable PG DB.

No Google calls: JWKS uses MockTransport and tokens are signed by a fresh local
RSA key. Each test applies production SQL in a random schema, then drops it.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
import uuid

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
import httpx
import jwt
import pytest
from sqlalchemy import create_engine, func, make_url, select, text

from server.google_auth import (
    GoogleAuthConfig, GoogleAuthenticator, challenges, discard_restored_sessions,
    families, identities, sessions,
)

URL = os.environ.get('REPLAY_TEST_POSTGRES_URL')
pytestmark = pytest.mark.skipif(not URL, reason='Explicit disposable PostgreSQL URL is required')
CLIENT_ID = '1234567890-syntheticpostgres.apps.googleusercontent.com'


@pytest.fixture
def google_pg():
    parsed = make_url(URL)
    if parsed.get_backend_name() != 'postgresql' or not (parsed.database or '').startswith('replay_test'):
        pytest.fail('Google PostgreSQL tests require a disposable replay_test* database')
    schema = 'google_test_' + uuid.uuid4().hex
    owner = create_engine(parsed)
    engines, clients, services = [], [], []
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), kid='pg-google-key', alg='RS256', use='sig')

    def transport(request):
        assert str(request.url) == 'https://www.googleapis.com/oauth2/v3/certs'
        return httpx.Response(200, json={'keys': [jwk]})

    with owner.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        scoped = parsed.update_query_dict({'options': f'-csearch_path={schema} -clock_timeout=10000 -cstatement_timeout=15000'})
        for _ in range(2):
            engine = create_engine(scoped)
            engines.append(engine)
            client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
            clients.append(client)
            services.append(GoogleAuthenticator(engine, GoogleAuthConfig(CLIENT_ID,
                allowed_emails=('synthetic.pg@gmail.com',)), http_client=client))
        with engines[0].begin() as connection:
            connection.exec_driver_sql(Path('migrations/005_google_auth.sql').read_text())
        yield services, key
    finally:
        for client in clients:
            asyncio.run(client.aclose())
        for engine in engines:
            engine.dispose()
        with owner.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        owner.dispose()


def credential(key, challenge):
    now = int(time.time())
    return jwt.encode(dict(iss='https://accounts.google.com', aud=CLIENT_ID, sub='google-pg-subject',
        iat=now, exp=now + 3600, email='synthetic.pg@gmail.com', email_verified=True, nonce=challenge['nonce']),
        key, algorithm='RS256', headers={'kid': 'pg-google-key'})


async def login(service, key):
    challenge = service.challenge('synthetic-client')
    return await service.exchange(credential(key, challenge), challenge['challenge_id'], challenge['challenge_secret'])


def test_postgres_challenge_and_refresh_each_have_one_winner_across_services(google_pg):
    services, key = google_pg
    first, second = services

    async def scenario():
        challenge = first.challenge('synthetic-client')
        token = credential(key, challenge)
        results = await asyncio.gather(*(service.exchange(token, challenge['challenge_id'], challenge['challenge_secret'])
            for service in services), return_exceptions=True)
        successful = [result for result in results if isinstance(result, dict)]
        rejected = [result for result in results if isinstance(result, HTTPException)]
        assert len(successful) == len(rejected) == 1 and rejected[0].status_code == 401
        authorization = 'Bearer ' + successful[0]['token']
        barrier = threading.Barrier(2)

        def rotate(service):
            barrier.wait(timeout=5)
            try:
                return service.refresh(authorization)
            except HTTPException as error:
                return error.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(rotate, services))
        assert sum(isinstance(result, dict) for result in results) == 1 and results.count(401) == 1
        current = next(result for result in results if isinstance(result, dict))
        assert (await second.authenticate('Bearer ' + current['token'])).has_role('operator')
        with pytest.raises(HTTPException) as old:
            await first.authenticate(authorization)
        assert old.value.status_code == 401
        with first.engine.connect() as connection:
            assert connection.execute(select(func.count()).select_from(sessions).where(sessions.c.retired.is_(False))).scalar_one() == 1

    asyncio.run(scenario())


def test_postgres_logout_rotation_race_revokes_family_and_preserves_other_device(google_pg):
    services, key = google_pg
    first, second = services

    async def scenario():
        unrelated = await login(first, key)
        for _ in range(12):
            session = await login(first, key)
            authorization = 'Bearer ' + session['token']
            barrier = threading.Barrier(2)

            def rotate():
                barrier.wait(timeout=5)
                try:
                    return first.refresh(authorization)
                except HTTPException as error:
                    assert error.status_code == 401
                    return None

            def logout():
                barrier.wait(timeout=5)
                second.logout(authorization)

            with ThreadPoolExecutor(max_workers=2) as pool:
                rotating, logging_out = pool.submit(rotate), pool.submit(logout)
                rotated = rotating.result()
                logging_out.result()
            candidates = [session['token']] + ([rotated['token']] if rotated else [])
            for token in candidates:
                with pytest.raises(HTTPException) as revoked:
                    await second.authenticate('Bearer ' + token)
                assert revoked.value.status_code == 401
        assert (await second.authenticate('Bearer ' + unrelated['token'])).has_role('operator')

    asyncio.run(scenario())


def test_postgres_shared_client_challenge_cap_and_restore_cleanup(google_pg):
    services, key = google_pg
    first, second = services

    def issue(index):
        try:
            return services[index % 2].challenge('shared-client')
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(issue, range(24)))
    assert sum(isinstance(result, dict) for result in results) == 20
    assert results.count(429) == 4

    async def scenario():
        session = await login(first, key)
        before = await first.authenticate('Bearer ' + session['token'])
        rotated = second.refresh('Bearer ' + session['token'])
        with first.engine.begin() as connection:
            discarded = discard_restored_sessions(connection)
            assert discarded == {'google_sessions_discarded': 2, 'google_challenges_discarded': 20}
            assert connection.execute(select(func.count()).select_from(identities)).scalar_one() == 1
            for table in (sessions, families, challenges):
                assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 0
        with pytest.raises(HTTPException) as restored:
            await second.authenticate('Bearer ' + rotated['token'])
        assert restored.value.status_code == 401
        fresh = await login(second, key)
        assert (await first.authenticate('Bearer ' + fresh['token'])).tenant_id == before.tenant_id

    asyncio.run(scenario())
