"""Synthetic, offline checks for the explicitly selected production AES provider."""
import base64
import json
from collections.abc import Mapping
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from server.secrets import EnvironmentAESGCMKeyProvider, LocalKeyringProvider, SecretError


KEYS = {'v1': b'\x11' * 32, 'v2': b'\x22' * 32}
CONTEXT = {'tenant_id': 'synthetic-tenant', 'subject_hash': 'a' * 64,
           'target': 'twitch', 'purpose': 'stream-connection-v1'}
PLAINTEXT = 'synthetic-stream-key / 비공개 연결'


def key_env(active='v1'):
    return {'REPLAY_SECRET_ACTIVE_KEY': active, 'REPLAY_SECRET_KEY_IDS': 'v1,v2',
            **{'REPLAY_SECRET_KEY_' + name.upper(): base64.b64encode(key).decode('ascii')
               for name, key in KEYS.items()}}


def test_environment_provider_round_trip_aad_and_no_output(capsys, caplog):
    provider = EnvironmentAESGCMKeyProvider.from_env(key_env())
    ciphertext = provider.encrypt(PLAINTEXT, CONTEXT)
    envelope = json.loads(ciphertext)
    assert envelope['v'] == 1 and envelope['provider'] == 'env-aesgcm' and envelope['key_id'] == 'v1'
    assert provider.decrypt(ciphertext, dict(reversed(list(CONTEXT.items())))) == PLAINTEXT
    # Check the complete AAD independently of the provider's encoder/decoder.
    aad = json.dumps({'v': 1, 'provider': 'env-aesgcm', 'key_id': 'v1', 'context': CONTEXT},
                     sort_keys=True, separators=(',', ':')).encode('utf-8')
    assert AESGCM(KEYS['v1']).decrypt(base64.b64decode(envelope['nonce']),
        base64.b64decode(envelope['ciphertext']), aad).decode() == PLAINTEXT
    assert len(base64.b64decode(envelope['nonce'])) == 12
    assert PLAINTEXT not in ciphertext and CONTEXT['tenant_id'] not in ciphertext
    for value in [PLAINTEXT, *key_env().values()]:
        assert value not in repr(provider)
    assert capsys.readouterr() == ('', '') and caplog.text == ''


def test_encrypt_uses_a_fresh_nonce_for_repeated_plaintext():
    provider = EnvironmentAESGCMKeyProvider(KEYS, 'v1')
    encrypted = [provider.encrypt(PLAINTEXT, CONTEXT) for _ in range(8)]
    assert len({json.loads(value)['nonce'] for value in encrypted}) == 8
    assert all(provider.decrypt(value, CONTEXT) == PLAINTEXT for value in encrypted)


def test_rotation_and_restart_retain_old_ciphertext():
    old = EnvironmentAESGCMKeyProvider({'v1': KEYS['v1']}, 'v1')
    old_ciphertext = old.encrypt(PLAINTEXT, CONTEXT)
    prepared = EnvironmentAESGCMKeyProvider.from_env(key_env('v1'))
    assert prepared.decrypt(old_ciphertext, CONTEXT) == PLAINTEXT
    rotated = EnvironmentAESGCMKeyProvider.from_env(key_env('v2'))
    new_ciphertext = rotated.encrypt(rotated.decrypt(old_ciphertext, CONTEXT), CONTEXT)
    assert json.loads(new_ciphertext)['key_id'] == 'v2'
    restarted = EnvironmentAESGCMKeyProvider.from_env(key_env('v2'))
    assert restarted.decrypt(old_ciphertext, CONTEXT) == restarted.decrypt(new_ciphertext, CONTEXT) == PLAINTEXT
    retired = EnvironmentAESGCMKeyProvider({'v2': KEYS['v2']}, 'v2')
    assert retired.decrypt(new_ciphertext, CONTEXT) == PLAINTEXT
    with pytest.raises(SecretError):
        retired.decrypt(old_ciphertext, CONTEXT)
    with pytest.raises(SecretError):
        old.decrypt(new_ciphertext, CONTEXT)


