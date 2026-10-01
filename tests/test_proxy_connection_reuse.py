"""Real HTTP framing/keep-alive with synthetic proxy failures; no paid traffic."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import sys
import time

import httpx
import pytest
from urllib.parse import urlsplit

from server.worker import run_job

from server import media_sources as sources
from test_commercial_api import commercial, run_claimed
from test_media_sources import sample_mp4

PROXY = 'http://synthetic__sessid.fixed:secret@gw.dataimpulse.com:823'
SOURCE = {'provider': 'youtube', 'url': 'https://youtu.be/SzrcusiORCI'}


@pytest.fixture
def origin(monkeypatch):
    lock = threading.Lock()
    state = {'connections': 0, 'open': 0, 'peak': 0, 'attempts': 0,
             'requests': Counter(), 'payloads': {}, 'partial': set(),
             'silent_close': set(), 'close': set(), 'connect_failures': 0,
             'failure_status': 502, 'hosts': []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            with lock:
                state['connections'] += 1
                state['open'] += 1
                state['peak'] = max(state['peak'], state['open'])

        def finish(self):
            try:
                super().finish()
            finally:
                with lock:
                    state['open'] -= 1

        def log_message(self, *_args):
            pass

        def do_GET(self):
            with lock:
                state['requests'][self.path] += 1
                partial = self.path in state['partial']
                state['partial'].discard(self.path)
                silent = self.path in state['silent_close']
                state['silent_close'].discard(self.path)
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/part-0')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path == '/denied':
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            payload = state['payloads'][self.path]
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            if self.path in state['close'] or partial:
                self.send_header('Connection', 'close')
                self.close_connection = True
            self.end_headers()
            self.wfile.write(payload[:17] if partial else payload)
            self.wfile.flush()
            if silent:
                self.close_connection = True

    errors = []
    class Server(ThreadingHTTPServer):
        def handle_error(self, *_args):
            # Budget rejection intentionally closes an unread response.
            if not isinstance(sys.exception(), (ConnectionResetError, BrokenPipeError)):
                errors.append(type(sys.exception()).__name__)
    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class Connection(http.client.HTTPConnection):
        def __init__(self, host, address, timeout, *, budget, proxy):
            super().__init__('127.0.0.1', server.server_port, timeout=timeout)
            self.budget = budget
            with lock:
                state['hosts'].append(host)

        def connect(self):
            with lock:
                state['attempts'] += 1
                fail = state['connect_failures'] > 0
                state['connect_failures'] -= int(fail)
            if fail:
                raise sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE',
                    reason='proxy_rejected', http_status=state['failure_status'])
            super().connect()
            self.sock = sources._BudgetedSocket(self.sock, self.budget)

    monkeypatch.setattr(sources, '_resolve', lambda *_: ['8.8.8.8'])
    monkeypatch.setattr(sources, '_ProxyHTTPSConnection', Connection)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        assert not errors


def tx(*, size=1024 * 1024, check=lambda: None):
    return sources._Transport(sources._Budget(size, 30, check), PROXY)


def fmt(count):
    return {'url': 'https://video.googlevideo.com/manifest', 'protocol': 'http_dash_segments',
            'fragments': [{'path': f'/part-{i}'} for i in range(count)]}


def assert_closed(state):
    end = time.monotonic() + 2
    while state['open'] and time.monotonic() < end:
        time.sleep(.01)
    assert state['open'] == 0


def test_four_connections_serve_many_ordered_segments(origin, tmp_path):
    origin['payloads'] = {f'/part-{i}': bytes([i]) * 100 for i in range(32)}
    transport = tx()
    output = tmp_path / 'media'
    sources._download_format(fmt(32), output, transport, 0)
    assert output.read_bytes() == b''.join(origin['payloads'].values())
    assert 1 <= origin['connections'] <= 4
    assert all(n == 1 for n in origin['requests'].values())
    assert transport.budget.requests == 32
    assert transport.budget.wire_bytes > transport.budget.media_bytes == 3200
    assert not transport._idle
    assert_closed(origin)


def test_partial_segment_retries_without_redownloading_completed_parts(origin, tmp_path, caplog):
    origin['payloads'] = {f'/part-{i}': bytes([i]) * 100 for i in range(24)}
    origin['partial'].add('/part-3')
    transport = tx()
    output = tmp_path / 'media'
    with caplog.at_level('INFO'):
        sources._download_format(fmt(24), output, transport, 0)
    assert output.read_bytes() == b''.join(origin['payloads'].values())
    assert origin['requests']['/part-3'] == 2
    assert all(n == 1 for p, n in origin['requests'].items() if p != '/part-3')
    assert transport.budget.requests == 25
    assert transport.budget.media_bytes == 2417
    assert transport.budget.request_retries == 1 and transport.budget.retries == 0
    assert 'source_request_retry' in caplog.text
    assert all(s not in caplog.text for s in ('secret', 'synthetic', 'googlevideo'))
    assert_closed(origin)


def test_stale_keepalive_is_discarded_and_only_current_request_retries(origin, tmp_path):
    origin['payloads'] = {'/first': b'a' * 30, '/second': b'b' * 30}
    origin['silent_close'].add('/first')
    transport = tx()
    try:
        with (tmp_path / 'media').open('wb') as file:
            transport.download('https://video.googlevideo.com/first', file, reuse=True)
            assert_closed(origin)
            transport.download('https://video.googlevideo.com/second', file, reuse=True)
        assert (tmp_path / 'media').read_bytes() == b'a' * 30 + b'b' * 30
        assert origin['requests'] == {'/first': 1, '/second': 1}
        assert transport.budget.requests == 3 and transport.budget.request_retries == 1
    finally:
        transport.close_idle()
    assert_closed(origin)


@pytest.mark.parametrize('path', ['/redirect', '/part-0'])
def test_redirect_and_connection_close_are_not_reused(origin, tmp_path, path):
    origin['payloads'] = {'/part-0': b'video'}
    origin['close'].add('/part-0')
    transport = tx()
    with (tmp_path / 'media').open('wb') as file:
        transport.download('https://video.googlevideo.com' + path, file, reuse=True)
    assert not transport._idle
    assert origin['connections'] == (2 if path == '/redirect' else 1)
    assert_closed(origin)


def test_pool_never_reuses_a_tunnel_for_another_hostname(origin, tmp_path):
    origin['payloads'] = {'/part-0': b'video'}
    transport = tx()
    try:
        with (tmp_path / 'media').open('wb') as file:
            for host in ('one.googlevideo.com', 'two.googlevideo.com'):
                transport.download(f'https://{host}/part-0', file, reuse=True)
        assert origin['hosts'] == ['one.googlevideo.com', 'two.googlevideo.com']
    finally:
        transport.close_idle()
    assert_closed(origin)


@pytest.mark.parametrize('status', [403, 407, 429])
def test_access_auth_and_quota_failures_do_not_retry(origin, tmp_path, status):
    origin.update(connect_failures=20, failure_status=status)
    transport = tx()
    with (tmp_path / 'media').open('wb') as file, pytest.raises(sources.SourceImportError):
        transport.download('https://video.googlevideo.com/part-0', file, reuse=True)
    assert origin['attempts'] == 1
    assert transport.budget.request_retries == 0 and not transport._idle


@pytest.mark.parametrize('operation', ['download', 'metadata'])
def test_persistent_502_has_bounded_request_retries(origin, tmp_path, operation):
    origin['connect_failures'] = 100
    transport = tx()
    with pytest.raises(sources.SourceImportError, match='SOURCE_PROXY_UNAVAILABLE'):
        if operation == 'download':
            with (tmp_path / 'media').open('wb') as file:
                transport.download('https://video.googlevideo.com/part-0', file)
        else:
            transport.metadata('https://video.googlevideo.com/part-0')
    assert origin['attempts'] == 3
    assert transport.budget.requests == 3 and transport.budget.request_retries == 2


def test_request_retry_cap_is_shared_across_requests_and_rotation(origin, tmp_path):
    origin['connect_failures'] = 100
    transport = tx()
    for _ in range(7):
        with (tmp_path / 'media').open('wb') as file, pytest.raises(sources.SourceImportError):
            transport.download('https://video.googlevideo.com/part-0', file)
    assert transport.budget.request_retries == sources._IMPORT_REQUEST_RETRIES == 8
    assert origin['attempts'] == transport.budget.requests == 7 + 8
    replacement = sources._Transport(transport.budget, PROXY, renew_session=True)
    with (tmp_path / 'media').open('wb') as file, pytest.raises(sources.SourceImportError):
        replacement.download('https://video.googlevideo.com/part-0', file)
    assert origin['attempts'] == 16 and replacement.budget.request_retries == 8


@pytest.mark.parametrize('cancel', [True, False])
def test_cancel_and_deadline_interrupt_retry_backoff(origin, tmp_path, monkeypatch, cancel):
    cancelled = False
    def check():
        if cancelled:
            raise RuntimeError('lease cancelled')
    transport = tx(check=check)
    def pause(_):
        nonlocal cancelled
        if cancel:
            cancelled = True
        else:
            transport.budget.deadline = 0
    monkeypatch.setattr(sources.time, 'sleep', pause)
    origin['connect_failures'] = 100
    with (tmp_path / 'media').open('wb') as file, pytest.raises(
            RuntimeError if cancel else sources.SourceImportError):
        transport.download('https://video.googlevideo.com/part-0', file)
    assert origin['attempts'] == 1


def test_retry_never_resets_partial_byte_budget(origin, tmp_path):
    origin['payloads'] = {'/part-0': b'a' * 100}
    origin['partial'].add('/part-0')
    transport = tx(size=100)
    with pytest.raises(sources.SourceImportError, match='SOURCE_TOO_LARGE'):
        sources._download_format(fmt(1), tmp_path / 'media', transport, 0)
    assert transport.budget.media_bytes == 17
    assert transport.budget.request_retries == 1
    assert_closed(origin)


@pytest.mark.parametrize('initial_failures,extractions,rotations', [(1, 1, 0), (3, 2, 1)])
def test_proxy_502_recovers_through_library_and_preview(
        commercial, origin, monkeypatch, sample_mp4, initial_failures, extractions, rotations):
    client, repo, _ = commercial
    origin['payloads'] = {'/part-0': sample_mp4}
    origin['connect_failures'] = initial_failures
    attempts = []
    def extract(source, transport):
        attempts.append(transport)
        return {'duration': 1, 'title': 'Recovered recording', 'formats': [{
            'url': 'https://video.googlevideo.com/part-0', 'protocol': 'https',
            'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'mp4a'}]}
    monkeypatch.setattr(sources, '_extract', extract)
    monkeypatch.setenv('REPLAY_SOURCE_PROXY_URL', PROXY)
    created = client.post('/api/media/imports', json=SOURCE,
        headers={'Idempotency-Key': 'bounded-request-recovery'})
    assert created.status_code == 202
    job = run_claimed(client)
    assert repo.get_job('alpha', job['id'])['state'] == 'completed'
    saved = client.get('/api/media').json()[0]
    preview = client.get('/api/media/' + saved['id'] + '/preview').json()
    payload = client.get(preview['url']).content
    assert payload[4:8] == b'ftyp' and len(payload) == saved['bytes']
    assert saved['name'] == 'Recovered recording'
    assert client.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []
    assert client.get('/api/usage').json()['storage_reserved_bytes'] == 0
    assert len(attempts) == extractions
    assert attempts[-1].budget.retries == rotations
    assert attempts[-1].budget.request_retries == (1 if initial_failures == 1 else 2)
    assert_closed(origin)


def test_request_retry_cap_is_atomic_across_workers():
    budget = sources._Budget(1024, 30, lambda: None)
    with ThreadPoolExecutor(max_workers=4) as pool:
        allowed = list(pool.map(lambda _: budget.retry_request(), range(100)))
    assert sum(allowed) == budget.request_retries == 8


def test_persistent_proxy_failure_keeps_retry_evidence_after_worker_cleanup(
        commercial, origin, monkeypatch, caplog):
    client, repo, _ = commercial
    origin['connect_failures'] = 100
    monkeypatch.setattr(sources, '_extract', lambda *_: {'duration': 1, 'formats': [{
        'url': 'https://video.googlevideo.com/part-0', 'protocol': 'https',
        'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'mp4a'}]})
    monkeypatch.setenv('REPLAY_SOURCE_PROXY_URL', PROXY)
    assert client.post('/api/media/imports', json=SOURCE,
        headers={'Idempotency-Key': 'persistent-proxy-failure'}).status_code == 202
    claim = client.post('/internal/claim',
        json={'worker_id': 'integration', 'version': 'integration-test'},
        headers={'Authorization': 'Bearer ' + 'c' * 32})
    assert claim.status_code == 200
    job = claim.json()['job']
    def route(request):
        url = urlsplit(str(request.url))
        response = client.request(request.method, url.path, content=request.read(),
                                  headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    with caplog.at_level('INFO'), httpx.Client(transport=httpx.MockTransport(route)) as http:
        result = run_job(job, client=http)
    assert result['state'] == 'failed' and result['error_code'] == 'SOURCE_PROXY_UNAVAILABLE'
    assert repo.get_job('alpha', job['id'])['state'] == 'failed'
    events = [json.loads(r.message) for r in caplog.records if r.name == 'replay.operations']
    failure = next(e for e in events if e['event'] == 'worker_finished')['source_failure']
    assert failure['request_retries'] == 6 and failure['retries'] == 2
    assert failure['http_status'] == 502 and origin['attempts'] == 9
    assert client.get('/api/usage').json()['storage_reserved_bytes'] == 0
    assert job['source'] == {} and job['callback_token'] == ''


@pytest.mark.parametrize('value', [-1, 9, True, '2'])
def test_callback_rejects_invalid_request_retry_evidence(value):
    from pydantic import ValidationError
    from server.production_app import Finish
    with pytest.raises(ValidationError):
        Finish(state='failed', error_code='SOURCE_PROXY_UNAVAILABLE', source_failure={
            'stage': 'download', 'reason': 'proxy_rejected', 'elapsed_ms': 1,
            'retries': 1, 'http_status': 502, 'request_retries': value})
