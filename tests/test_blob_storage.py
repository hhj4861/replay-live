"""Private Blob adapter contract, using synthetic HTTP transports only."""
import hashlib
import json
import logging
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from server.blob_storage import MAX_BLOB_PUT_BYTES, VercelBlobStorage
from server.storage import IntegrityError, ObjectNotFound, StorageError


CONTROL = 'https://studio.example.com/api/blob-control'
TOKEN = 'synthetic-control-token-' + 'x' * 32
DATA = b'synthetic-media-content' * 10
SHA = hashlib.sha256(DATA).hexdigest()


class Bridge:
    def __init__(self):
        self.calls = []
        self.body = DATA
        self.content_type = 'video/mp4'
        self.etag = 'opaque-version-2'
        self.deleted = False
        self.override = None
        self.status = 200
        self.download_body = None
        self.download_headers = {}

    def metadata(self, key):
        return {'bytes': len(self.body), 'size': len(self.body), 'sha256': hashlib.sha256(self.body).hexdigest(),
                'etag': self.etag, 'content_type': self.content_type, 'version_id': None, 'object_key': key}

    def __call__(self, request):
        self.calls.append(request)
        if str(request.url) == CONTROL:
            assert request.headers['authorization'] == 'Bearer ' + TOKEN
            payload = json.loads(request.content)
            operation = payload['operation']
            if self.status != 200:
                return httpx.Response(self.status, json={'code': 'SYNTHETIC_FAILURE', 'detail': 'must-not-echo-private-grant'},
                    headers={'Location': 'https://evil.example/?signed-grant=synthetic'})
            if self.override:
                return self.override(payload)
            if operation == 'health':
                return httpx.Response(200, json={'ready': True, 'access': 'private'})
            key = payload['object_key']
            if operation == 'upload':
                url = 'https://vercel.com/api/blob/?' + urlencode({'pathname': key,
                    'vercel-blob-delegation': 'synthetic-delegation', 'vercel-blob-signature': 'synthetic-upload-grant',
                    'vercel-blob-maximum-size-in-bytes': str(payload['size']), 'vercel-blob-allowed-content-types': payload['content_type'],
                    'vercel-blob-add-random-suffix': 'false', 'vercel-blob-allow-overwrite': 'false'})
                return httpx.Response(200, json={'url': url, 'method': 'PUT', 'headers': {
                    'Content-Type': payload['content_type'], 'Content-Length': str(payload['size'])},
                    'expires_in': payload['expires'], 'object_key': key})
            if operation == 'download':
                return httpx.Response(200, json={'url': 'https://synthetic.private.blob.vercel-storage.com/' + key
                    + '?vercel-blob-delegation=synthetic-delegation&vercel-blob-signature=synthetic-download-grant&cache=0',
                    'method': 'GET', 'headers': {}, 'expires_in': payload['expires']})
            if operation in ('head', 'verify'):
                return httpx.Response(404) if self.deleted else httpx.Response(200, json=self.metadata(key))
            if operation == 'delete':
                self.deleted = True
                return httpx.Response(200, json={'deleted': True})
            raise AssertionError('Unexpected synthetic operation')
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        assert 'proxy-authorization' not in request.headers
        if request.url.host == 'vercel.com':
            assert request.method == 'PUT'
            self.body = request.content
            self.content_type = request.headers['content-type']
            return httpx.Response(200, json={'url': 'synthetic-result'})
        assert request.url.host == 'synthetic.private.blob.vercel-storage.com'
        assert request.method == 'GET'
        assert request.headers['if-match'] == '"' + self.etag + '"'
        return httpx.Response(200, content=self.download_body if self.download_body is not None else self.body,
            headers={'ETag': '"' + self.etag + '"', 'Content-Type': self.content_type, **self.download_headers})


@pytest.fixture
def adapter():
    bridge = Bridge()
    with httpx.Client(transport=httpx.MockTransport(bridge), follow_redirects=True,
                      headers={'Authorization': 'must-not-forward', 'Cookie': 'must-not-forward',
                               'Proxy-Authorization': 'must-not-forward'}, auth=('synthetic', 'must-not-forward')) as client:
        storage = VercelBlobStorage(CONTROL, TOKEN, client=client)
        yield storage, bridge, storage.key('alpha', 'recording')


