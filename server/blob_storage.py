"""Tenant-scoped private Vercel Blob access through the authenticated SDK bridge.

Only the bridge holds the Blob store credential. Grants are short-lived official
Blob URLs; neither control credentials nor bridge errors reach object clients.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .storage import IntegrityError, ObjectNotFound, StorageError, _Keys, _expiry, _upload_constraints


MAX_BLOB_PUT_BYTES = 128 * 1024**2
_CONTROL_TIMEOUT = httpx.Timeout(90, connect=5, pool=5)
_TRANSFER_TIMEOUT = httpx.Timeout(30, connect=5, pool=5)
_MAX_CONTROL_RESPONSE = 16 * 1024


def _https_url(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 16000
            or any(ord(char) < 33 or ord(char) > 126 for char in value) or '\\' in value
            or '#' in value or re.search(r'%(?![a-fA-F0-9]{2})', value)):
        raise StorageError('Invalid private storage URL')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or not re.fullmatch(r'[A-Za-z0-9.-]+(?::443)?', parsed.netloc)
                or parsed.hostname.endswith('.')):
            raise ValueError()
        return parsed
    except ValueError:
        raise StorageError('Invalid private storage URL') from None


def _opaque_etag(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or any(ord(char) < 33 or ord(char) > 126 or char in '\\"' for char in value)):
        raise IntegrityError('Stored object version marker is invalid')
    return value


def _decoded_path(value):
    try:
        return unquote(value, errors='strict')
    except (ValueError, UnicodeError):
        raise StorageError('Private storage authorization is invalid') from None


class VercelBlobStorage(_Keys):
    max_put_bytes = MAX_BLOB_PUT_BYTES

    def __init__(self, control_url, control_token, *, max_put_bytes=MAX_BLOB_PUT_BYTES,
                 client=None, prefix='replay'):
        super().__init__(prefix)
        parsed = _https_url(control_url)
        host = parsed.hostname
        if (parsed.path != '/api/blob-control' or parsed.query
                or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', host)
                or re.fullmatch(r'[0-9.]+', host) or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home'))):
            raise StorageError('Invalid private storage control endpoint')
        if (not isinstance(control_token, str) or not 32 <= len(control_token) <= 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in control_token)):
            raise StorageError('Private storage control authorization is required')
        if isinstance(max_put_bytes, bool) or not isinstance(max_put_bytes, int) or not 1 <= max_put_bytes <= MAX_BLOB_PUT_BYTES:
            raise StorageError('Invalid private storage upload limit')
        self.control_url = control_url
        self._control_token = control_token
        self.max_put_bytes = max_put_bytes
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=_CONTROL_TIMEOUT, follow_redirects=False, trust_env=False)
        # Match API/worker telemetry: transport debug output includes signed URLs.
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)

    def close(self):
        if self._owns_client:
            self.client.close()

    @contextmanager
    def _response(self, method, url, *, headers=None, content=None, payload=None, control=False):
        response = None
        try:
            request = self.client.build_request(method, url, headers=headers, content=content, json=payload,
                timeout=_CONTROL_TIMEOUT if control else _TRANSFER_TIMEOUT)
            # Caller-injected clients must not forward their default credentials
            # or cookies to signed object endpoints or override bridge auth.
            for name in ('authorization', 'cookie', 'proxy-authorization'):
                request.headers.pop(name, None)
            if control:
                request.headers['authorization'] = 'Bearer ' + self._control_token
            response = self.client.send(request, stream=True, follow_redirects=False, auth=None)
            yield response
        except StorageError:
            raise
        except Exception:
            raise StorageError('Private object storage request failed') from None
        finally:
            if response is not None:
                response.close()

    def _call(self, operation, tenant_id=None, key=None, **values):
        payload = {'operation': operation}
        if operation != 'health':
            payload.update(tenant_id=tenant_id, object_key=self._validate_key(tenant_id, key))
        payload.update(values)
        deadline = time.monotonic() + 90
        with self._response('POST', self.control_url, payload=payload,
                            headers={'Accept': 'application/json'}, control=True) as response:
            if response.status_code == 404:
                raise ObjectNotFound('Object not found')
            if response.status_code == 409:
                raise IntegrityError('Stored object does not match the requested content')
            if response.status_code != 200:
                raise StorageError('Private object storage is unavailable')
            content = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                if time.monotonic() > deadline or len(content) + len(chunk) > _MAX_CONTROL_RESPONSE:
                    raise StorageError('Private storage response exceeded its limit')
                content.extend(chunk)
            try:
                result = json.loads(content)
            except (ValueError, UnicodeError):
                raise StorageError('Private storage response is invalid') from None
            if not isinstance(result, dict):
                raise StorageError('Private storage response is invalid')
            return result

    def _signed(self, value, key, method, expires, *, size=None, content_type=None):
        if (not isinstance(value, dict) or value.get('method') != method
                or value.get('object_key', key) != key or not isinstance(value.get('headers'), dict)
                or isinstance(value.get('expires_in'), bool) or not isinstance(value.get('expires_in'), int)
                or not 1 <= value['expires_in'] <= expires):
            raise StorageError('Private storage authorization is invalid')
        parsed = _https_url(value.get('url'))
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, errors='strict', max_num_fields=30)
        except (ValueError, UnicodeError):
            raise StorageError('Private storage authorization is invalid') from None
        if any(len(items) != 1 for items in query.values()):
            raise StorageError('Private storage authorization is invalid')
        signed_fields = {name for name in query if name.startswith('vercel-blob-')}
        common = {'vercel-blob-delegation', 'vercel-blob-signature', 'vercel-blob-valid-until'}
        if (not {'vercel-blob-delegation', 'vercel-blob-signature'} <= signed_fields
                or any(not query[name][0] for name in signed_fields)):
            raise StorageError('Private storage authorization is invalid')
        headers = value['headers']
        if method == 'PUT':
            if (parsed.hostname != 'vercel.com' or parsed.path != '/api/blob/'
                    or query.get('pathname') != [key] or set(query) - signed_fields != {'pathname'}):
                raise StorageError('Private storage upload authorization is invalid')
            constraints = {'vercel-blob-maximum-size-in-bytes': str(size), 'vercel-blob-allowed-content-types': content_type,
                           'vercel-blob-add-random-suffix': 'false', 'vercel-blob-allow-overwrite': 'false'}
            if signed_fields - common - constraints.keys() or any(query.get(name) != [item] for name, item in constraints.items()):
                raise StorageError('Private storage upload constraints do not match')
            normalized = {name.lower(): item for name, item in headers.items() if isinstance(name, str)}
            if (len(normalized) != len(headers) or set(normalized) != {'content-type', 'content-length'}
                    or normalized.get('content-type') != content_type or normalized.get('content-length') != str(size)):
                raise StorageError('Private storage upload constraints do not match')
            clean_headers = {'Content-Type': content_type, 'Content-Length': str(size)}
        else:
            if (not re.fullmatch(r'[a-z0-9]+\.private\.blob\.vercel-storage\.com', parsed.hostname)
                    or _decoded_path(parsed.path) != '/' + key
                    or set(query) - signed_fields - {'cache'} or signed_fields - common or query.get('cache') != ['0'] or headers):
                raise StorageError('Private storage download authorization is invalid')
            clean_headers = {}
        return {'url': value['url'], 'method': method, 'headers': clean_headers,
                'expires_in': value['expires_in'], **({'object_key': key} if method == 'PUT' else {})}

    def presign_upload(self, tenant_id, key, *, size, sha256, expires=900, content_type='video/mp4'):
        key = self._validate_key(tenant_id, key)
        _upload_constraints(size, sha256, content_type, max_bytes=self.max_put_bytes)
        expires = _expiry(expires)
        result = self._call('upload', tenant_id, key, size=size, sha256=sha256,
                            expires=expires, content_type=content_type)
        return self._signed(result, key, 'PUT', expires, size=size, content_type=content_type)

    def presign_download(self, tenant_id, key, *, expires=300, filename=None):
        key = self._validate_key(tenant_id, key)
        expires = _expiry(expires)
        values = {'expires': expires}
        if filename is not None:
            values['filename'] = re.sub(r'[^a-zA-Z0-9._-]', '_', str(filename))[:180] or 'video.mp4'
        return self._signed(self._call('download', tenant_id, key, **values), key, 'GET', expires)

    def _metadata(self, tenant_id, key, value):
        key = self._validate_key(tenant_id, key)
        if (value.get('object_key') != key or isinstance(value.get('bytes'), bool)
                or not isinstance(value.get('bytes'), int) or not 1 <= value['bytes'] <= self.max_put_bytes
                or isinstance(value.get('size'), bool) or not isinstance(value.get('size'), int) or value.get('size') != value['bytes']
                or not isinstance(value.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', value['sha256'])
                or value.get('content_type') not in ('video/mp4', 'video/x-flv', 'application/octet-stream')
                or value.get('version_id') is not None):
            raise IntegrityError('Stored object metadata is invalid')
        return {'bytes': value['bytes'], 'size': value['bytes'], 'sha256': value['sha256'],
                'etag': _opaque_etag(value.get('etag')), 'content_type': value['content_type'],
                'version_id': None, 'object_key': key}

    def head(self, tenant_id, key):
        return self._metadata(tenant_id, key, self._call('head', tenant_id, key))

    def verify_upload(self, tenant_id, key, *, size, sha256, content_type='video/mp4'):
        self._validate_key(tenant_id, key)
        _upload_constraints(size, sha256, content_type, max_bytes=self.max_put_bytes)
        item = self._metadata(tenant_id, key, self._call('verify', tenant_id, key,
            size=size, sha256=sha256, content_type=content_type))
        if item['bytes'] != size or not hmac.compare_digest(item['sha256'], sha256) or item['content_type'] != content_type:
            raise IntegrityError('Uploaded object size, checksum or type does not match')
        return item

    def download(self, tenant_id, key, path):
        key = self._validate_key(tenant_id, key)
        expected = self.head(tenant_id, key)
        signed = self.presign_download(tenant_id, key)
        destination = Path(path)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        deadline = time.monotonic() + 180
        try:
            with self._response('GET', signed['url'], headers={'If-Match': '"' + expected['etag'] + '"',
                                                               'Accept-Encoding': 'identity'}) as response:
                if response.status_code == 404:
                    raise ObjectNotFound('Object not found')
                if response.status_code != 200:
                    raise StorageError('Could not download private object')
                if (response.headers.get('etag', '').strip('"') != expected['etag']
                        or response.headers.get('content-type', '').split(';')[0].strip() != expected['content_type']
                        or response.headers.get('content-encoding', 'identity') != 'identity'):
                    raise IntegrityError('Downloaded object version or type does not match')
                length = response.headers.get('content-length')
                if length is not None and length != str(expected['bytes']):
                    raise IntegrityError('Downloaded object size does not match')
                digest, actual = hashlib.sha256(), 0
                with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                    temporary = Path(output.name)
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        actual += len(chunk)
                        if actual > expected['bytes'] or time.monotonic() > deadline:
                            raise IntegrityError('Downloaded object exceeded its limit')
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if actual != expected['bytes'] or not hmac.compare_digest(digest.hexdigest(), expected['sha256']):
                    raise IntegrityError('Downloaded object checksum does not match')
                os.replace(temporary, destination)
                temporary = None
                return expected
        except StorageError:
            raise
        except Exception:
            raise StorageError('Could not download private object') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def upload(self, tenant_id, key, path, content_type='video/mp4'):
        key = self._validate_key(tenant_id, key)
        try:
            path = Path(path)
            size = path.stat().st_size
            if not 1 <= size <= self.max_put_bytes:
                raise StorageError('Object size is outside the single-upload limit')
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            signed = self.presign_upload(tenant_id, key, size=size, sha256=digest, content_type=content_type)
            deadline = time.monotonic() + 180
            def chunks(source):
                actual = 0
                while chunk := source.read(1024 * 1024):
                    actual += len(chunk)
                    if actual > size or time.monotonic() > deadline:
                        raise IntegrityError('Uploaded object exceeded its limit')
                    yield chunk
                if actual != size:
                    raise IntegrityError('Uploaded object size changed')
            with path.open('rb') as source, self._response('PUT', signed['url'],
                    headers=signed['headers'], content=chunks(source)) as response:
                if response.status_code not in (200, 201):
                    raise StorageError('Could not upload immutable private object')
            return self.verify_upload(tenant_id, key, size=size, sha256=digest, content_type=content_type)
        except StorageError:
            raise
        except Exception:
            raise StorageError('Could not upload immutable private object') from None

    def delete(self, tenant_id, key):
        result = self._call('delete', tenant_id, key)
        if result.get('deleted') is not True:
            raise StorageError('Could not delete private object')

    def health(self):
        result = self._call('health')
        if result.get('ready') is not True or result.get('access') != 'private':
            raise StorageError('Private object storage is unavailable')
        return True
