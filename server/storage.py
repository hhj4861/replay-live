"""Private immutable object storage, with explicit development-only filesystem.

Signed PUTs bind length, checksum, type and create-only semantics. ETags are
recorded as opaque version markers, never treated as a content hash. A successful
upload is admitted only after S3's verified SHA-256 and actual size match.
"""
import base64
from collections.abc import AsyncIterable
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlencode, urlsplit

from .aws_identity import AWSIdentityError
from .output_policy import MAX_OUTPUT_BYTES, S3_SINGLE_PUT_MAX_BYTES


class StorageError(ValueError):
    pass


class ObjectNotFound(StorageError):
    pass


class IntegrityError(StorageError):
    pass


def _checksum(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise StorageError('A lowercase SHA-256 checksum is required')
    return base64.b64encode(bytes.fromhex(value)).decode('ascii')


def _upload_constraints(size, sha256, content_type, *, max_bytes=S3_SINGLE_PUT_MAX_BYTES):
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= max_bytes:
        raise StorageError('Object size is outside the single-upload limit')
    checksum = _checksum(sha256)
    if content_type not in ('video/mp4', 'video/x-flv', 'application/octet-stream'):
        raise StorageError('Unsupported object content type')
    return checksum


def _expiry(expires):
    if isinstance(expires, bool) or not isinstance(expires, int) or not 1 <= expires <= 900:
        raise StorageError('Signed URLs must expire within 15 minutes')
    return expires


class _Keys:
    def __init__(self, prefix='replay'):
        if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', prefix):
            raise StorageError('Invalid object prefix')
        self.prefix = prefix

    def _tenant_prefix(self, tenant_id):
        if not isinstance(tenant_id, str) or not tenant_id.strip() or len(tenant_id) > 200 or any(ord(c) < 32 for c in tenant_id):
            raise StorageError('Invalid tenant')
        return f'{self.prefix}/{hashlib.sha256(tenant_id.encode()).hexdigest()}/'

    def key(self, tenant_id, object_id, kind='media', extension='mp4'):
        if kind not in ('media', 'outputs') or extension not in ('mp4', 'flv') or not isinstance(object_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', object_id):
            raise StorageError('Invalid object identity')
        return f'{self._tenant_prefix(tenant_id)}{kind}/{object_id}.{extension}'

    def _validate_key(self, tenant_id, key):
        prefix = self._tenant_prefix(tenant_id)
        if not isinstance(key, str) or not key.startswith(prefix) or not re.fullmatch(r'(?:media|outputs)/[A-Za-z0-9_-]{1,128}\.(?:mp4|flv)', key[len(prefix):]):
            raise StorageError('Object does not belong to this tenant')
        return key


class S3Storage(_Keys):
    max_put_bytes = S3_SINGLE_PUT_MAX_BYTES

    def __init__(self, bucket, *, client=None, prefix='replay', kms_key_id=None, region_name=None):
        super().__init__(prefix)
        if not isinstance(bucket, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
            raise StorageError('Invalid S3 bucket')
        self.bucket = bucket
        self.kms_key_id = kms_key_id
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client('s3', region_name=region_name, config=Config(signature_version='s3v4',
                connect_timeout=3, read_timeout=5, retries={'total_max_attempts': 2, 'mode': 'standard'}))
        self.client = client

    def _encryption(self):
        if self.kms_key_id:
            return {'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': self.kms_key_id}
        return {'ServerSideEncryption': 'AES256'}

    def presign_upload(self, tenant_id, key, *, size, sha256, expires=900, content_type='video/mp4'):
        key = self._validate_key(tenant_id, key)
        checksum = _upload_constraints(size, sha256, content_type)
        params = {'Bucket': self.bucket, 'Key': key, 'ContentLength': size,
                  'ContentType': content_type, 'ChecksumSHA256': checksum,
                  'IfNoneMatch': '*', **self._encryption()}
        headers = {'Content-Type': content_type, 'Content-Length': str(size),
                   'x-amz-checksum-sha256': checksum, 'If-None-Match': '*',
                   'x-amz-server-side-encryption': params['ServerSideEncryption']}
        if self.kms_key_id:
            headers['x-amz-server-side-encryption-aws-kms-key-id'] = self.kms_key_id
        try:
            url = self.client.generate_presigned_url('put_object', Params=params,
                ExpiresIn=_expiry(expires), HttpMethod='PUT')
        except AWSIdentityError:
            raise
        except Exception:
            raise StorageError('Could not issue upload authorization') from None
        return {'url': url, 'method': 'PUT', 'headers': headers, 'expires_in': expires,
                'object_key': key}

    def presign_download(self, tenant_id, key, *, expires=300, filename=None):
        key = self._validate_key(tenant_id, key)
        params = {'Bucket': self.bucket, 'Key': key}
        if filename is not None:
            safe = re.sub(r'[^a-zA-Z0-9._-]', '_', str(filename))[:180] or 'video.mp4'
            params['ResponseContentDisposition'] = f'attachment; filename="{safe}"'
        try:
            url = self.client.generate_presigned_url('get_object', Params=params,
                ExpiresIn=_expiry(expires), HttpMethod='GET')
        except AWSIdentityError:
            raise
        except Exception:
            raise StorageError('Could not issue download authorization') from None
        return {'url': url, 'method': 'GET', 'headers': {}, 'expires_in': expires}

    def head(self, tenant_id, key):
        key = self._validate_key(tenant_id, key)
        try:
            item = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode='ENABLED')
        except AWSIdentityError:
            raise
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
                raise ObjectNotFound('Object not found') from None
            raise StorageError('Could not inspect stored object') from None
        checksum = item.get('ChecksumSHA256', '')
        try:
            sha256 = base64.b64decode(checksum, validate=True).hex() if checksum else ''
        except (ValueError, TypeError):
            raise IntegrityError('Stored object checksum is invalid') from None
        return {'bytes': int(item['ContentLength']), 'size': int(item['ContentLength']),
                'sha256': sha256, 'etag': str(item.get('ETag', '')).strip('"'),
                'content_type': item.get('ContentType', 'application/octet-stream'),
                'version_id': item.get('VersionId'), 'object_key': key}

    def verify_upload(self, tenant_id, key, *, size, sha256, content_type='video/mp4'):
        _upload_constraints(size, sha256, content_type)
        item = self.head(tenant_id, key)
        if item['bytes'] != size or not hmac.compare_digest(item['sha256'], sha256) or item['content_type'] != content_type:
            raise IntegrityError('Uploaded object size, checksum or type does not match')
        return item

    def download(self, tenant_id, key, path):
        key = self._validate_key(tenant_id, key)
        expected = self.head(tenant_id, key)
        if expected['bytes'] > 5 * 1024**3 or not re.fullmatch('[a-f0-9]{64}', expected['sha256']):
            raise IntegrityError('Stored object size or checksum is invalid')
        destination = Path(path)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = None
        body = None
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key, ChecksumMode='ENABLED', IfMatch=expected['etag'])
            body = response['Body']
            digest = hashlib.sha256()
            actual = 0
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                tmp = Path(output.name)
                while chunk := body.read(1024 * 1024):
                    actual += len(chunk)
                    if actual > expected['bytes']:
                        raise IntegrityError('Downloaded object is larger than expected')
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if actual != expected['bytes'] or not hmac.compare_digest(digest.hexdigest(), expected['sha256']):
                raise IntegrityError('Downloaded object checksum does not match')
            os.replace(tmp, destination)
            tmp = None
            return expected
        except StorageError:
            raise
        except AWSIdentityError:
            raise
        except Exception:
            raise StorageError('Could not download object') from None
        finally:
            if body is not None:
                body.close()
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def upload(self, tenant_id, key, path, content_type='video/mp4'):
        key = self._validate_key(tenant_id, key)
        path = Path(path)
        size = path.stat().st_size
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        checksum = _upload_constraints(size, digest, content_type)
        try:
            with path.open('rb') as source:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=source,
                    ContentLength=size, ContentType=content_type, ChecksumSHA256=checksum,
                    IfNoneMatch='*', **self._encryption())
        except AWSIdentityError:
            raise
        except Exception:
            raise StorageError('Could not upload immutable object') from None
        return self.verify_upload(tenant_id, key, size=size, sha256=digest, content_type=content_type)

    def delete(self, tenant_id, key):
        key = self._validate_key(tenant_id, key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except AWSIdentityError:
            raise
        except Exception:
            raise StorageError('Could not delete object') from None

    def health(self):
        try:
            block = self.client.get_public_access_block(Bucket=self.bucket)['PublicAccessBlockConfiguration']
            if not all(block.get(name) is True for name in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')):
                raise StorageError('S3 bucket public access must be blocked')
            self.client.head_bucket(Bucket=self.bucket)
        except StorageError:
            raise
        except AWSIdentityError:
            raise
        except Exception:
            raise StorageError('Private object storage is unavailable') from None
        return True


class LocalStorage(_Keys):
    """Development only. Signed routes must use verify_local_signature first."""
    max_put_bytes = MAX_OUTPUT_BYTES

    def __init__(self, root, *, signing_key, base_url, allow_development=False,
                 prefix='replay', clock=time.time):
        super().__init__(prefix)
        if allow_development is not True:
            raise StorageError('Local storage requires explicit development mode')
        signing_key = signing_key.encode() if isinstance(signing_key, str) else signing_key
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise StorageError('Local storage signing key requires at least 32 bytes')
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('https', 'http') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise StorageError('Invalid local API base URL')
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.signing_key = signing_key
        self.base_url = base_url.rstrip('/')
        self.clock = clock

    def _path(self, tenant_id, key):
        self._validate_key(tenant_id, key)
        path = self.root / key
        if not path.resolve().is_relative_to(self.root):
            raise StorageError('Object path escapes private storage')
        return path

    def _signature(self, method, tenant_id, key, expires, size=None, sha256=None, content_type=None):
        payload = json.dumps([method, tenant_id, key, int(expires), size, sha256, content_type], separators=(',', ':')).encode()
        return hmac.new(self.signing_key, payload, hashlib.sha256).hexdigest()

    def _signed(self, method, tenant_id, key, expires, *, size=None, sha256=None, content_type=None):
        self._validate_key(tenant_id, key)
        expires_at = int(self.clock()) + _expiry(expires)
        query = {'tenant': tenant_id, 'key': key, 'expires': expires_at,
                 'signature': self._signature(method, tenant_id, key, expires_at, size, sha256, content_type)}
        if method == 'PUT':
            query.update(size=size, sha256=sha256, content_type=content_type)
        return {'url': f'{self.base_url}/api/storage/local?{urlencode(query)}',
                'method': method, 'headers': {'Content-Type': content_type} if method == 'PUT' else {},
                'expires_in': expires, 'object_key': key}

    def presign_upload(self, tenant_id, key, *, size, sha256, expires=900, content_type='video/mp4'):
        _upload_constraints(size, sha256, content_type, max_bytes=self.max_put_bytes)
        return self._signed('PUT', tenant_id, key, expires, size=size, sha256=sha256, content_type=content_type)

    def presign_download(self, tenant_id, key, *, expires=300, filename=None):
        return self._signed('GET', tenant_id, key, expires)

    def verify_local_signature(self, method, tenant_id, key, expires, signature, *,
                               size=None, sha256=None, content_type=None):
        path = self._path(tenant_id, key)
        if isinstance(expires, bool) or not isinstance(expires, int) or not self.clock() < expires <= self.clock() + 900:
            raise StorageError('Object authorization has expired')
        if method == 'PUT':
            _upload_constraints(size, sha256, content_type, max_bytes=self.max_put_bytes)
        elif method != 'GET' or any(value is not None for value in (size, sha256, content_type)):
            raise StorageError('Invalid object authorization')
        expected = self._signature(method, tenant_id, key, expires, size, sha256, content_type)
        if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
            raise StorageError('Invalid object authorization')
        return path

    def head(self, tenant_id, key):
        path = self._path(tenant_id, key)
        if not path.is_file():
            raise ObjectNotFound('Object not found')
        with path.open('rb') as source:
            checksum = hashlib.file_digest(source, 'sha256').hexdigest()
        return {'bytes': path.stat().st_size, 'size': path.stat().st_size, 'sha256': checksum,
                'etag': checksum, 'object_key': key,
                'content_type': 'video/x-flv' if path.suffix == '.flv' else 'video/mp4'}

    def verify_upload(self, tenant_id, key, *, size, sha256, content_type='video/mp4'):
        _upload_constraints(size, sha256, content_type, max_bytes=self.max_put_bytes)
        item = self.head(tenant_id, key)
        if item['bytes'] != size or not hmac.compare_digest(item['sha256'], sha256) or item['content_type'] != content_type:
            raise IntegrityError('Uploaded object size, checksum or type does not match')
        return item

    def put_bytes(self, tenant_id, key, data, *, size, sha256, content_type='video/mp4'):
        _upload_constraints(size, sha256, content_type, max_bytes=self.max_put_bytes)
        if len(data) != size or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), sha256):
            raise IntegrityError('Uploaded object size or checksum does not match')
        path = self._path(tenant_id, key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            raise StorageError('Object already exists') from None
        try:
            with os.fdopen(fd, 'wb') as destination:
                destination.write(data)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return self.verify_upload(tenant_id, key, size=size, sha256=sha256, content_type=content_type)

    async def put_stream(self, tenant_id, key, chunks: AsyncIterable[bytes], *, size, sha256,
                         content_type='video/mp4'):
        """Write a signed request incrementally; never buffer or reread the object.

        The next request chunk is pulled only after this chunk has been written.
        A rejected, disconnected or cancelled request removes its partial file.
        """
        _upload_constraints(size, sha256, content_type, max_bytes=self.max_put_bytes)
        path = self._path(tenant_id, key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            raise StorageError('Object already exists') from None
        actual = 0
        digest = hashlib.sha256()
        try:
            with os.fdopen(fd, 'wb') as destination:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise IntegrityError('Upload stream must contain bytes')
                    if actual + len(chunk) > size:
                        raise IntegrityError('Uploaded object is larger than expected')
                    actual += len(chunk)
                    # Bound each filesystem operation even if a caller supplies
                    # an unusually large chunk. memoryview adds no second copy.
                    view = memoryview(chunk)
                    for offset in range(0, len(view), 1024 * 1024):
                        part = view[offset:offset + 1024 * 1024]
                        digest.update(part)
                        destination.write(part)
                if actual != size or not hmac.compare_digest(digest.hexdigest(), sha256):
                    raise IntegrityError('Uploaded object size or checksum does not match')
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {'bytes': actual, 'size': actual, 'sha256': digest.hexdigest(),
                'etag': digest.hexdigest(), 'object_key': key, 'content_type': content_type}

    def upload(self, tenant_id, key, path, content_type='video/mp4'):
        source = Path(path)
        destination = self._path(tenant_id, key)
        with source.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        size = source.stat().st_size
        _upload_constraints(size, digest, content_type, max_bytes=self.max_put_bytes)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            raise StorageError('Object already exists') from None
        try:
            with os.fdopen(fd, 'wb') as output, source.open('rb') as input_file:
                while chunk := input_file.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            return self.verify_upload(tenant_id, key, size=size, sha256=digest, content_type=content_type)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def download(self, tenant_id, key, path):
        import shutil
        source = self._path(tenant_id, key)
        metadata = self.head(tenant_id, key)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        with destination.open('rb') as stream:
            if not hmac.compare_digest(hashlib.file_digest(stream, 'sha256').hexdigest(), metadata['sha256']):
                destination.unlink(missing_ok=True)
                raise IntegrityError('Downloaded object checksum does not match')
        return metadata

    def delete(self, tenant_id, key):
        self._path(tenant_id, key).unlink(missing_ok=True)

    def health(self):
        try:
            with tempfile.TemporaryFile(dir=self.root) as stream:
                stream.write(b'replay-health')
                stream.flush()
        except OSError:
            raise StorageError('Private local storage is unavailable') from None
        return True