@pytest.mark.parametrize('field,value', [
    ('REPLAY_SECRET_ACTIVE_KEY', None), ('REPLAY_SECRET_ACTIVE_KEY', 'missing'),
    ('REPLAY_SECRET_ACTIVE_KEY', 'V1'), ('REPLAY_SECRET_KEY_IDS', None),
    ('REPLAY_SECRET_KEY_IDS', ''), ('REPLAY_SECRET_KEY_IDS', 'v1,v1'),
    ('REPLAY_SECRET_KEY_IDS', 'v1,V1'), ('REPLAY_SECRET_KEY_IDS', 'v1,'),
    ('REPLAY_SECRET_KEY_IDS', 'v1, v2'), ('REPLAY_SECRET_KEY_IDS', '1v'),
    ('REPLAY_SECRET_KEY_IDS', 'v-one'), ('REPLAY_SECRET_KEY_IDS', 'v' * 33),
    ('REPLAY_SECRET_KEY_IDS', ','.join(f'v{i}' for i in range(9))),
    ('REPLAY_SECRET_KEY_V1', None), ('REPLAY_SECRET_KEY_V1', 'secret-invalid-key'),
    ('REPLAY_SECRET_KEY_V1', base64.b64encode(b'\x11' * 31).decode()),
    ('REPLAY_SECRET_KEY_V1', base64.b64encode(b'\x11' * 33).decode()),
    ('REPLAY_SECRET_KEY_V1', base64.b64encode(KEYS['v1']).decode().rstrip('=')),
    ('REPLAY_SECRET_KEY_V1', base64.b64encode(KEYS['v1']).decode() + '\n'),
    ('REPLAY_SECRET_KEY_V1', '_' * 43 + '='),
    ('REPLAY_SECRET_KEY_V1', b'not-a-string'),
])
def test_invalid_environment_fails_with_fixed_message(field, value):
    env = key_env()
    env[field] = value
    with pytest.raises(SecretError) as error:
        EnvironmentAESGCMKeyProvider.from_env(env)
    assert str(error.value) == 'Invalid environment encryption configuration'
    assert error.value.__suppress_context__


def test_env_key_encoding_must_be_canonical():
    env = key_env()
    canonical = env['REPLAY_SECRET_KEY_V1']
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    # Change only unused base64 pad bits: permissive decoders return the same key.
    changed = canonical[:-2] + alphabet[alphabet.index(canonical[-2]) + 1] + '='
    assert base64.b64decode(changed) == KEYS['v1']
    env['REPLAY_SECRET_KEY_V1'] = changed
    with pytest.raises(SecretError):
        EnvironmentAESGCMKeyProvider.from_env(env)


def test_only_declared_environment_names_are_read(monkeypatch):
    source = key_env()
    reads = []
    class ExactEnvironment(Mapping):
        def __getitem__(self, name):
            reads.append(name)
            return source[name]
        def __iter__(self):
            raise AssertionError('Do not enumerate environment secrets')
        def __len__(self):
            return len(source)
    monkeypatch.setattr('server.secrets.os', SimpleNamespace(environ=ExactEnvironment()))
    assert EnvironmentAESGCMKeyProvider.from_env().active_key_id == 'v1'
    assert set(reads) == set(source)
    with pytest.raises(SecretError):
        EnvironmentAESGCMKeyProvider.from_env({})


def test_constructor_enforces_key_count_and_raw_key_size():
    eight = {f'v{i}': bytes([i + 1]) * 32 for i in range(8)}
    assert EnvironmentAESGCMKeyProvider(eight, 'v0').active_key_id == 'v0'
    invalid = [({}, 'v1'), ({**eight, 'v8': b'\x09' * 32}, 'v0'),
               ({'V1': KEYS['v1']}, 'V1'), ({'v1': b'short'}, 'v1'),
               ({'v1': base64.b64encode(KEYS['v1']).decode()}, 'v1'),
               (KEYS, 'missing'), (None, 'v1')]
    for keys, active in invalid:
        with pytest.raises(SecretError):
            EnvironmentAESGCMKeyProvider(keys, active)


@pytest.mark.parametrize('plaintext', ['', b'bytes', None, 'x' * 4097, '가' * 1366, '\ud800'],
                         ids=['empty', 'bytes', 'none', 'too-long', 'multibyte-too-long', 'invalid-unicode'])
def test_plaintext_limit_counts_utf8_bytes(plaintext):
    provider = EnvironmentAESGCMKeyProvider(KEYS, 'v1')
    with pytest.raises(SecretError, match='^Secret encryption failed$'):
        provider.encrypt(plaintext, CONTEXT)
    exact = '가' * 1365 + 'x'
    assert len(exact.encode()) == 4096
    assert provider.decrypt(provider.encrypt(exact, CONTEXT), CONTEXT) == exact