@pytest.mark.parametrize('url', ['http://studio.example.com/api/blob-control', 'https://user:pass@studio.example.com/api/blob-control',
    'https://@studio.example.com/api/blob-control', 'https://localhost/api/blob-control', 'https://127.0.0.1/api/blob-control',
    'https://example.internal/api/blob-control', 'https://studio.example.com:8443/api/blob-control',
    'https://studio.example.com/api/other', 'https://studio.example.com/api/blob-control?secret=synthetic',
    'https://studio.example.com/api/blob-control#', 'https://studio.example.com/api/blob-control\n',
    'https://studio.example.com\\evil/api/blob-control'])
def test_control_endpoint_is_fixed_https_without_credentials_or_private_hosts(url):
    with pytest.raises(StorageError):
        VercelBlobStorage(url, TOKEN)


@pytest.mark.parametrize('token', ['', 'short', 'x' * 31, 'x' * 32 + '\r\nAuthorization: synthetic', None])
def test_control_credentials_fail_without_echoing_values(token):
    with pytest.raises(StorageError) as caught:
        VercelBlobStorage(CONTROL, token)
    assert 'Authorization:' not in str(caught.value)


@pytest.mark.parametrize('limit', [0, True, -1, 1.0, MAX_BLOB_PUT_BYTES + 1])
def test_upload_ceiling_cannot_exceed_the_blob_deployment_budget(limit):
    with pytest.raises(StorageError):
        VercelBlobStorage(CONTROL, TOKEN, max_put_bytes=limit)


def test_every_key_operation_checks_tenant_before_network_or_filesystem(adapter, tmp_path):
    storage, bridge, key = adapter
    operations = [lambda: storage.head('other', key), lambda: storage.verify_upload('other', key, size=len(DATA), sha256=SHA),
        lambda: storage.presign_upload('other', key, size=len(DATA), sha256=SHA), lambda: storage.presign_download('other', key),
        lambda: storage.delete('other', key), lambda: storage.download('other', key, tmp_path / 'output'),
        lambda: storage.upload('other', key, tmp_path / 'missing')]
    for operation in operations:
        with pytest.raises(StorageError):
            operation()
    assert bridge.calls == [] and list(tmp_path.iterdir()) == []


def test_upload_grant_binds_size_checksum_type_and_expiry(adapter):
    storage, bridge, key = adapter
    signed = storage.presign_upload('alpha', key, size=len(DATA), sha256=SHA, expires=120)
    assert signed['method'] == 'PUT' and signed['object_key'] == key and signed['expires_in'] == 120
    assert signed['headers'] == {'Content-Type': 'video/mp4', 'Content-Length': str(len(DATA))}
    assert parse_qs(httpx.URL(signed['url']).query.decode())['pathname'] == [key]
    payload = json.loads(bridge.calls[-1].content)
    assert payload == {'operation': 'upload', 'tenant_id': 'alpha', 'object_key': key,
                       'size': len(DATA), 'sha256': SHA, 'content_type': 'video/mp4', 'expires': 120}
    assert TOKEN not in json.dumps(signed)
    assert bridge.calls[-1].extensions['timeout']['read'] == 90


@pytest.mark.parametrize('change', [{'size': True}, {'size': 0}, {'size': MAX_BLOB_PUT_BYTES + 1}, {'sha256': 'invalid'},
    {'sha256': 'A' * 64}, {'content_type': 'text/html'}, {'expires': 0}, {'expires': 901}, {'expires': True}])
def test_invalid_upload_constraints_never_issue_grants(adapter, change):
    storage, bridge, key = adapter
    with pytest.raises(StorageError):
        storage.presign_upload('alpha', key, **{'size': len(DATA), 'sha256': SHA, **change})
    assert bridge.calls == []


def test_download_grant_keeps_private_host_and_never_returns_credentials(adapter):
    storage, bridge, key = adapter
    signed = storage.presign_download('alpha', key, filename='a"\r\nSet-Cookie: bad')
    assert signed['method'] == 'GET' and signed['headers'] == {} and signed['expires_in'] == 300
    assert httpx.URL(signed['url']).host == 'synthetic.private.blob.vercel-storage.com'
    assert TOKEN not in json.dumps(signed)
    assert json.loads(bridge.calls[-1].content)['filename'] == 'a___Set-Cookie__bad'


