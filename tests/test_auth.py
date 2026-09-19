import asyncio
import json
import time

from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException
import httpx
import jwt
import pytest

from server.auth import AuthConfig, JWTAuthenticator

ISSUER = 'https://issuer.example/'
AUDIENCE = 'replay-api'


@pytest.fixture(scope='module')
def signing_keys():
    return {'rsa': rsa.generate_private_key(public_exponent=65537, key_size=2048),
            'ec': ec.generate_private_key(ec.SECP256R1())}


def claims(**changes):
    now = int(time.time())
    return dict(iss=ISSUER, aud=AUDIENCE, sub='customer-1', iat=now, exp=now + 300,
                tenant_id='tenant-a', roles=['operator'], **changes)


def jwk(key, kid='key-1', algorithm='RS256'):
    implementation = jwt.algorithms.RSAAlgorithm if algorithm == 'RS256' else jwt.algorithms.ECAlgorithm
    return dict(json.loads(implementation.to_jwk(key.public_key())), kid=kid, alg=algorithm, use='sig')


def config(**kwargs):
    return AuthConfig(issuer=ISSUER, audience=AUDIENCE, jwks_url='https://issuer.example/keys', leeway=0, **kwargs)


@pytest.mark.parametrize('kind,algorithm', [('rsa', 'RS256'), ('ec', 'ES256')])
def test_real_asymmetric_token_and_tenant_authorization(signing_keys, kind, algorithm):
    async def scenario():
        key = signing_keys[kind]
        seen = []
        def transport(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={'keys': [jwk(key, algorithm=algorithm)]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            authenticator = JWTAuthenticator(config(), client)
            token = jwt.encode(claims(), key, algorithm=algorithm, headers={'kid': 'key-1'})
            principal = await authenticator.authenticate('Bearer ' + token)
            assert (principal.subject, principal.tenant_id, principal.roles) == ('customer-1', 'tenant-a', ('operator',))
            assert principal.has_role('operator') and not principal.has_role('admin')
            with pytest.raises(HTTPException) as rejected:
                await authenticator.authenticate('Bearer ' + token, requested_tenant='tenant-b')
            assert rejected.value.status_code == 403
            assert seen == ['https://issuer.example/keys']
    asyncio.run(scenario())


@pytest.mark.parametrize('changes', [
    {'iss': 'https://attacker.example/'}, {'aud': 'different-api'}, {'exp': 1},
    {'tenant_id': ['tenant-a']}, {'tenant_id': '../private'}, {'roles': 'admin'},
    {'token_use': 'id'}, {'iat': True}, {'sub': ''},
])
def test_wrong_issuer_audience_expiry_and_claim_shapes_rejected(signing_keys, changes):
    async def scenario():
        key = signing_keys['rsa']
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'keys': [jwk(key)]}))) as client:
            auth = JWTAuthenticator(config(), client)
            payload = claims()
            payload.update(changes)
            token = jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'key-1'})
            with pytest.raises(HTTPException) as rejected:
                await auth.authenticate('Bearer ' + token)
            assert rejected.value.status_code == 401
    asyncio.run(scenario())


def test_membership_claim_required_and_no_role_privilege_default(signing_keys):
    async def scenario():
        key = signing_keys['rsa']
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'keys': [jwk(key)]}))) as client:
            auth = JWTAuthenticator(config(membership_claim='memberships'), client)
            payload = claims()
            payload.pop('roles')
            payload['memberships'] = ['tenant-b']
            token = jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'key-1'})
            with pytest.raises(HTTPException) as denied:
                await auth.authenticate('Bearer ' + token)
            assert denied.value.status_code == 403
            payload['memberships'] = ['tenant-a']
            token = jwt.encode(payload, key, algorithm='RS256', headers={'kid': 'key-1'})
            assert (await auth.authenticate('Bearer ' + token)).roles == ()
    asyncio.run(scenario())


def test_algorithm_confusion_and_token_supplied_keys_make_no_http_requests(signing_keys):
    async def scenario():
        def forbidden(request):
            raise AssertionError('Untrusted token must not select a key endpoint')
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            auth = JWTAuthenticator(config(), client)
            tokens = [jwt.encode(claims(), 'synthetic-shared-secret' * 3, algorithm='HS256', headers={'kid': 'key-1'}),
                      jwt.encode(claims(), '', algorithm='none', headers={'kid': 'key-1'}),
                      jwt.encode(claims(), signing_keys['rsa'], algorithm='RS256', headers={'kid': 'key-1', 'jku': 'https://attacker.example/keys'})]
            for token in tokens:
                with pytest.raises(HTTPException) as rejected:
                    await auth.authenticate('Bearer ' + token)
                assert rejected.value.status_code == 401
    asyncio.run(scenario())


def test_unknown_key_refresh_is_bounded_and_signing_rotation_works(signing_keys):
    async def scenario():
        rsa_key, ec_key = signing_keys['rsa'], signing_keys['ec']
        requests = []
        def transport(request):
            requests.append(request)
            keys = [jwk(rsa_key)] if len(requests) == 1 else [jwk(rsa_key), jwk(ec_key, 'rotated-key', 'ES256')]
            return httpx.Response(200, json={'keys': keys})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            auth = JWTAuthenticator(config(), client)
            await auth.authenticate('Bearer ' + jwt.encode(claims(), rsa_key, algorithm='RS256', headers={'kid': 'key-1'}))
            new_token = jwt.encode(claims(), ec_key, algorithm='ES256', headers={'kid': 'rotated-key'})
            for _ in range(5):
                with pytest.raises(HTTPException):
                    await auth.authenticate('Bearer ' + new_token)
            assert len(requests) == 1
            auth._refreshed -= 6
            assert (await auth.authenticate('Bearer ' + new_token)).tenant_id == 'tenant-a'
            assert len(requests) == 2
    asyncio.run(scenario())


def test_development_auth_is_explicit_and_forbidden_in_production():
    with pytest.raises(ValueError):
        AuthConfig()
    with pytest.raises(ValueError):
        config(dev_token='synthetic-development-token')
    with pytest.raises(ValueError):
        AuthConfig(mode='development')
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError('No network')))) as client:
            auth = JWTAuthenticator(AuthConfig(mode='development', dev_token='synthetic-development-token'), client)
            principal = await auth.authenticate('Bearer synthetic-development-token')
            assert principal.tenant_id == 'development' and principal.has_role('operator')
    asyncio.run(scenario())
