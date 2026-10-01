"""Provider failures stay distinct through CONNECT, retries and worker callbacks."""
import io
import json
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import ValidationError

from server import media_sources as sources
from server.production_app import Finish
from server.worker import run_job
from test_commercial_api import commercial


CASES = [
    (403, 'PORT_BLOCKED', 'SOURCE_PROXY_ACCESS_DENIED'),
    (403, 'SITE_PERMANENTLY_BLOCKED', 'SOURCE_PROXY_ACCESS_DENIED'),
    (403, 'HOST_BLOCKED', 'SOURCE_PROXY_ACCESS_DENIED'),
    (407, 'NO_USER', 'SOURCE_PROXY_CONFIGURATION'),
    (407, 'TRAFFIC_EXHAUSTED', 'SOURCE_PROXY_QUOTA_EXHAUSTED'),
    (407, 'THREADS_EXHAUSTED', 'SOURCE_PROXY_BUSY'),
    (407, 'USER_RATE_LIMIT_EXCEEDED', 'SOURCE_PROXY_QUOTA_EXHAUSTED'),
    (429, 'USER_RATE_LIMIT_EXCEEDED', 'SOURCE_PROXY_QUOTA_EXHAUSTED'),
    (407, 'PORT_NOT_ALLOWED', 'SOURCE_PROXY_CONFIGURATION'),
    (407, 'USER_BLOCKED', 'SOURCE_PROXY_CONFIGURATION'),
    (500, 'INTERNAL_SERVER_ERROR', 'SOURCE_PROXY_UNAVAILABLE'),
    (502, 'NO_HOST_CONNECTION', 'SOURCE_PROXY_CONNECT_FAILED'),
    (503, 'NO_RAY', 'SOURCE_PROXY_POOL_EMPTY'),
]


def rejecting_proxy(monkeypatch, status, reason):
    state = {'opened': 0, 'closed': 0}

    class Socket:
        def __init__(self, *_):
            state['opened'] += 1
            self.response = io.BytesIO(
                f'HTTP/1.1 {status} {reason}\r\nX-Secret: hidden-secret\r\n\r\n'.encode())
        def settimeout(self, _):
            pass
        def connect(self, _):
            pass
        def sendall(self, _):
            pass
        def recv(self, size):
            return self.response.read(size)
        def close(self):
            state['closed'] += 1

    monkeypatch.setattr(sources, '_resolve', lambda *_: ['8.8.8.8'])
    monkeypatch.setattr(sources.socket, 'socket', Socket)
    return state


@pytest.mark.parametrize(('status', 'label', 'code'), CASES)
def test_actual_connect_preserves_only_documented_reason(monkeypatch, status, label, code):
    state = rejecting_proxy(monkeypatch, status, label)
    connection = sources._ProxyHTTPSConnection('www.youtube.com', '8.8.8.8', 5,
        budget=sources._Budget(1024, 30, lambda: None), proxy='hidden-credential')
    with pytest.raises(sources.SourceImportError) as caught:
        connection.connect()
    assert (caught.value.code, caught.value.proxy_error, caught.value.http_status) == (code, label, status)
    assert str(caught.value) == code
    assert state == {'opened': 1, 'closed': 1}


@pytest.mark.parametrize('header', [
    b'HTTP/1.1 502 private-credential\r\n\r\n',
    b'HTTP/1.1 502 NO_HOST_CONNECTION https://private.example/?secret=hidden\r\n\r\n',
    b'HTTP/1.1 502 TRAFFIC_EXHAUSTED\r\n\r\n',
    b'HTTP/1.1 407 NO_HOST_CONNECTION\r\n\r\n',
    b'HTTP/1.1 502 Bad Gateway\r\nX-Error: NO_HOST_CONNECTION\r\n\r\n',
    b'HTTP/1.1 502 \xffprivate\r\n\r\n',
    b'malformed secret\r\n\r\n',
])
def test_unknown_mismatched_and_free_text_reasons_remain_generic(header):
    error = sources._proxy_rejection(header)
    assert error.code == 'SOURCE_PROXY_UNAVAILABLE' and error.proxy_error is None
    assert 'secret' not in str(error) and 'private' not in str(error)


