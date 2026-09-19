"""Versioned, tenant-bound secrets with explicitly selected production providers."""
import base64
import json
import os
import re
from typing import Mapping, Protocol

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .aws_identity import AWSIdentityError


class SecretError(ValueError):
    pass


class KeyProvider(Protocol):
    def encrypt(self, plaintext: str, context: Mapping[str, str] | None = None) -> str: ...
    def decrypt(self, ciphertext: str, context: Mapping[str, str] | None = None) -> str: ...


def _context(context):
    result = dict(context or {})
    if not result.get('tenant_id') or not all(isinstance(key, str) and isinstance(value, str)
                                            and 0 < len(key) <= 128 and 0 < len(value) <= 256
                                            for key, value in result.items()):
        raise SecretError('Secret encryption requires a valid tenant context')
    if len(result) > 16:
        raise SecretError('Secret context is too large')
    return result


def _encode(provider, ciphertext, key_id):
    return json.dumps({'v': 1, 'provider': provider, 'key_id': key_id, 'ciphertext': ciphertext},
                      separators=(',', ':'), sort_keys=True)


def _decode(value, provider):
    try:
        envelope = json.loads(value)
        if not isinstance(envelope, dict) or envelope.get('v') != 1 or envelope.get('provider') != provider:
            raise ValueError()
        if not isinstance(envelope.get('key_id'), str) or not isinstance(envelope.get('ciphertext'), str):
            raise ValueError()
        return envelope
    except (ValueError, TypeError):
        raise SecretError('Invalid secret envelope') from None


class LocalKeyringProvider:
    """Development-only key rotation, supplied by the caller; never writes key files."""
    def __init__(self, keys: Mapping[str, str | bytes], active_key_id: str, *, mode: str):
        if mode not in ('development', 'test'):
            raise SecretError('Local master keys are forbidden in production')
        if not keys or active_key_id not in keys:
            raise SecretError('An active development key is required')
        self._keys = {name: Fernet(value.encode() if isinstance(value, str) else value) for name, value in keys.items()}
        self.active_key_id = active_key_id

    def encrypt(self, plaintext: str, context: Mapping[str, str] | None = None) -> str:
        if not isinstance(plaintext, str):
            raise SecretError('Secret must be text')
        payload = json.dumps({'value': plaintext, 'context': _context(context)}, sort_keys=True).encode()
        encrypted = self._keys[self.active_key_id].encrypt(payload).decode()
        return _encode('local', encrypted, self.active_key_id)

    def decrypt(self, ciphertext: str, context: Mapping[str, str] | None = None) -> str:
        expected = _context(context)
        envelope = _decode(ciphertext, 'local')
        try:
            payload = json.loads(self._keys[envelope['key_id']].decrypt(envelope['ciphertext'].encode()))
            if payload['context'] != expected or not isinstance(payload['value'], str):
                raise ValueError()
            return payload['value']
        except (KeyError, InvalidToken, ValueError, TypeError):
            raise SecretError('Secret cannot be decrypted for this context') from None


