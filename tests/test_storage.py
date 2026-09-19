"""Storage tests use an injected S3 client and real private development files."""
import base64
import asyncio
import hashlib
import io
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from server.storage import (IntegrityError, LocalStorage, ObjectNotFound,
                            S3Storage, StorageError)


DATA = b'private synthetic video content'
SHA = hashlib.sha256(DATA).hexdigest()


class FakeS3:
    def __init__(self):
        self.calls = []
        self.content = DATA
        self.content_type = 'video/mp4'
        self.checksum = base64.b64encode(bytes.fromhex(SHA)).decode()
        self.private = True

    def generate_presigned_url(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        return 'https://private.example/object?signed=opaque'

    def head_object(self, **kwargs):
        self.calls.append(('head', kwargs))
        return {'ContentLength': len(self.content), 'ChecksumSHA256': self.checksum,
                'ETag': '"opaque-multipart-etag-2"', 'ContentType': self.content_type}

    def get_object(self, **kwargs):
        self.calls.append(('get', kwargs))
        return {'Body': io.BytesIO(self.content)}

    def put_object(self, **kwargs):
        self.calls.append(('put', {k: v for k, v in kwargs.items() if k != 'Body'}))
        self.content = kwargs['Body'].read()
        self.checksum = kwargs['ChecksumSHA256']
        self.content_type = kwargs['ContentType']
        return {'ETag': '"opaque"'}

    def delete_object(self, **kwargs):
        self.calls.append(('delete', kwargs))

    def get_public_access_block(self, **kwargs):
        return {'PublicAccessBlockConfiguration': {name: self.private for name in
            ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')}}

    def head_bucket(self, **kwargs):
        return {}


@pytest.fixture
def local(tmp_path):
    return LocalStorage(tmp_path / 'objects', signing_key=b'x' * 32,
                        base_url='http://localhost:8080', allow_development=True)


def signed_args(signed):
    parsed = {key: values[0] for key, values in parse_qs(urlsplit(signed['url']).query).items()}
    constraints = {}
    if 'size' in parsed:
        constraints = {'size': int(parsed['size']), 'sha256': parsed['sha256'], 'content_type': parsed['content_type']}
    return parsed, constraints


def test_storage_keys_scope_tenants_without_leaking_provider_subjects(local):
    tenant = 'organization:provider/customer-id'
    key = local.key(tenant, '123')
    assert tenant not in key and key.endswith('/media/123.mp4')
    assert local.key('other', '123') != key
    for malicious in [key.replace('/media/', '/../'), key + '/../../escape', key.replace('/media/', '/media//'), '/etc/passwd', key + '\\escape']:
        with pytest.raises(StorageError):
            local.presign_download(tenant, malicious)
    with pytest.raises(StorageError, match='belong'):
        local.presign_download('other', key)


def test_local_storage_requires_explicit_development_and_private_key(tmp_path):
    with pytest.raises(StorageError, match='development'):
        LocalStorage(tmp_path, signing_key=b'x' * 32, base_url='http://localhost')
    with pytest.raises(StorageError, match='32 bytes'):
        LocalStorage(tmp_path, signing_key='short', base_url='http://localhost', allow_development=True)


def test_local_signatures_bind_method_tenant_key_and_upload_constraints(local):
    key = local.key('a', 'sample')
    signed = local.presign_upload('a', key, size=len(DATA), sha256=SHA)
    args, constraints = signed_args(signed)
    verified = local.verify_local_signature('PUT', args['tenant'], args['key'], int(args['expires']), args['signature'], **constraints)
    assert verified == local.root / key
    for changes in [{'size': len(DATA) + 1}, {'sha256': '0' * 64}, {'content_type': 'video/x-flv'}]:
        with pytest.raises(StorageError, match='authorization'):
            local.verify_local_signature('PUT', 'a', key, int(args['expires']), args['signature'], **(constraints | changes))
    with pytest.raises(StorageError):
        local.verify_local_signature('GET', 'a', key, int(args['expires']), args['signature'])
    with pytest.raises(StorageError):
        local.verify_local_signature('PUT', 'b', key, int(args['expires']), args['signature'], **constraints)


def test_expired_signed_url_is_rejected(tmp_path):
    now = [1000]
    local = LocalStorage(tmp_path, signing_key=b'a' * 32, base_url='http://localhost',
                         allow_development=True, clock=lambda: now[0])
    key = local.key('a', 'sample')
    signed = local.presign_download('a', key, expires=10)
    args, _ = signed_args(signed)
    now[0] = 1010
    with pytest.raises(StorageError, match='expired'):
        local.verify_local_signature('GET', 'a', key, int(args['expires']), args['signature'])


def test_local_objects_are_create_only_verified_and_private(local, tmp_path):
    key = local.key('a', 'sample')
    result = local.put_bytes('a', key, DATA, size=len(DATA), sha256=SHA)
    assert result['sha256'] == SHA
    assert os.stat(local.root / key).st_mode & 0o777 == 0o600
    with pytest.raises(StorageError, match='already exists'):
        local.put_bytes('a', key, DATA, size=len(DATA), sha256=SHA)
    with pytest.raises(IntegrityError):
        local.verify_upload('a', key, size=len(DATA) + 1, sha256=SHA)
    with pytest.raises(IntegrityError):
        local.put_bytes('a', local.key('a', 'wrong'), DATA + b'x', size=len(DATA), sha256=SHA)
    destination = tmp_path / 'download.mp4'
    local.download('a', key, destination)
    assert destination.read_bytes() == DATA
    local.delete('a', key)
    local.delete('a', key)
    with pytest.raises(ObjectNotFound):
        local.head('a', key)


def test_local_paths_cannot_escape_through_existing_symlink(local, tmp_path):
    key = local.key('a', 'sample')
    tenant_directory = local.root / '/'.join(key.split('/')[:2])
    tenant_directory.parent.mkdir()
    tenant_directory.symlink_to(tmp_path)
    with pytest.raises(StorageError, match='escapes'):
        local.put_bytes('a', key, DATA, size=len(DATA), sha256=SHA)


def test_local_streamed_upload_roundtrip_and_flv_type(local, tmp_path):
    source = tmp_path / 'output.flv'
    source.write_bytes(DATA)
    key = local.key('a', 'job-1', kind='outputs', extension='flv')
    item = local.upload('a', key, source, content_type='video/x-flv')
    assert item['content_type'] == 'video/x-flv'
    assert local.health() is True
    with pytest.raises(StorageError):
        local.presign_upload('a', key, size=1, sha256=SHA, expires=901)


def test_async_local_upload_hashes_incrementally_without_rereading(local, monkeypatch):
    key = local.key('a', 'async-result', kind='outputs', extension='flv')
    synced = []
    original_fsync = os.fsync

    def sync(fd):
        synced.append(os.fstat(fd).st_size)
        original_fsync(fd)

    def unexpected_reread(*args, **kwargs):
        pytest.fail('Streaming upload must not reread the whole object')

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(hashlib, 'file_digest', unexpected_reread)

    async def chunks():
        yield b''
        for offset in range(0, len(DATA), 4):
            yield DATA[offset:offset + 4]

    result = asyncio.run(local.put_stream('a', key, chunks(), size=len(DATA), sha256=SHA, content_type='video/x-flv'))
    assert result == {'bytes': len(DATA), 'size': len(DATA), 'sha256': SHA,
        'etag': SHA, 'object_key': key, 'content_type': 'video/x-flv'}
    assert synced == [len(DATA)]
    path = local.root / key
    assert path.read_bytes() == DATA
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('failure', ['too_large', 'early_eof', 'checksum', 'disconnect', 'invalid_chunk'])
def test_async_local_upload_failure_removes_only_partial_file(local, failure, monkeypatch):
    key = local.key('a', 'async-failure')
    original_fdopen = os.fdopen
    written = []

    class RecordingFile:
        def __init__(self, *args, **kwargs):
            self.file = original_fdopen(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def write(self, data):
            written.append(bytes(data))
            return self.file.write(data)

    monkeypatch.setattr(os, 'fdopen', RecordingFile)

    async def chunks():
        yield DATA[:4]
        if failure == 'early_eof':
            return
        if failure == 'disconnect':
            raise ConnectionError('synthetic disconnect')
        if failure == 'invalid_chunk':
            yield 'invalid'
        elif failure == 'too_large':
            yield DATA[4:] + b'overflow'
        else:
            yield b'!' * (len(DATA) - 4)

    expected = ConnectionError if failure == 'disconnect' else IntegrityError
    with pytest.raises(expected):
        asyncio.run(local.put_stream('a', key, chunks(), size=len(DATA), sha256=SHA))
    assert not (local.root / key).exists()
    if failure == 'too_large':
        assert written == [DATA[:4]]  # The entire overflowing chunk was rejected before writing.


def test_cancelled_async_local_upload_removes_partial_file(local):
    key = local.key('a', 'cancelled')

    async def cancel_upload():
        started = asyncio.Event()

        async def chunks():
            yield DATA[:4]
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(local.put_stream('a', key, chunks(), size=len(DATA), sha256=SHA))
        await started.wait()
        assert (local.root / key).exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_upload())
    assert not (local.root / key).exists()


def test_duplicate_async_local_upload_preserves_existing_object_without_consuming_stream(local):
    key = local.key('a', 'existing')
    local.put_bytes('a', key, DATA, size=len(DATA), sha256=SHA)

    async def chunks():
        pytest.fail('Duplicate upload must fail before consuming the request body')
        yield b'unreachable'

    with pytest.raises(StorageError, match='already exists'):
        asyncio.run(local.put_stream('a', key, chunks(), size=len(DATA), sha256=SHA))
    assert (local.root / key).read_bytes() == DATA


def test_large_local_upload_signatures_keep_s3_single_put_limit(local):
    size = 12 * 1024**3
    key = local.key('a', 'large-local', kind='outputs', extension='flv')
    signed = local.presign_upload('a', key, size=size, sha256=SHA, content_type='video/x-flv')
    args, constraints = signed_args(signed)
    local.verify_local_signature('PUT', 'a', key, int(args['expires']), args['signature'], **constraints)
    assert local.max_put_bytes == 32 * 1024**3 and constraints['size'] == size
    client = FakeS3()
    s3 = S3Storage('replay-private', client=client)
    assert s3.max_put_bytes == 5 * 1024**3
    with pytest.raises(StorageError, match='limit'):
        s3.presign_upload('a', key, size=size, sha256=SHA, content_type='video/x-flv')
    assert client.calls == []


def test_s3_put_signature_binds_checksum_length_type_encryption_and_immutability():
    client = FakeS3()
    storage = S3Storage('replay-private', client=client, kms_key_id='alias/replay')
    key = storage.key('tenant', 'media-id')
    signed = storage.presign_upload('tenant', key, size=len(DATA), sha256=SHA)
    operation, call = client.calls[-1]
    assert operation == 'put_object'
    params = call['Params']
    assert params['ContentLength'] == len(DATA)
    assert params['ChecksumSHA256'] == base64.b64encode(bytes.fromhex(SHA)).decode()
    assert params['IfNoneMatch'] == '*'
    assert params['ServerSideEncryption'] == 'aws:kms' and params['SSEKMSKeyId'] == 'alias/replay'
    assert signed['headers']['If-None-Match'] == '*'
    assert signed['headers']['Content-Length'] == str(len(DATA))
    assert call['HttpMethod'] == 'PUT' and call['ExpiresIn'] == 900
    before = len(client.calls)
    with pytest.raises(StorageError):
        storage.presign_upload('different', key, size=len(DATA), sha256=SHA)
    assert len(client.calls) == before


def test_s3_head_validates_real_checksum_and_treats_etag_as_opaque():
    client = FakeS3()
    storage = S3Storage('replay-private', client=client)
    key = storage.key('a', 'sample')
    item = storage.verify_upload('a', key, size=len(DATA), sha256=SHA)
    assert item['etag'] == 'opaque-multipart-etag-2' and item['sha256'] == SHA
    assert client.calls[-1][1]['ChecksumMode'] == 'ENABLED'
    client.checksum = ''
    with pytest.raises(IntegrityError):
        storage.verify_upload('a', key, size=len(DATA), sha256=SHA)
    client.checksum = base64.b64encode(bytes.fromhex('0' * 64)).decode()
    with pytest.raises(IntegrityError):
        storage.verify_upload('a', key, size=len(DATA), sha256=SHA)


def test_s3_download_verifies_bytes_and_preserves_destination_on_corruption(tmp_path):
    client = FakeS3()
    storage = S3Storage('replay-private', client=client)
    key = storage.key('a', 'sample')
    destination = tmp_path / 'media.mp4'
    storage.download('a', key, destination)
    assert destination.read_bytes() == DATA
    client.content = b'x' * len(DATA)
    with pytest.raises(IntegrityError):
        storage.download('a', key, destination)
    assert destination.read_bytes() == DATA
    assert [path.name for path in tmp_path.iterdir()] == ['media.mp4']


def test_s3_upload_and_signed_download_do_not_enable_public_access(tmp_path):
    client = FakeS3()
    storage = S3Storage('replay-private', client=client)
    key = storage.key('a', 'output', kind='outputs', extension='flv')
    source = tmp_path / 'output.flv'
    source.write_bytes(DATA)
    item = storage.upload('a', key, source, content_type='video/x-flv')
    assert item['bytes'] == len(DATA)
    put = next(call for operation, call in client.calls if operation == 'put')
    assert 'ACL' not in put and put['ServerSideEncryption'] == 'AES256' and put['IfNoneMatch'] == '*'
    signed = storage.presign_download('a', key, filename='a"\r\nSet-Cookie: bad')
    assert signed['method'] == 'GET'
    disposition = client.calls[-1][1]['Params']['ResponseContentDisposition']
    assert '\r' not in disposition and '\n' not in disposition
    assert storage.health() is True
    client.private = False
    with pytest.raises(StorageError, match='public access'):
        storage.health()


def test_s3_sdk_failures_are_sanitized():
    class FailingClient(FakeS3):
        def head_object(self, **kwargs):
            raise RuntimeError('https://signed-url?secret=should-not-leak')

    storage = S3Storage('replay-private', client=FailingClient())
    with pytest.raises(StorageError) as caught:
        storage.head('a', storage.key('a', 'sample'))
    assert 'secret' not in str(caught.value)


def test_real_boto3_signer_accepts_and_signs_create_only_checksum_headers():
    import boto3
    from botocore.config import Config
    client = boto3.client('s3', region_name='us-east-1', aws_access_key_id='test-access',
                          aws_secret_access_key='test-secret', config=Config(signature_version='s3v4'))
    storage = S3Storage('replay-test-private', client=client)
    signed = storage.presign_upload('a', storage.key('a', 'sample'), size=len(DATA), sha256=SHA)
    headers = parse_qs(urlsplit(signed['url']).query)['X-Amz-SignedHeaders'][0].split(';')
    assert {'content-length', 'content-type', 'if-none-match', 'x-amz-checksum-sha256',
            'x-amz-server-side-encryption'}.issubset(headers)
