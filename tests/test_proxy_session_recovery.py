"""Recover one broken proxy session without renewing traffic/time budgets."""
import base64
import hashlib
import json

import pytest

from server import media_sources as sources
from test_commercial_api import commercial, run_claimed
from test_media_sources import Response, sample_mp4


PROXY = 'http://synthetic__cr.us;sessid.fixed;sessttl.30:secret@gw.dataimpulse.com:823'
SOURCE = {'provider': 'youtube', 'url': 'https://youtu.be/3G579tV_YB8'}


def setup_transport(monkeypatch, responses):
    # Exercise whole-import recovery after the short request retries have been
    # exhausted. The default request retry path has its own integration tests.
    monkeypatch.setattr(sources, '_REQUEST_RETRIES', 0)
    attempts, requests = [], []
    monkeypatch.setattr(sources, '_resolve', lambda *_: ['8.8.8.8'])

    class Connection:
        def __init__(self, host, address, timeout, *, budget, proxy):
            self.proxy = proxy

        def request(self, method, path, body=None, headers=None):
            requests.append((self.proxy, path))

        def getresponse(self):
            assert responses, 'unexpected additional paid request'
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        def close(self):
            pass

    monkeypatch.setattr(sources, '_ProxyHTTPSConnection', Connection)

    def extract(source, transport):
        attempts.append(transport)
        transport.budget.check()
        return {'duration': 1, 'title': 'My recording', 'formats': [{
            'url': f'https://video.googlevideo.com/media-{len(attempts)}',
            'protocol': 'https', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'mp4a'}]}

    monkeypatch.setattr(sources, '_extract', extract)
    return attempts, requests


def run(output, **kwargs):
    return sources.download_source(SOURCE, output, max_bytes=kwargs.pop('max_bytes', 1024 * 1024),
        max_duration=0, timeout=30, check_active=kwargs.pop('check_active', lambda: None),
        proxy_url=PROXY, **kwargs)


@pytest.mark.parametrize('failure', [
    sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502),
    sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=503),
    sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=504),
    TimeoutError('private proxy details'), ConnectionResetError('private proxy details'),
    Response(b'partial', headers={'Content-Length': '100'}),
])
def test_failed_connection_refreshes_metadata_and_uses_new_session(
        monkeypatch, tmp_path, sample_mp4, caplog, failure):
    attempts, requests = setup_transport(monkeypatch, [failure, Response(sample_mp4)])
    output = tmp_path / 'out.mp4'
    with caplog.at_level('INFO'):
        result = run(output)
    assert result['bytes'] > 0 and result['sha256'] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result['source_title'] == 'My recording'
    assert len(attempts) == len(requests) == 2
    assert attempts[0].proxy != attempts[1].proxy
    assert requests == [(attempts[0].proxy, '/media-1'), (attempts[1].proxy, '/media-2')]
    assert attempts[0].budget is attempts[1].budget
    assert attempts[1].budget.retries == 1
    assert list(tmp_path.iterdir()) == [output]
    assert 'source_proxy_rotated' in caplog.text
    assert all(value not in caplog.text for value in ('secret', 'synthetic', 'private proxy', 'googlevideo'))


def test_rotation_replaces_fixed_session_but_keeps_account_and_targeting():
    old = base64.b64decode(sources._proxy_authorization(PROXY)).decode()
    new = base64.b64decode(sources._proxy_authorization(PROXY, renew_session=True)).decode()
    assert old == 'synthetic__cr.us;sessid.fixed;sessttl.30:secret'
    assert new.startswith('synthetic__cr.us;sessttl.30;sessid.') and new.endswith(':secret')
    assert 'sessid.fixed' not in new and new.count('sessid.') == 1


@pytest.mark.parametrize('status', [403, 407, 429])
def test_auth_quota_or_denied_connection_is_not_retried(monkeypatch, tmp_path, status):
    failure = sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=status)
    attempts, requests = setup_transport(monkeypatch, [failure])
    with pytest.raises(sources.SourceImportError) as caught:
        run(tmp_path / 'out.mp4')
    assert caught.value is failure and caught.value.diagnostics.retries == 0
    assert len(attempts) == len(requests) == 1 and not list(tmp_path.iterdir())