@pytest.mark.parametrize('context', [None, {}, {'tenant_id': ''}, {'tenant_id': 3},
    {'tenant_id': 'x' * 257}, {**CONTEXT, 'bad': True}, {**CONTEXT, 'x' * 129: 'x'},
    {'tenant_id': 't', **{f'k{i}': 'v' for i in range(16)}}, [('tenant_id', 't')]])
def test_invalid_context_is_rejected_for_both_operations(context):
    provider = EnvironmentAESGCMKeyProvider(KEYS, 'v1')
    ciphertext = provider.encrypt(PLAINTEXT, CONTEXT)
    with pytest.raises(SecretError):
        provider.encrypt(PLAINTEXT, context)
    with pytest.raises(SecretError):
        provider.decrypt(ciphertext, context)


@pytest.mark.parametrize('field', list(CONTEXT))
def test_every_owner_context_field_is_authenticated(field):
    provider = EnvironmentAESGCMKeyProvider(KEYS, 'v1')
    ciphertext = provider.encrypt(PLAINTEXT, CONTEXT)
    for changed in ({**CONTEXT, field: 'other'}, {key: value for key, value in CONTEXT.items() if key != field}):
        with pytest.raises(SecretError):
            provider.decrypt(ciphertext, changed)


@pytest.mark.parametrize('field,value', [('v', 2), ('v', True), ('v', 1.0),
    ('provider', 'local'), ('provider', 'aws-kms'), ('key_id', 'missing'), ('key_id', 'v2'),
    ('nonce', ''), ('nonce', '!' * 16), ('nonce', base64.b64encode(b'x' * 11).decode()),
    ('nonce', base64.b64encode(b'x' * 13).decode()), ('ciphertext', ''),
    ('ciphertext', base64.b64encode(b'x' * 16).decode()), ('ciphertext', 'bad*'),
    ('ciphertext', base64.b64encode(b'x' * 4113).decode()), ('extra', 'not-allowed')],
    ids=lambda value: f'text-{len(value)}' if isinstance(value, str) and len(value) > 80 else None)
def test_modified_envelope_fails_closed(field, value):
    # Same raw key under two IDs proves that key_id itself is authenticated.
    provider = EnvironmentAESGCMKeyProvider({'v1': KEYS['v1'], 'v2': KEYS['v1']}, 'v1')
    envelope = json.loads(provider.encrypt(PLAINTEXT, CONTEXT))
    envelope[field] = value
    with pytest.raises(SecretError, match='^Secret cannot be decrypted for this context$'):
        provider.decrypt(json.dumps(envelope), CONTEXT)


@pytest.mark.parametrize('field', ['nonce', 'ciphertext'])
def test_authentication_rejects_bit_flip(field):
    provider = EnvironmentAESGCMKeyProvider(KEYS, 'v1')
    envelope = json.loads(provider.encrypt(PLAINTEXT, CONTEXT))
    original = base64.b64decode(envelope[field])
    envelope[field] = base64.b64encode(bytes([original[0] ^ 1]) + original[1:]).decode()
    with pytest.raises(SecretError):
        provider.decrypt(json.dumps(envelope), CONTEXT)


@pytest.mark.parametrize('value', [None, [], '{}', '[]', 'null', 'x' * 8193,
                                  '{"v":1,"v":1}', '{"broken":'],
                         ids=['none', 'list', 'empty-object', 'json-list', 'null', 'too-long', 'duplicate', 'incomplete'])
def test_malformed_ciphertext_is_rejected(value):
    with pytest.raises(SecretError):
        EnvironmentAESGCMKeyProvider(KEYS, 'v1').decrypt(value, CONTEXT)


def test_internal_errors_and_local_production_refusal_never_expose_keys(monkeypatch, capsys, caplog):
    provider = EnvironmentAESGCMKeyProvider(KEYS, 'v1')
    def fail(size):
        raise RuntimeError(PLAINTEXT + key_env()['REPLAY_SECRET_KEY_V1'])
    monkeypatch.setattr('server.secrets.os.urandom', fail)
    with pytest.raises(SecretError) as error:
        provider.encrypt(PLAINTEXT, CONTEXT)
    assert str(error.value) == 'Secret encryption failed' and error.value.__suppress_context__
    with pytest.raises(SecretError, match='Local master keys are forbidden in production'):
        LocalKeyringProvider({'v1': key_env()['REPLAY_SECRET_KEY_V1']}, 'v1', mode='production')
    assert capsys.readouterr() == ('', '') and caplog.text == ''
