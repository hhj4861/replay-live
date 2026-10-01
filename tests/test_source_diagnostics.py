"""Failure evidence survives worker cleanup without exposing source credentials."""
import http.client
import io
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import ValidationError

from server import media_sources as sources
from server.production_app import Finish
from server.worker import run_job
from test_commercial_api import commercial
from test_media_sources import Response, network


@pytest.mark.parametrize(('error', 'reason'), [
    (TimeoutError('https://secret.example/?token=hidden'), 'network_timeout'),
    (ssl.SSLError('private certificate details'), 'tls_error'),
    (ConnectionResetError('private address'), 'connection_reset'),
    (http.client.RemoteDisconnected('private response'), 'connection_reset'),
    (http.client.BadStatusLine('private response'), 'connection_error'),
])
def test_transport_failure_has_safe_reason_and_bounded_retry(monkeypatch, tmp_path, caplog, error, reason):
    network(monkeypatch, [])
    def fail(_self):
        raise error
    monkeypatch.setattr(sources._PinnedHTTPSConnection, 'getresponse', fail)
    with caplog.at_level('INFO'), pytest.raises(sources.SourceImportError) as caught:
        sources.download_source({'provider': 'direct', 'url': 'https://media.example/file?token=hidden'},
            tmp_path / 'out.mp4', max_bytes=1024, max_duration=0, timeout=30, check_active=lambda: None)
    failure = caught.value.diagnostics
    assert caught.value.code == 'SOURCE_UNAVAILABLE'
    assert failure.stage == 'download' and failure.reason == reason and failure.retries == 1
    assert failure.http_status is None
    assert not (tmp_path / 'out.mp4').exists()
    assert str(error) not in caplog.text and 'hidden' not in caplog.text


def test_origin_http_status_is_recorded_without_body_or_url(monkeypatch, tmp_path):
    network(monkeypatch, [Response(b'secret upstream response', status=503), Response(status=503)])
    with pytest.raises(sources.SourceImportError) as caught:
        sources.download_source({'provider': 'direct', 'url': 'https://media.example/file'},
            tmp_path / 'out.mp4', max_bytes=1024, max_duration=0, timeout=30, check_active=lambda: None)
    assert caught.value.diagnostics.http_status == 503
    assert caught.value.diagnostics.reason == 'http_error'
    assert caught.value.diagnostics.retries == 1


@pytest.mark.parametrize('status', [407, 429, 502, 503])
def test_proxy_rejection_preserves_status_without_credentials(monkeypatch, status):
    class Socket:
        closed = False
        def __init__(self):
            self.response = io.BytesIO(f'HTTP/1.1 {status} Private reason\r\nX-Secret: hidden\r\n\r\n'.encode())
        def settimeout(self, _timeout):
            pass
        def connect(self, _address):
            pass
        def sendall(self, _data):
            pass
        def recv(self, size):
            return self.response.read(size)
        def close(self):
            self.closed = True
    sock = Socket()
    monkeypatch.setattr(sources, '_resolve', lambda *_: ['8.8.8.8'])
    monkeypatch.setattr(sources.socket, 'socket', lambda *_: sock)
    connection = sources._ProxyHTTPSConnection('www.youtube.com', '8.8.8.8', 5,
        budget=sources._Budget(1024, 30, lambda: None), proxy='secret-credential')
    with pytest.raises(sources.SourceImportError) as caught:
        connection.connect()
    assert caught.value.code == 'SOURCE_PROXY_UNAVAILABLE'
    assert caught.value.reason == 'proxy_rejected' and caught.value.http_status == status
    assert str(caught.value) == 'SOURCE_PROXY_UNAVAILABLE'
    assert sock.closed


def test_unexpected_extractor_failure_is_logged_safely(monkeypatch, tmp_path, caplog):
    def fail(*_args):
        raise RuntimeError('http://username:secret@gw.dataimpulse.com:823')
    monkeypatch.setattr(sources, '_extract', fail)
    with caplog.at_level('INFO'), pytest.raises(sources.SourceImportError) as caught:
        sources.download_source({'provider': 'youtube', 'url': 'https://youtu.be/3G579tV_YB8'},
            tmp_path / 'out.mp4', max_bytes=1024, max_duration=0, timeout=30, check_active=lambda: None)
    assert caught.value.diagnostics.stage == 'metadata'
    assert caught.value.diagnostics.reason == 'unexpected_error'
    assert 'source_import_failed' in caplog.text and 'username' not in caplog.text
    assert 'secret' not in caplog.text and 'dataimpulse' not in caplog.text