class EnvironmentAESGCMKeyProvider:
    """Production AES keys supplied explicitly; no generation, files, or fallback.

    Keep versioned keys in API-only Secret environment variables. Retain old
    versions until stored ciphertext, retained backups, and rollback deployments
    no longer need them. Keys are readable by the API process, unlike remote KMS.
    """
    _KEY_ID = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
    _PROVIDER = 'env-aesgcm'

    def __init__(self, keys: Mapping[str, bytes], active_key_id: str):
        try:
            if (not isinstance(keys, Mapping) or not 1 <= len(keys) <= 8
                    or not isinstance(active_key_id, str) or not self._KEY_ID.fullmatch(active_key_id)
                    or active_key_id not in keys):
                raise ValueError()
            if any(not isinstance(name, str) or not self._KEY_ID.fullmatch(name)
                   or not isinstance(value, bytes) or len(value) != 32 for name, value in keys.items()):
                raise ValueError()
            self._keys = {name: AESGCM(value) for name, value in keys.items()}
            self.active_key_id = active_key_id
        except Exception:
            raise SecretError('Invalid environment encryption configuration') from None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        """Read only declared key variables; an explicit mapping replaces env."""
        try:
            source = os.environ if environ is None else environ
            if not isinstance(source, Mapping):
                raise ValueError()
            active = source.get('REPLAY_SECRET_ACTIVE_KEY')
            declared = source.get('REPLAY_SECRET_KEY_IDS')
            if not isinstance(declared, str) or not 1 <= len(declared) <= 263:
                raise ValueError()
            names = declared.split(',')
            if (not 1 <= len(names) <= 8 or len(set(names)) != len(names)
                    or any(not cls._KEY_ID.fullmatch(name) for name in names)):
                raise ValueError()
            keys = {}
            for name in names:
                encoded = source.get('REPLAY_SECRET_KEY_' + name.upper())
                if not isinstance(encoded, str) or len(encoded) != 44:
                    raise ValueError()
                decoded = base64.b64decode(encoded, validate=True)
                if len(decoded) != 32 or base64.b64encode(decoded).decode('ascii') != encoded:
                    raise ValueError()
                keys[name] = decoded
            return cls(keys, active)
        except Exception:
            raise SecretError('Invalid environment encryption configuration') from None

    @classmethod
    def _aad(cls, key_id, context):
        if not isinstance(context, Mapping):
            raise ValueError()
        return json.dumps({'v': 1, 'provider': cls._PROVIDER, 'key_id': key_id,
                           'context': _context(context)}, sort_keys=True, separators=(',', ':')).encode('utf-8')

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def encrypt(self, plaintext: str, context: Mapping[str, str] | None = None) -> str:
        try:
            if not isinstance(plaintext, str):
                raise ValueError()
            data = plaintext.encode('utf-8')
            if not 1 <= len(data) <= 4096:
                raise ValueError()
            key_id = self.active_key_id
            aad = self._aad(key_id, context)
            nonce = os.urandom(12)
            encrypted = self._keys[key_id].encrypt(nonce, data, aad)
            return json.dumps({'v': 1, 'provider': self._PROVIDER, 'key_id': key_id,
                               'nonce': base64.b64encode(nonce).decode('ascii'),
                               'ciphertext': base64.b64encode(encrypted).decode('ascii')},
                              sort_keys=True, separators=(',', ':'))
        except Exception:
            raise SecretError('Secret encryption failed') from None

    def decrypt(self, ciphertext: str, context: Mapping[str, str] | None = None) -> str:
        try:
            if not isinstance(ciphertext, str) or not 1 <= len(ciphertext) <= 8192:
                raise ValueError()
            envelope = json.loads(ciphertext, object_pairs_hook=self._unique_object)
            if (not isinstance(envelope, dict)
                    or set(envelope) != {'v', 'provider', 'key_id', 'nonce', 'ciphertext'}
                    or type(envelope['v']) is not int or envelope['v'] != 1
                    or envelope['provider'] != self._PROVIDER
                    or not isinstance(envelope['key_id'], str) or envelope['key_id'] not in self._keys):
                raise ValueError()
            nonce_text, encrypted_text = envelope['nonce'], envelope['ciphertext']
            if (not isinstance(nonce_text, str) or len(nonce_text) != 16
                    or not isinstance(encrypted_text, str)):
                raise ValueError()
            nonce = base64.b64decode(nonce_text, validate=True)
            encrypted = base64.b64decode(encrypted_text, validate=True)
            if (len(nonce) != 12 or not 17 <= len(encrypted) <= 4112
                    or base64.b64encode(nonce).decode('ascii') != nonce_text
                    or base64.b64encode(encrypted).decode('ascii') != encrypted_text):
                raise ValueError()
            aad = self._aad(envelope['key_id'], context)
            return self._keys[envelope['key_id']].decrypt(nonce, encrypted, aad).decode('utf-8')
        except Exception:
            raise SecretError('Secret cannot be decrypted for this context') from None


class AWSKMSKeyProvider:
    """Use the runtime IAM identity and KMS encryption context; no local master key."""
    def __init__(self, key_id: str, *, region_name: str | None = None, client=None):
        if not key_id or not key_id.startswith('arn:') or ':kms:' not in key_id or ':key/' not in key_id:
            raise SecretError('Production KMS requires a fixed key ARN')
        self.key_id = key_id
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client('kms', region_name=region_name,
                                  config=Config(connect_timeout=3, read_timeout=5, retries={'total_max_attempts': 2, 'mode': 'standard'}))
        self._client = client

    def encrypt(self, plaintext: str, context: Mapping[str, str] | None = None) -> str:
        if not isinstance(plaintext, str) or not 1 <= len(plaintext.encode()) <= 4096:
            raise SecretError('Secret must contain 1 to 4096 bytes')
        encryption_context = _context(context)
        try:
            result = self._client.encrypt(KeyId=self.key_id, Plaintext=plaintext.encode(),
                                          EncryptionContext=encryption_context, EncryptionAlgorithm='SYMMETRIC_DEFAULT')
            encrypted = base64.b64encode(result['CiphertextBlob']).decode()
            return _encode('aws-kms', encrypted, self.key_id)
        except AWSIdentityError:
            raise
        except Exception:
            raise SecretError('KMS encryption failed') from None

    def decrypt(self, ciphertext: str, context: Mapping[str, str] | None = None) -> str:
        encryption_context = _context(context)
        envelope = _decode(ciphertext, 'aws-kms')
        if envelope['key_id'] != self.key_id:
            raise SecretError('Secret uses an unapproved KMS key')
        try:
            encrypted = base64.b64decode(envelope['ciphertext'], validate=True)
            result = self._client.decrypt(KeyId=self.key_id, CiphertextBlob=encrypted,
                                          EncryptionContext=encryption_context, EncryptionAlgorithm='SYMMETRIC_DEFAULT')
            return result['Plaintext'].decode()
        except AWSIdentityError:
            raise
        except Exception:
            raise SecretError('Secret cannot be decrypted for this context') from None


def redact(value: str, sensitive_values=()) -> str:
    """Sanitize explicitly known secrets plus common credential/stream URL forms."""
    result = str(value)
    for secret in sorted((str(item) for item in sensitive_values if item), key=len, reverse=True):
        result = result.replace(secret, '[REDACTED]')
    result = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [REDACTED]', result)
    result = re.sub(r'(?i)rtmps?://[^\s\"\'<>]+', '[REDACTED_STREAM_URL]', result)
    result = re.sub(r'(?i)([\"\']?(?:stream_key|invite_code|access_token|refresh_token|authorization)[\"\']?\s*[:=]\s*)([\"\']?)[^\s,}\"\']+\2',
                    r'\1[REDACTED]', result)
    return result
