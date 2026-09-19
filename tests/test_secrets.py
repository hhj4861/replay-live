import json

import boto3
from botocore.stub import Stubber
from cryptography.fernet import Fernet
import pytest

from server.secrets import AWSKMSKeyProvider, LocalKeyringProvider, SecretError, redact

CONTEXT = {'tenant_id': 'tenant-a', 'job_id': 'job-a'}
KEY_ARN = 'arn:aws:kms:us-east-1:111122223333:key/12345678-1234-1234-1234-123456789012'


def test_development_keyring_rotation_context_and_tamper_protection():
    old, new = Fernet.generate_key(), Fernet.generate_key()
    first = LocalKeyringProvider({'old': old}, 'old', mode='test')
    encrypted = first.encrypt('synthetic-stream-key', CONTEXT)
    assert 'synthetic-stream-key' not in encrypted
    rotated = LocalKeyringProvider({'old': old, 'new': new}, 'new', mode='development')
    assert rotated.decrypt(encrypted, CONTEXT) == 'synthetic-stream-key'
    assert json.loads(rotated.encrypt('next-secret', CONTEXT))['key_id'] == 'new'
    with pytest.raises(SecretError):
        rotated.decrypt(encrypted, {'tenant_id': 'tenant-b', 'job_id': 'job-a'})
    with pytest.raises(SecretError):
        rotated.decrypt(encrypted, {'tenant_id': 'tenant-a', 'job_id': 'different-job'})
    changed = json.loads(encrypted)
    changed['ciphertext'] = changed['ciphertext'][:-5] + 'AAAAA'
    with pytest.raises(SecretError):
        rotated.decrypt(json.dumps(changed), CONTEXT)
    with pytest.raises(SecretError):
        LocalKeyringProvider({'old': old}, 'old', mode='production')
    with pytest.raises(SecretError):
        first.encrypt('secret', {})


def test_kms_adapter_uses_key_arn_and_encryption_context_with_real_sdk_stubber():
    client = boto3.client('kms', region_name='us-east-1', aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
    provider = AWSKMSKeyProvider(KEY_ARN, client=client)
    with Stubber(client) as stub:
        stub.add_response('encrypt', {'CiphertextBlob': b'kms-ciphertext', 'KeyId': KEY_ARN},
                          {'KeyId': KEY_ARN, 'Plaintext': b'synthetic-stream-key', 'EncryptionContext': CONTEXT,
                           'EncryptionAlgorithm': 'SYMMETRIC_DEFAULT'})
        encrypted = provider.encrypt('synthetic-stream-key', CONTEXT)
        assert json.loads(encrypted)['provider'] == 'aws-kms'
        assert 'synthetic-stream-key' not in encrypted
        stub.add_response('decrypt', {'Plaintext': b'synthetic-stream-key', 'KeyId': KEY_ARN},
                          {'KeyId': KEY_ARN, 'CiphertextBlob': b'kms-ciphertext', 'EncryptionContext': CONTEXT,
                           'EncryptionAlgorithm': 'SYMMETRIC_DEFAULT'})
        assert provider.decrypt(encrypted, CONTEXT) == 'synthetic-stream-key'
        stub.assert_no_pending_responses()
    changed = json.loads(encrypted)
    changed['key_id'] = KEY_ARN.replace('111122223333', '999999999999')
    with pytest.raises(SecretError, match='unapproved'):
        provider.decrypt(json.dumps(changed), CONTEXT)


def test_kms_denial_does_not_echo_sdk_exception_or_sensitive_value():
    client = boto3.client('kms', region_name='us-east-1', aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
    with Stubber(client) as stub:
        stub.add_client_error('encrypt', service_error_code='AccessDeniedException',
                              service_message='synthetic-private-detail', http_status_code=403)
        with pytest.raises(SecretError) as error:
            AWSKMSKeyProvider(KEY_ARN, client=client).encrypt('synthetic-value', CONTEXT)
        assert 'synthetic' not in str(error.value)


def test_redaction_removes_bearer_urls_and_explicit_secrets():
    text = 'Authorization: Bearer synthetic-bearer stream_key=synthetic-key rtmps://ingest.example/live2/synthetic-stream known-secret'
    sanitized = redact(text, ['known-secret'])
    assert all(secret not in sanitized for secret in ('synthetic-bearer', 'synthetic-key', 'synthetic-stream', 'known-secret'))
