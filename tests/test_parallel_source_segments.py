"""Bounded concurrent transfers: ordered bytes, shared limits and real remux."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import time

import pytest

from server import media_sources as sources
from test_media_sources import Response, sample_mp4
from test_commercial_api import commercial, run_claimed

PROXY = 'http://synthetic__sessid.fixed:secret@gw.dataimpulse.com:823'


@pytest.fixture(autouse=True)
def exhausted_request_retries(monkeypatch):
    # Keep fault/barrier tests focused on sibling abort and session fallback.
    # Default request retries and real persistent sockets are tested separately.
    monkeypatch.setattr(sources, '_REQUEST_RETRIES', 0)


def network(monkeypatch, payloads, *, delay=None, failure=None, headers=None):
    lock = threading.Lock()
    state = {'active': 0, 'peak': 0, 'started': [], 'closed': 0, 'proxies': []}
    monkeypatch.setattr(sources, '_resolve', lambda *_: ['8.8.8.8'])

    class Connection:
        def __init__(self, host, address, timeout, *, budget, proxy=None):
            self.budget, self.proxy = budget, proxy
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
                state['proxies'].append(proxy)

        def request(self, method, path, body=None, headers=None):
            self.path = path
            with lock:
                state['started'].append(path)

        def getresponse(self):
            if delay:
                delay(self.path)
            if failure:
                error = failure(self.path, self.proxy)
                if error:
                    raise error
            return Response(payloads[self.path], headers=headers)

        def close(self):
            with lock:
                state['active'] -= 1
                state['closed'] += 1

    monkeypatch.setattr(sources, '_ProxyHTTPSConnection', Connection)
    monkeypatch.setattr(sources, '_PinnedHTTPSConnection', Connection)
    return state


def fmt(count):
    return {'url': 'https://video.googlevideo.com/manifest', 'protocol': 'http_dash_segments',
            'vcodec': 'avc1', 'acodec': 'mp4a', 'ext': 'mp4',
            'fragments': [{'path': f'/part-{i}'} for i in range(count)]}


def transport(size=1024 * 1024, *, proxy=PROXY, timeout=30, check=lambda: None):
    return sources._Transport(sources._Budget(size, timeout, check), proxy)


def test_parallel_segments_preserve_order_and_single_session(monkeypatch, tmp_path, caplog):
    payloads = {f'/part-{i}': bytes([i]) * 37 for i in range(32)}
    state = network(monkeypatch, payloads, delay=lambda p: time.sleep(.002 * (4 - int(p[6:]) % 4)))
    tx = transport()
    output = tmp_path / 'input.media'
    with caplog.at_level('INFO'):
        sources._download_format(fmt(32), output, tx, 0)
    assert output.read_bytes() == b''.join(payloads.values())
    assert 1 < state['peak'] <= 4 and state['active'] == 0
    assert state['closed'] == tx.budget.requests == 32
    assert len(set(state['proxies'])) == 1
    assert tx.budget.media_bytes == len(output.read_bytes()) and tx.budget.retries == 0
    assert list(tmp_path.iterdir()) == [output]
    event = next(json.loads(r.message) for r in caplog.records if 'source_transfer' in r.message)
    assert event['segments'] == 32 and event['concurrency'] == 4
    assert all(value not in caplog.text for value in ('secret', 'synthetic', 'googlevideo'))


def test_slow_first_segment_bounds_prefetch_and_temporary_files(monkeypatch, tmp_path):
    release, siblings_done = threading.Event(), threading.Event()
    count, lock = 0, threading.Lock()

    def delay(path):
        nonlocal count
        if path == '/part-0':
            assert release.wait(5)
        else:
            with lock:
                count += 1
                if count == 3:
                    siblings_done.set()

    state = network(monkeypatch, {f'/part-{i}': b'x' for i in range(40)}, delay=delay)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sources._download_format, fmt(40), tmp_path / 'input', transport(), 0)
        try:
            assert siblings_done.wait(3)
            assert len(state['started']) == 4
            assert len(list(tmp_path.glob('segments-*/*'))) <= 4
        finally:
            release.set()
        future.result(timeout=5)
    assert state['active'] == 0 and (tmp_path / 'input').read_bytes() == b'x' * 40


@pytest.mark.parametrize('cause', ['proxy', 'bytes', 'requests', 'wire', 'deadline', 'cancel'])
def test_failure_stops_siblings_before_return_and_keeps_limits(monkeypatch, tmp_path, cause):
    gate = threading.Barrier(4)
    cancelled = threading.Event()

    def check():
        if cancelled.is_set():
            raise RuntimeError('lease cancelled')

    tx = transport(size=10 if cause == 'bytes' else 1000, check=check)
    error = sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502)

    def delay(path):
        if cause == 'requests':
            time.sleep(.01)
            return
        gate.wait(timeout=3)
        if path == '/part-1':
            if cause == 'deadline':
                tx.budget.deadline = 0
            if cause == 'cancel':
                cancelled.set()
            if cause == 'wire':
                tx.budget.consume_wire(tx.budget.max_bytes + sources._METADATA_TOTAL + 4 * 1024 * 1024 + 1)
        else:
            time.sleep(.02)

    if cause == 'requests':
        tx.budget.requests = sources._MAX_REQUESTS - 1
    state = network(monkeypatch, {f'/part-{i}': b'a' * 20 for i in range(50)}, delay=delay,
                    failure=lambda p, _: error if cause == 'proxy' and p == '/part-1' else None,
                    headers={} if cause == 'bytes' else None)
    expected = {'proxy': 'SOURCE_PROXY_UNAVAILABLE', 'bytes': 'SOURCE_TOO_LARGE',
                'requests': 'SOURCE_TOO_COMPLEX', 'wire': 'SOURCE_TOO_LARGE',
                'deadline': 'SOURCE_TIMEOUT', 'cancel': 'lease cancelled'}[cause]
    with pytest.raises((sources.SourceImportError, RuntimeError), match=expected):
        sources._download_format(fmt(50), tmp_path / 'input', tx, 0)
    assert state['active'] == 0 and len(state['started']) <= 4
    assert not list(tmp_path.glob('segments-*')) and tx.budget._transfer_error is None
    assert tx.budget.retries == 0
    if cause == 'bytes':
        assert tx.budget.media_bytes > tx.budget.max_bytes
    if cause == 'wire':
        assert tx.budget.wire_bytes > tx.budget.max_bytes + sources._METADATA_TOTAL


def test_atomic_accounting_across_workers():
    b = sources._Budget(10_000_000, 30, lambda: None)

    def consume(_):
        for _ in range(1000):
            b.request()
            b.consume(3, metadata=False)
            b.consume(2, metadata=True)
            b.consume_wire(7)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(consume, range(4)))
    assert (b.requests, b.media_bytes, b.metadata_bytes, b.wire_bytes) == (4000, 12000, 8000, 28000)


def test_sibling_failure_still_counts_bytes_already_received():
    b = sources._Budget(1000, 30, lambda: None)
    error = sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE')

    class Raw:
        def readinto(self, buffer):
            buffer[:3] = b'abc'
            b.abort_transfer(error)
            return 3

        def close(self):
            pass

    class Socket:
        def settimeout(self, value):
            pass

    with sources._CheckedSocketReader(Raw(), Socket(), b) as reader:
        with pytest.raises(sources.SourceImportError):
            reader.readinto(bytearray(10))
    assert b.wire_bytes == 3


def test_non_proxy_segments_remain_serial(monkeypatch, tmp_path):
    state = network(monkeypatch, {f'/part-{i}': b'x' for i in range(8)})
    sources._download_format(fmt(8), tmp_path / 'input', transport(proxy=None), 0)
    assert state['peak'] == 1 and state['started'] == [f'/part-{i}' for i in range(8)]


def test_hls_map_is_first_even_when_it_finishes_last(monkeypatch, tmp_path):
    playlist = b'#EXTM3U\n#EXT-X-MAP:URI="/part-0"\n#EXTINF:1,\n/part-1\n#EXTINF:1,\n/part-2\n#EXT-X-ENDLIST\n'
    network(monkeypatch, {'/manifest': playlist, '/part-0': b'init', '/part-1': b'one', '/part-2': b'two'},
            delay=lambda p: time.sleep(.03 if p == '/part-0' else .001))
    value = {'url': 'https://video.googlevideo.com/manifest', 'protocol': 'm3u8_native'}
    assert sources._download_format(value, tmp_path / 'input', transport(), 10) == 2
    assert (tmp_path / 'input').read_bytes() == b'initonetwo'


def test_parallel_proxy_recovery_reaches_library(commercial, monkeypatch, sample_mp4):
    client, repo, _ = commercial
    chunks = [sample_mp4[i:i + 1024] for i in range(0, len(sample_mp4), 1024)]
    sessions = []

    def extract(source, tx):
        sessions.append(tx.proxy)
        return {'duration': 1, 'title': 'Parallel recording', 'formats': [fmt(len(chunks))]}

    monkeypatch.setattr(sources, '_extract', extract)
    monkeypatch.setenv('REPLAY_SOURCE_PROXY_URL', PROXY)
    failed = sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502)
    state = network(monkeypatch, {f'/part-{i}': chunk for i, chunk in enumerate(chunks)},
                    delay=lambda _: time.sleep(.005),
                    failure=lambda p, proxy: failed if proxy == sessions[0] and p == '/part-1' else None)
    created = client.post('/api/media/imports', json={'provider': 'youtube', 'url': 'https://youtu.be/fixture'},
                          headers={'Idempotency-Key': 'parallel-recovery'})
    assert created.status_code == 202
    job = run_claimed(client)
    assert repo.get_job('alpha', job['id'])['state'] == 'completed'
    media = client.get('/api/media').json()[0]
    assert media['status'] == 'ready' and media['name'] == 'Parallel recording'
    preview = client.get('/api/media/' + media['id'] + '/preview').json()
    content = client.get(preview['url']).content
    assert content[4:8] == b'ftyp' and len(content) == media['bytes']
    assert len(sessions) == 2 and sessions[0] != sessions[1]
    assert state['active'] == 0 and state['peak'] <= 4
    assert client.get('/api/usage').json()['storage_reserved_bytes'] == 0
    assert client.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []


@pytest.mark.parametrize('status,attempts', [(502, 3), (403, 1), (407, 1), (429, 1)])
def test_parallel_failures_keep_import_retry_policy(monkeypatch, tmp_path, status, attempts):
    budgets = []

    def extract(source, tx):
        budgets.append(tx.budget)
        return {'duration': 1, 'formats': [fmt(20)]}

    monkeypatch.setattr(sources, '_extract', extract)
    error = sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=status)
    state = network(monkeypatch, {}, delay=lambda _: time.sleep(.01), failure=lambda *_: error)
    with pytest.raises(sources.SourceImportError) as caught:
        sources.download_source({'provider': 'youtube', 'url': 'https://youtu.be/fixture'},
            tmp_path / 'out.mp4', max_bytes=1000, max_duration=0, timeout=10,
            check_active=lambda: None, proxy_url=PROXY)
    assert caught.value.http_status == status
    assert len(budgets) == attempts and all(b is budgets[0] for b in budgets)
    assert budgets[0].retries == attempts - 1
    assert len(state['started']) <= 4 * attempts and state['active'] == 0
    assert not list(tmp_path.iterdir())


def test_latency_benchmark_preserves_bytes_and_request_count(monkeypatch, tmp_path):
    payloads = {f'/part-{i}': bytes([i]) * 1024 for i in range(24)}
    results = []
    for proxy in (None, PROXY):
        state = network(monkeypatch, payloads, delay=lambda _: time.sleep(.025))
        tx = transport(proxy=proxy)
        path = tmp_path / ('parallel' if proxy else 'serial')
        start = time.monotonic()
        sources._download_format(fmt(len(payloads)), path, tx, 0)
        results.append({'seconds': time.monotonic() - start, 'requests': tx.budget.requests,
                        'bytes': tx.budget.media_bytes, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'peak': state['peak']})
    serial, parallel = results
    assert all(serial[k] == parallel[k] for k in ('requests', 'bytes', 'sha256'))
    assert serial['peak'] == 1 and parallel['peak'] == 4
    # Concurrency/order assertions are deterministic; don't make wall-clock
    # thresholds a flaky CI gate. Emit timings for the local comparison.
    print(json.dumps({'serial': serial, 'parallel': parallel,
                      'speedup': round(serial['seconds'] / parallel['seconds'], 2)}))