@pytest.mark.parametrize('change', [{'url': 'https://evil.example/path?vercel-blob-signature=private'},
    {'url': 'https://vercel.com.evil.example/api/blob/?pathname=other&vercel-blob-signature=private'},
    {'url': 'https://vercel.com/api/blob/?pathname=other&vercel-blob-signature=private'},
    {'method': 'POST'}, {'headers': {'Authorization': 'must-not-forward'}}, {'expires_in': 901},
    {'object_key': 'replay/other/media/id.mp4'}, {'headers': {'Content-Type': 'video/mp4', 'Content-Length': '999'}},
    {'headers': {'Content-Type': 'video/mp4', 'content-type': 'video/mp4', 'Content-Length': str(len(DATA))}}])
def test_untrusted_upload_grant_host_key_headers_and_lifetime_fail_closed(adapter, change):
    storage, bridge, key = adapter
    signed = storage.presign_upload('alpha', key, size=len(DATA), sha256=SHA)
    bridge.override = lambda payload: httpx.Response(200, json={**signed, **change})
    with pytest.raises(StorageError):
        storage.presign_upload('alpha', key, size=len(DATA), sha256=SHA)


@pytest.mark.parametrize('name,value', [('vercel-blob-allow-overwrite', 'true'), ('vercel-blob-add-random-suffix', 'true'),
    ('vercel-blob-maximum-size-in-bytes', str(len(DATA) + 1)), ('vercel-blob-allowed-content-types', 'text/html'),
    ('vercel-blob-callback-url', 'https://evil.example'), ('vercel-blob-delegation', '')])
def test_signed_upload_policy_cannot_relax_binding_or_immutability(adapter, name, value):
    storage, bridge, key = adapter
    signed = storage.presign_upload('alpha', key, size=len(DATA), sha256=SHA)
    parsed = httpx.URL(signed['url'])
    query = parse_qs(parsed.query.decode())
    query[name] = [value]
    changed = str(parsed.copy_with(query=urlencode(query, doseq=True).encode()))
    bridge.override = lambda payload: httpx.Response(200, json={**signed, 'url': changed})
    with pytest.raises(StorageError):
        storage.presign_upload('alpha', key, size=len(DATA), sha256=SHA)


@pytest.mark.parametrize('change', [{'url': 'https://synthetic.public.blob.vercel-storage.com/path?vercel-blob-signature=private'},
    {'url': 'https://synthetic.private.blob.vercel-storage.com.evil.example/path?vercel-blob-signature=private'},
    {'url': 'https://synthetic.private.blob.vercel-storage.com/other?vercel-blob-signature=private'},
    {'url': 'https://synthetic.private.blob.vercel-storage.com/%FF?vercel-blob-signature=private'},
    {'headers': {'Authorization': 'must-not-forward'}}, {'method': 'PUT'}])
def test_private_download_grants_reject_foreign_paths_public_hosts_or_headers(adapter, change):
    storage, bridge, key = adapter
    signed = storage.presign_download('alpha', key)
    bridge.override = lambda payload: httpx.Response(200, json={**signed, **change})
    with pytest.raises(StorageError) as caught:
        storage.presign_download('alpha', key)
    assert 'signature=' not in str(caught.value)


def test_head_and_verify_only_return_validated_metadata(adapter):
    storage, bridge, key = adapter
    assert storage.head('alpha', key) == bridge.metadata(key)
    assert storage.verify_upload('alpha', key, size=len(DATA), sha256=SHA) == bridge.metadata(key)
    assert json.loads(bridge.calls[-1].content)['operation'] == 'verify'
    bridge.override = lambda payload: httpx.Response(200, json={**bridge.metadata(key), 'sha256': '0' * 64})
    with pytest.raises(IntegrityError):
        storage.verify_upload('alpha', key, size=len(DATA), sha256=SHA)


@pytest.mark.parametrize('change', [{'bytes': True}, {'bytes': MAX_BLOB_PUT_BYTES + 1}, {'size': float(len(DATA))},
    {'size': 1}, {'sha256': ''}, {'sha256': 'A' * 64}, {'etag': ''}, {'etag': 'bad\r\nHeader'},
    {'content_type': 'text/html'}, {'object_key': 'foreign'}, {'version_id': 'unexpected-version'}])