@pytest.mark.parametrize(('status', 'label', 'code'), CASES)
def test_only_transient_provider_errors_retry_and_rotate(monkeypatch, tmp_path, caplog, status, label, code):
    state = rejecting_proxy(monkeypatch, status, label)
    monkeypatch.setattr(sources, '_extract', lambda source, tx: tx.metadata('https://www.youtube.com/watch?v=SzrcusiORCI'))
    with caplog.at_level('INFO'), pytest.raises(sources.SourceImportError) as caught:
        sources.download_source({'provider': 'youtube', 'url': 'https://youtu.be/SzrcusiORCI'},
            tmp_path / 'out.mp4', max_bytes=1024 * 1024, max_duration=0, timeout=30,
            check_active=lambda: None, proxy_url='http://synthetic:secret@gw.dataimpulse.com:823')
    failure = caught.value.diagnostics
    transient = label in {'NO_HOST_CONNECTION', 'NO_RAY'}
    assert caught.value.code == code and failure.proxy_error == label
    assert failure.retries == int(transient)
    assert failure.request_retries == (4 if transient else 0)
    assert state['opened'] == (6 if transient else 1)
    assert state['closed'] == state['opened']
    assert not list(tmp_path.iterdir())
    assert all(secret not in caplog.text for secret in ['hidden-secret', 'synthetic', 'SzrcusiORCI'])


@pytest.mark.parametrize('value', ['secret-message', 'https://private.example', True, 502])
def test_callback_rejects_free_text_proxy_reason(value):
    with pytest.raises(ValidationError):
        Finish(state='failed', error_code='SOURCE_PROXY_UNAVAILABLE', source_failure={
            'stage': 'metadata', 'reason': 'proxy_rejected', 'elapsed_ms': 10,
            'retries': 0, 'http_status': 407, 'proxy_error': value})


def test_safe_provider_reason_survives_worker_cleanup(commercial, monkeypatch, tmp_path, caplog):
    client, repo, _ = commercial
    rejecting_proxy(monkeypatch, 407, 'TRAFFIC_EXHAUSTED')
    monkeypatch.setattr(sources, '_extract', lambda source, tx: tx.metadata('https://www.youtube.com/watch?v=SzrcusiORCI'))
    monkeypatch.setenv('REPLAY_SOURCE_PROXY_URL', 'http://synthetic:secret@gw.dataimpulse.com:823')
    created = client.post('/api/media/imports', json={'provider': 'youtube',
        'url': 'https://youtu.be/SzrcusiORCI'}, headers={'Idempotency-Key': 'provider-reason-flow'})
    assert created.status_code == 202
    claim = client.post('/internal/claim', json={'worker_id': 'integration', 'version': 'integration-test'},
                        headers={'Authorization': 'Bearer ' + 'c' * 32})
    job = claim.json()['job']
    workdir = tmp_path / 'worker'
    workdir.mkdir()

    def transport(request):
        response = client.request(request.method, urlsplit(str(request.url)).path,
                                  content=request.read(), headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    with caplog.at_level('INFO'), httpx.Client(transport=httpx.MockTransport(transport)) as http:
        result = run_job(job, client=http, workdir=workdir)
    assert result['state'] == 'failed' and result['error_code'] == 'SOURCE_PROXY_QUOTA_EXHAUSTED'
    item = client.get('/api/media').json()[0]
    assert item['status'] == 'failed' and item['error_code'] == 'SOURCE_PROXY_QUOTA_EXHAUSTED'
    records = [json.loads(r.message) for r in caplog.records if r.name == 'replay.operations']
    finish = next(r for r in records if r['event'] == 'worker_finished')
    assert finish['source_failure']['proxy_error'] == 'TRAFFIC_EXHAUSTED'
    assert finish['source_failure']['request_retries'] == 0
    assert repo.usage('alpha')['storage_reserved_bytes'] == 0
    assert job['source'] == {} and job['callback_token'] == ''
    assert not list(workdir.iterdir())
    assert 'hidden-secret' not in caplog.text and 'synthetic' not in caplog.text