@pytest.mark.parametrize('override', [
    {'reason': 'http://username:password@example.com'}, {'stage': 'private-path'},
    {'http_status': True}, {'http_status': 600}, {'http_status': '503'},
    {'elapsed_ms': -1}, {'elapsed_ms': 14_400_001}, {'retries': 3},
    {'url': 'https://private.example'}, {'message': 'secret'},
])
def test_callback_rejects_unbounded_or_free_text_diagnostics(override):
    details = dict(stage='download', reason='network_timeout', elapsed_ms=5000, retries=1)
    details.update(override)
    with pytest.raises(ValidationError):
        Finish(state='failed', error_code='SOURCE_UNAVAILABLE', source_failure=details)


def test_import_failure_reaches_control_plane_log_after_worker_cleanup(commercial, monkeypatch, tmp_path, caplog):
    client, repo, _ = commercial
    network(monkeypatch, [])
    def fail(_self):
        raise TimeoutError('secret-proxy-credential')
    monkeypatch.setattr(sources._PinnedHTTPSConnection, 'getresponse', fail)
    created = client.post('/api/media/imports', json={'provider': 'direct',
        'url': 'https://media.example/own.mp4?token=secret-source'},
        headers={'Idempotency-Key': 'diagnostic-flow'})
    assert created.status_code == 202
    claim = client.post('/internal/claim', json={'worker_id': 'integration', 'version': 'integration-test'},
                        headers={'Authorization': 'Bearer ' + 'c' * 32})
    assert claim.status_code == 200
    job = claim.json()['job']
    (tmp_path / 'worker').mkdir()
    def transport(request):
        url = urlsplit(str(request.url))
        response = client.request(request.method, url.path, content=request.read(), headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    with caplog.at_level('INFO'), httpx.Client(transport=httpx.MockTransport(transport)) as http:
        result = run_job(job, client=http, workdir=tmp_path / 'worker')
    assert result['state'] == 'failed' and result['error_code'] == 'SOURCE_UNAVAILABLE'
    records = [json.loads(r.message) for r in caplog.records if r.name == 'replay.operations']
    finished = next(r for r in records if r['event'] == 'worker_finished')
    assert finished['job_id'] == job['id']
    assert finished['source_failure']['stage'] == 'download'
    assert finished['source_failure']['reason'] == 'network_timeout'
    assert finished['source_failure']['retries'] == 1
    assert repo.get_job('alpha', job['id'])['state'] == 'failed'
    assert job['source'] == {} and job['callback_token'] == ''
    assert 'secret-proxy' not in caplog.text and 'secret-source' not in caplog.text
    assert not list((tmp_path / 'worker').iterdir())


def test_legacy_callback_without_diagnostics_remains_valid():
    assert Finish(state='failed', error_code='SOURCE_UNAVAILABLE').source_failure is None


@pytest.mark.parametrize(('script_name', 'variable', 'module'), [
    ('build-worker-snapshot.mjs', 'files', 'server.worker'),
    ('youtube-server-probe.mjs', 'sources', 'server.media_sources'),
])
def test_packaged_file_allowlist_contains_required_imports(tmp_path, script_name, variable, module):
    root = Path(__file__).resolve().parents[1]
    script = (root / 'scripts' / script_name).read_text()
    files = re.findall(r"'([^']+)'", re.search(r'const ' + variable + r' = \[(.*?)\];', script).group(1))
    for name in files:
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, destination)
    result = subprocess.run([sys.executable, '-I', '-c',
        'import sys,importlib; sys.path.insert(0, sys.argv[1]); module=importlib.import_module(sys.argv[2]); '
        'assert module.__file__.startswith(sys.argv[1])', str(tmp_path), module],
        cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
