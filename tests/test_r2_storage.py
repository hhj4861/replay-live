import hashlib
import json
from urllib.parse import urlsplit

import httpx
import pytest

from server.r2_storage import R2Storage
from server.storage import StorageError
from test_vercel_free_runtime import settings


CONTROL = 'https://api.synthetic.workers.dev/api/blob-control'
TOKEN = 'synthetic-r2-control-' * 3


def test_r2_production_configuration_and_output_limit():
    cfg = settings(storage_provider='cloudflare-r2', blob_control_url=CONTROL,
                   dispatch_mode='cloudflare', max_output_bytes=64 * 1024**2)
    assert cfg.storage_provider == 'cloudflare-r2'
    with pytest.raises(ValueError):
        settings(storage_provider='cloudflare-r2', blob_control_url=CONTROL, max_output_bytes=64 * 1024**2 + 1)


def test_r2_grants_pin_origin_path_method_headers_and_expiry():
    storage = R2Storage(CONTROL, TOKEN)
    key = storage.key('tenant', 'video')
    descriptor = {'method': 'PUT', 'object_key': key, 'expires_in': 300,
        'headers': {'Content-Type': 'video/mp4', 'Content-Length': '3'},
        'url': f'https://api.synthetic.workers.dev/objects/{key}?grant=payload.' + 'x' * 43}
    def check(value):
        return storage._signed(value, key, 'PUT', 300, size=3, content_type='video/mp4')
    assert check(descriptor)['url'] == descriptor['url']
    for changed in [
        {'url': descriptor['url'].replace('api.synthetic.workers.dev', 'evil.example')},
        {'url': descriptor['url'].replace('video.mp4', 'other.mp4')},
        {'url': descriptor['url'] + '&grant=second'},
        {'url': descriptor['url'] + '&token=secret'}, {'method': 'GET'},
        {'expires_in': 301}, {'expires_in': True}, {'headers': {'Authorization': TOKEN}},
    ]:
        with pytest.raises(StorageError): check(descriptor | changed)
    storage.close()


def test_repeated_health_polling_shares_one_probe_and_failed_probe_is_not_cached():
    clock = [100.0]
    calls = []
    status = [200]
    def handler(request):
        assert str(request.url) == CONTROL
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        calls.append(request)
        return httpx.Response(status[0], json={'ready': True, 'access': 'private'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        storage = R2Storage(CONTROL, TOKEN, client=http, clock=lambda: clock[0])
        for _ in range(100): assert storage.health()
        assert len(calls) == 1
        clock[0] += 61
        status[0] = 503
        for _ in range(2):
            with pytest.raises(StorageError): storage.health()
        assert len(calls) == 3
        status[0] = 200
        assert storage.health()


def test_upload_and_verification_use_worker_capability_without_control_token():
    data = b'media'
    digest = hashlib.sha256(data).hexdigest()
    key = R2Storage(CONTROL, TOKEN).key('tenant', 'video')
    def handler(request):
        if request.url.path == '/api/blob-control':
            value = json.loads(request.content)
            assert request.headers['authorization'] == 'Bearer ' + TOKEN
            if value['operation'] == 'upload':
                return httpx.Response(200, json={'url': f'https://{urlsplit(CONTROL).netloc}/objects/{key}?grant=payload.' + 'a' * 43,
                    'method': 'PUT', 'headers': {'Content-Type': 'video/mp4', 'Content-Length': str(len(data))},
                    'object_key': key, 'expires_in': 300})
            return httpx.Response(200, json={'bytes': len(data), 'size': len(data), 'sha256': digest,
                'content_type': 'video/mp4', 'etag': 'immutable-version', 'version_id': None, 'object_key': key})
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        assert request.content == data
        return httpx.Response(200)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        storage = R2Storage(CONTROL, TOKEN, client=http)
        signed = storage.presign_upload('tenant', key, size=len(data), sha256=digest, expires=300)
        with storage._response('PUT', signed['url'], headers=signed['headers'], content=data) as reply:
            assert reply.status_code == 200
        assert storage.verify_upload('tenant', key, size=len(data), sha256=digest)['sha256'] == digest