def test_malformed_or_wrong_owner_metadata_is_rejected(adapter, change):
    storage, bridge, key = adapter
    bridge.override = lambda payload: httpx.Response(200, json={**bridge.metadata(key), **change})
    with pytest.raises(IntegrityError):
        storage.head('alpha', key)


def test_download_is_bounded_verifies_bytes_hash_etag_and_atomically_preserves_existing_file(adapter, tmp_path):
    storage, bridge, key = adapter
    destination = tmp_path / 'video.mp4'
    assert storage.download('alpha', key, destination) == bridge.metadata(key)
    assert destination.read_bytes() == DATA and destination.stat().st_mode & 0o777 == 0o600
    for corrupted in [b'x' * len(DATA), DATA[:-1], DATA + b'x']:
        bridge.download_body = corrupted
        with pytest.raises(IntegrityError):
            storage.download('alpha', key, destination)
        assert destination.read_bytes() == DATA
        assert [path.name for path in tmp_path.iterdir()] == ['video.mp4']
    bridge.download_body = None
    bridge.download_headers = {'ETag': '"changed-version"'}
    with pytest.raises(IntegrityError):
        storage.download('alpha', key, destination)
    assert destination.read_bytes() == DATA


def test_local_file_upload_sends_only_bound_headers_then_checks_server_hash(adapter, tmp_path):
    storage, bridge, key = adapter
    source = tmp_path / 'output.flv'
    source.write_bytes(DATA)
    item = storage.upload('alpha', key, source, content_type='video/x-flv')
    assert item['bytes'] == len(DATA) and item['sha256'] == SHA and item['content_type'] == 'video/x-flv'
    put = next(request for request in bridge.calls if request.method == 'PUT')
    assert put.content == DATA and put.headers['content-length'] == str(len(DATA))
    assert json.loads(bridge.calls[-1].content)['operation'] == 'verify'


@pytest.mark.parametrize('status,exception', [(301, StorageError), (302, StorageError), (307, StorageError),
    (308, StorageError), (400, StorageError), (401, StorageError), (404, ObjectNotFound),
    (409, IntegrityError), (502, StorageError)])
def test_bridge_errors_are_generic_and_redirects_never_follow(adapter, status, exception, caplog):
    storage, bridge, key = adapter
    bridge.status = status
    with caplog.at_level(logging.INFO), pytest.raises(exception) as caught:
        storage.head('alpha', key)
    assert len(bridge.calls) == 1
    assert 'must-not-echo' not in str(caught.value) and TOKEN not in str(caught.value)
    assert TOKEN not in caplog.text and 'vercel-blob-signature' not in caplog.text


def test_transport_exceptions_malformed_json_and_oversized_responses_are_sanitized(adapter):
    storage, bridge, key = adapter
    def failed(payload):
        raise httpx.ConnectError('https://private-url?signature=must-not-echo')
    for handler in [failed, lambda payload: httpx.Response(200, content=b'invalid-secret-json'),
                    lambda payload: httpx.Response(200, json=[]), lambda payload: httpx.Response(200, content=b'x' * 17000)]:
        bridge.override = handler
        with pytest.raises(StorageError) as caught:
            storage.head('alpha', key)
        assert 'must-not-echo' not in str(caught.value) and 'invalid-secret' not in str(caught.value)


def test_delete_is_scoped_idempotent_and_health_requires_private_access(adapter):
    storage, bridge, key = adapter
    assert storage.health() is True
    assert json.loads(bridge.calls[-1].content) == {'operation': 'health'}
    storage.delete('alpha', key)
    assert json.loads(bridge.calls[-1].content) == {'operation': 'delete', 'tenant_id': 'alpha', 'object_key': key}
    with pytest.raises(ObjectNotFound):
        storage.head('alpha', key)
    storage.delete('alpha', key)
    bridge.override = lambda payload: httpx.Response(200, json={'ready': True, 'access': 'public'})
    with pytest.raises(StorageError):
        storage.health()


def test_close_only_closes_owned_http_client(adapter):
    storage, _, _ = adapter
    storage.close()
    assert not storage.client.is_closed
    owned = VercelBlobStorage(CONTROL, TOKEN)
    owned.close()
    assert owned.client.is_closed