def test_three_unavailable_sessions_stop_without_fourth_request(monkeypatch, tmp_path):
    failures = [sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502)
                for _ in range(3)]
    attempts, requests = setup_transport(monkeypatch, list(failures))
    with pytest.raises(sources.SourceImportError) as caught:
        run(tmp_path / 'out.mp4')
    assert caught.value is failures[2]
    assert caught.value.diagnostics.retries == 2 and caught.value.diagnostics.http_status == 502
    assert len(attempts) == len(requests) == 3 and not list(tmp_path.iterdir())


def test_partial_transfer_is_still_charged_to_original_byte_budget(monkeypatch, tmp_path, sample_mp4):
    partial = Response(sample_mp4[:100], headers={'Content-Length': str(len(sample_mp4))})
    attempts, _ = setup_transport(monkeypatch, [partial, Response(sample_mp4)])
    with pytest.raises(sources.SourceImportError, match='SOURCE_TOO_LARGE'):
        run(tmp_path / 'out.mp4', max_bytes=len(sample_mp4))
    assert attempts[0].budget is attempts[1].budget
    assert attempts[1].budget.media_bytes >= 100
    assert not list(tmp_path.iterdir())


def test_expired_deadline_prevents_new_session(monkeypatch, tmp_path):
    attempts, requests = setup_transport(monkeypatch, [])

    def download(_fmt, path, transport, _max_duration):
        transport.budget.deadline = 0
        raise sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502)

    monkeypatch.setattr(sources, '_download_format', download)
    with pytest.raises(sources.SourceImportError, match='SOURCE_TIMEOUT'):
        run(tmp_path / 'out.mp4')
    assert len(attempts) == 1 and not requests and not list(tmp_path.iterdir())


def test_cancelled_lease_prevents_new_session(monkeypatch, tmp_path):
    attempts, _ = setup_transport(monkeypatch, [])
    cancelled = False

    def check():
        if cancelled:
            raise RuntimeError('lease cancelled')

    def download(*_args):
        nonlocal cancelled
        cancelled = True
        raise sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502)

    monkeypatch.setattr(sources, '_download_format', download)
    with pytest.raises(RuntimeError, match='lease cancelled'):
        run(tmp_path / 'out.mp4', check_active=check)
    assert len(attempts) == 1 and not list(tmp_path.iterdir())


def test_latched_metadata_proxy_failure_can_recover(monkeypatch, tmp_path, sample_mp4):
    attempts, requests = setup_transport(monkeypatch, [Response(sample_mp4)])
    original = sources._extract

    def extract(source, transport):
        result = original(source, transport)
        if len(attempts) == 1:
            error = sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502)
            transport.budget.failure = error
            raise error
        return result

    monkeypatch.setattr(sources, '_extract', extract)
    assert run(tmp_path / 'out.mp4')['bytes'] > 0
    assert len(attempts) == 2 and len(requests) == 1
    assert attempts[1].budget.failure is None


def test_session_recovery_reaches_library_and_playback(commercial, monkeypatch, sample_mp4, caplog):
    client, repo, _ = commercial
    failure = sources.SourceImportError('SOURCE_PROXY_UNAVAILABLE', reason='proxy_rejected', http_status=502)
    attempts, _ = setup_transport(monkeypatch, [failure, Response(sample_mp4)])
    monkeypatch.setenv('REPLAY_SOURCE_PROXY_URL', PROXY)
    created = client.post('/api/media/imports', json=SOURCE,
                          headers={'Idempotency-Key': 'proxy-session-recovery'})
    assert created.status_code == 202
    media_id = created.json()['media']['id']
    with caplog.at_level('INFO'):
        job = run_claimed(client)
    assert repo.get_job('alpha', job['id'])['state'] == 'completed'
    saved = client.get('/api/media').json()[0]
    assert saved['id'] == media_id and saved['status'] == 'ready'
    assert saved['name'] == 'My recording' and saved['bytes'] > 0
    preview = client.get('/api/media/' + media_id + '/preview').json()
    payload = client.get(preview['url']).content
    assert payload[4:8] == b'ftyp' and len(payload) == saved['bytes']
    assert client.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []
    usage = client.get('/api/usage').json()
    assert usage['storage_bytes'] == len(payload) and usage['storage_reserved_bytes'] == 0
    events = [json.loads(r.message) for r in caplog.records if r.name == 'replay.operations']
    finished = next(r for r in events if r['event'] == 'worker_finished')
    assert finished['state'] == 'completed'
    assert len(attempts) == 2 and job['source'] == {} and job['callback_token'] == ''
