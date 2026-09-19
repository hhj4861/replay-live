"""Public source imports: deterministic network doubles, real local MP4 remux."""
import hashlib
import io
from pathlib import Path
import socket
import subprocess

import pytest

from server import media_sources as sources


def budget(size=1024 * 1024, check=lambda: None):
    return sources._Budget(size, 30, check)


class Response:
    def __init__(self, payload=b'', status=200, headers=None):
        self.payload = io.BytesIO(payload)
        self.status = status
        self.headers = headers if headers is not None else {'Content-Length': str(len(payload))}

    def getheader(self, key, default=None):
        return next((v for k, v in self.headers.items() if k.lower() == key.lower()), default)

    def getheaders(self):
        return list(self.headers.items())

    def read1(self, size):
        return self.payload.read(size)

    def close(self):
        self.payload.close()


def network(monkeypatch, responses, answers=None):
    requests = []
    resolutions = []

    def getaddrinfo(host, port, family, socktype):
        resolutions.append((host, port, family, socktype))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
                for ip in (answers or {}).get(host, ['8.8.8.8'])]

    class Connection:
        def __init__(self, host, address, timeout, *, budget=None):
            self.host, self.address = host, address

        def request(self, method, path, body=None, headers=None):
            requests.append(dict(host=self.host, address=self.address, method=method, path=path,
                                 body=body, headers=headers))

        def getresponse(self):
            assert responses, 'Unexpected additional network request'
            return responses.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(sources.socket, 'getaddrinfo', getaddrinfo)
    monkeypatch.setattr(sources, '_PinnedHTTPSConnection', Connection)
    return requests, resolutions


def test_catalog_and_normalization_keep_source_independent_from_targets():
    assert {s['id'] for s in sources.source_platforms()} == {
        'youtube', 'twitch', 'facebook', 'instagram', 'tiktok', 'naver', 'chzzk', 'kick', 'direct'}
    assert sources.normalize_source('youtube', 'https://www.youtube.com:443/watch?v=BaW_jenozKc') == {
        'provider': 'youtube', 'url': 'https://www.youtube.com/watch?v=BaW_jenozKc'}
    assert sources.normalize_source('direct', 'https://media.example.com/path?signature=synthetic')['provider'] == 'direct'


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'data:video/mp4;base64,AAAA', 'http://media.example.com/a.mp4',
    'https://user:password@media.example.com/a.mp4', 'https://media.example.com:8443/a.mp4',
    'https://localhost/a.mp4', 'https://127.0.0.1/a.mp4', 'https://127.1/a.mp4',
    'https://2130706433/a.mp4', 'https://0x7f000001/a.mp4', 'https://0177.0.0.1/a.mp4',
    'https://169.254.169.254/a.mp4', 'https://100.100.100.200/a.mp4', 'https://[::1]/a.mp4',
    'https://10.0.0.1/a.mp4', 'https://224.0.0.1/a.mp4', 'https://metadata.google.internal/a.mp4',
    'https://server.local/a.mp4', 'https://media.example.com./a.mp4',
    'https://media.example.com/a.mp4#fragment', 'https://media.example.com/a%0d%0a.mp4',
    'https://media.example.com/a%qq.mp4', 'https://media.example.com\\@127.0.0.1/a.mp4',
    'https://media.example.com/' + 'a' * 4096,
])
def test_unsafe_source_forms_are_rejected_without_echoing_url(url):
    with pytest.raises(sources.SourceImportError) as error:
        sources.normalize_source('direct', url)
    assert url not in str(error.value)


@pytest.mark.parametrize(('provider', 'url'), [
    ('youtube', 'https://www.youtube.com/watch?v=BaW_jenozKc&list=PL123'),
    ('youtube', 'https://www.youtube.com/@channel/live'),
    ('youtube', 'https://youtube.com.evil.example/watch?v=BaW_jenozKc'),
    ('twitch', 'https://www.twitch.tv/channel'),
    ('instagram', 'https://www.instagram.com/channel/'),
    ('tiktok', 'https://www.tiktok.com/@user/live'),
    ('naver', 'https://tv.naver.com/l/123'),
    ('chzzk', 'https://chzzk.naver.com/live/abcd'),
    ('kick', 'https://kick.com/channel'),
])
def test_pages_must_match_selected_provider_and_a_recording(provider, url):
    with pytest.raises(sources.SourceImportError):
        sources.normalize_source(provider, url)


def test_dns_mixed_private_answer_blocks_connection(monkeypatch):
    requests, resolutions = network(monkeypatch, [], {'media.example.com': ['8.8.8.8', '10.0.0.1']})
    with pytest.raises(sources.SourceImportError, match='SOURCE_URL_UNSAFE'):
        sources._Transport(budget()).metadata('https://media.example.com/recording')
    assert not requests and resolutions[0][2] == socket.AF_INET


@pytest.mark.parametrize('location', ['https://127.0.0.1/secret', 'http://public.example.com/a.mp4',
                                     'file:///etc/passwd', 'https://private.example.com/a.mp4'])
def test_redirect_is_revalidated_and_pinned(monkeypatch, location):
    requests, _ = network(monkeypatch, [Response(status=302, headers={'Location': location})],
                          {'private.example.com': ['169.254.169.254']})
    with pytest.raises(sources.SourceImportError):
        sources._Transport(budget()).metadata('https://public.example.com/recording')
    assert len(requests) == 1


def test_each_redirect_uses_fresh_public_pin_and_drops_cross_host_post_body(monkeypatch):
    requests, resolutions = network(monkeypatch, [
        Response(status=307, headers={'Location': 'https://cdn.example.com/video'}), Response(b'{}')],
        {'public.example.com': ['8.8.8.8'], 'cdn.example.com': ['1.1.1.1']})
    result = sources._Transport(budget()).metadata('https://public.example.com/api', method='POST', data=b'{"id":1}',
        headers={'Authorization': 'secret', 'Cookie': 'secret', 'Client-ID': 'public-client', 'Host': 'private.example.com'})
    assert result[2] == b'{}'
    assert [r['address'] for r in requests] == ['8.8.8.8', '1.1.1.1']
    assert requests[1]['method'] == 'GET' and requests[1]['body'] is None
    assert 'Client-ID' not in requests[1]['headers']
    assert all('Authorization' not in r['headers'] and 'Cookie' not in r['headers'] and 'Host' not in r['headers'] for r in requests)
    assert len(resolutions) == 2


def test_connection_uses_numeric_socket_and_hostname_verified_tls(monkeypatch):
    calls = []

    class Socket:
        def settimeout(self, timeout):
            calls.append(('timeout', timeout))

        def connect(self, address):
            calls.append(('connect', address))

        def close(self):
            pass

    class Context:
        def wrap_socket(self, raw, server_hostname):
            calls.append(('tls', server_hostname))
            return raw

    context = sources.ssl.create_default_context(cafile=sources.certifi.where())
    assert context.check_hostname and context.verify_mode == sources.ssl.CERT_REQUIRED
    monkeypatch.setattr(sources.socket, 'socket', lambda family, kind: Socket())
    connection = sources._PinnedHTTPSConnection('cdn.example.com', '8.8.8.8', 2)
    connection._context = Context()
    connection.connect()
    assert calls == [('timeout', 2), ('connect', ('8.8.8.8', 443)), ('tls', 'cdn.example.com')]


def test_oversize_and_truncated_responses_fail(monkeypatch):
    requests, _ = network(monkeypatch, [Response(b'x', headers={'Content-Length': '1001'})])
    with pytest.raises(sources.SourceImportError, match='SOURCE_TOO_LARGE'):
        sources._Transport(budget(1000)).download('https://public.example.com/a', io.BytesIO())
    network(monkeypatch, [Response(b'x', headers={'Content-Length': '2'})])
    with pytest.raises(sources.SourceImportError, match='SOURCE_INCOMPLETE'):
        sources._Transport(budget()).download('https://public.example.com/a', io.BytesIO())


def test_socket_reader_checks_deadline_during_slow_response_headers():
    class Socket:
        def settimeout(self, timeout):
            assert 0 < timeout <= 5

    limits = budget()
    raw = io.BytesIO(b'HTTP/1.1 200 OK\r\n')
    reader = sources._CheckedSocketReader(raw, Socket(), limits)
    assert reader.readinto(bytearray(1)) == 1
    assert limits.wire_bytes == 1
    limits.deadline = 0
    with pytest.raises(sources.SourceImportError, match='SOURCE_TIMEOUT'):
        reader.readinto(bytearray(1))
    reader.close()


def test_socket_reader_wire_limit_includes_headers_and_chunk_framing():
    class Socket:
        def settimeout(self, timeout):
            pass

    limits = budget(1)
    limits.wire_bytes = limits.max_bytes + sources._METADATA_TOTAL + 4 * 1024 * 1024
    reader = sources._CheckedSocketReader(io.BytesIO(b'x'), Socket(), limits)
    with pytest.raises(sources.SourceImportError, match='SOURCE_TOO_LARGE'):
        reader.readinto(bytearray(1))
    reader.close()


def test_chunked_aggregate_limit_covers_all_segment_files(monkeypatch):
    network(monkeypatch, [Response(b'123456', headers={}), Response(b'123456', headers={})])
    transport = sources._Transport(budget(10))
    transport.download('https://public.example.com/1', io.BytesIO())
    with pytest.raises(sources.SourceImportError, match='SOURCE_TOO_LARGE'):
        transport.download('https://public.example.com/2', io.BytesIO())


@pytest.mark.parametrize(('manifest', 'code'), [
    ('#EXTM3U\n#EXTINF:1,\na.ts\n', 'SOURCE_RECORDING_REQUIRED'),
    ('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST', 'SOURCE_RESTRICTED'),
    ('#EXTM3U\n#EXT-X-BYTERANGE:12@0\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST', 'SOURCE_FORMAT_UNSUPPORTED'),
    ('#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n#EXT-X-ENDLIST', 'SOURCE_URL_INVALID'),
    ('#EXTM3U\n#EXTINF:100,\na.ts\n#EXT-X-ENDLIST', 'SOURCE_DURATION_EXCEEDED'),
])
def test_hls_rejects_live_encryption_ranges_local_segments_and_excess_duration(monkeypatch, manifest, code):
    network(monkeypatch, [Response(manifest.encode())])
    with pytest.raises(sources.SourceImportError, match=code):
        sources._hls_segments('https://public.example.com/playlist.m3u8', sources._Transport(budget()), {}, 10)


def test_hls_vod_relative_segments_and_init_are_explicit_https(monkeypatch):
    manifest = b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\nsegment.m4s\n#EXT-X-ENDLIST'
    network(monkeypatch, [Response(manifest)])
    assert sources._hls_segments('https://public.example.com/vod/list.m3u8', sources._Transport(budget()), {}, 10) == ([
        'https://public.example.com/vod/init.mp4', 'https://public.example.com/vod/segment.m4s'], 1.0)


def test_recording_policy_rejects_live_private_playlists_and_drm():
    for value in [{'is_live': True}, {'live_status': 'post_live'}, {'_type': 'playlist'},
                  {'availability': 'private'}, {'has_drm': True}]:
        with pytest.raises(sources.SourceImportError):
            sources._recording(value, 100)
    sources._recording({'live_status': 'was_live', 'duration': 15}, 100)


def test_format_selection_excludes_drm_remote_protocols_and_separates_audio():
    video = {'url': 'https://cdn.example.com/v.mp4', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none', 'height': 720}
    audio = {'url': 'https://cdn.example.com/a.m4a', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a'}
    info = {'formats': [{**video, 'url': 'http://cdn.example.com/v.mp4'}, {**video, 'has_drm': True}, video, audio]}
    selected = sources._select_formats(info)
    assert len(selected) == 2 and selected[0]['url'] == video['url']
    assert selected[1]['url'] == audio['url']


def test_extractor_only_uses_pinned_handler_and_no_plugins_js_or_cookies(monkeypatch):
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.globals import all_plugins_loaded, plugin_dirs
    network(monkeypatch, [Response(b'{"duration":15,"formats":[{"url":"https://cdn.example.com/v.mp4"}]}')])

    def extract(ie, url):
        downloader = ie._downloader
        assert set(downloader._request_director.handlers) == {'Pinned'}
        assert downloader.params['js_runtimes'] == {} and downloader.params['cachedir'] is False
        assert downloader.params['cookiesfrombrowser'] is None and downloader.params['cookiefile'] is None
        assert set(downloader._ies) == {'Youtube'}
        return ie._download_json('https://www.youtube.com/api', 'id')

    monkeypatch.setattr(YoutubeIE, '_real_extract', extract)
    info = sources._extract(sources.normalize_source('youtube', 'https://www.youtube.com/watch?v=BaW_jenozKc'), sources._Transport(budget()))
    assert info['duration'] == 15 and plugin_dirs.value == [] and all_plugins_loaded.value is True


def test_extractor_rejection_preserves_safe_ssrf_code(monkeypatch):
    from yt_dlp.extractor.youtube import YoutubeIE
    network(monkeypatch, [Response(status=302, headers={'Location': 'https://127.0.0.1/private?token=secret'})])
    monkeypatch.setattr(YoutubeIE, '_real_extract', lambda ie, url: ie._download_json('https://www.youtube.com/api', 'id'))
    with pytest.raises(sources.SourceImportError, match='SOURCE_URL_UNSAFE') as error:
        sources._extract(sources.normalize_source('youtube', 'https://www.youtube.com/watch?v=BaW_jenozKc'), sources._Transport(budget()))
    assert 'secret' not in str(error.value)


def test_extractor_inherits_public_media_headers_without_credentials(monkeypatch):
    from yt_dlp.extractor.youtube import YoutubeIE

    def extract(ie, url):
        ie._downloader.params['http_headers']['User-Agent'] = 'metadata-agent'
        return {'http_headers': {'Referer': 'https://www.youtube.com/', 'Cookie': 'secret-cookie',
                                 'Authorization': 'secret-token', 'X-Forwarded-For': '8.8.8.8'},
                'formats': [{'url': 'https://cdn.example.com/first.mp4'},
                            {'url': 'https://cdn.example.com/second.mp4',
                             'http_headers': {'user-agent': 'format-agent', 'referer': 'https://www.youtube.com/watch?v=BaW_jenozKc'}}]}

    monkeypatch.setattr(YoutubeIE, '_real_extract', extract)
    info = sources._extract(sources.normalize_source('youtube', 'https://www.youtube.com/watch?v=BaW_jenozKc'), sources._Transport(budget()))
    first, second = [fmt['http_headers'] for fmt in info['formats']]
    assert first['User-Agent'] == 'metadata-agent' and first['Referer'] == 'https://www.youtube.com/'
    assert second['User-Agent'] == 'format-agent' and second['Referer'].endswith('watch?v=BaW_jenozKc')
    for headers in (info['http_headers'], first, second):
        assert 'Cookie' not in headers and 'Authorization' not in headers and 'X-Forwarded-For' not in headers
        assert headers['Accept-Encoding'] == 'identity'
        assert sum(name.lower() == 'user-agent' for name in headers) == 1


@pytest.mark.parametrize(('reason', 'expected', 'code'), [
    ("Sign in to confirm you're not a bot", True, 'SOURCE_BOT_CHECK_REQUIRED'),
    ('Sign in to confirm you\u2019re not a bot', True, 'SOURCE_BOT_CHECK_REQUIRED'),
    ('YouTube is requiring a captcha challenge before playback', True, 'SOURCE_BOT_CHECK_REQUIRED'),
    ('Sign in to view this video', True, 'SOURCE_RESTRICTED'),
    ('Login required to watch this recording', True, 'SOURCE_RESTRICTED'),
    ('This is a private video', True, 'SOURCE_RESTRICTED'),
    ('This video is age-restricted', True, 'SOURCE_RESTRICTED'),
    ('Video unavailable', True, 'SOURCE_UNAVAILABLE'),
    ('Requested format is not available', True, 'SOURCE_UNAVAILABLE'),
    ('Sign in to confirm you are not a bot', False, 'SOURCE_UNAVAILABLE'),
    ('Failed request https://captcha.example.com/login?token=synthetic-secret', True, 'SOURCE_UNAVAILABLE'),
])
def test_extractor_final_expected_restrictions_are_classified_without_secrets(monkeypatch, capsys, reason, expected, code):
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.utils import ExtractorError

    def extract(ie, url):
        raise ExtractorError(reason + ' https://example.com/?token=synthetic-secret', expected=expected)

    monkeypatch.setattr(YoutubeIE, '_real_extract', extract)
    with pytest.raises(sources.SourceImportError) as caught:
        sources._extract(sources.normalize_source('youtube', 'https://youtu.be/BaW_jenozKc'), sources._Transport(budget()))
    assert caught.value.code == code and str(caught.value) == code
    assert caught.value.__suppress_context__
    assert 'synthetic-secret' not in repr(caught.value)
    assert capsys.readouterr() == ('', '')


def test_extractor_uses_typed_wrapped_original_reason_and_bounds_cycles(monkeypatch):
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.utils import DownloadError, ExtractorError

    original = ExtractorError('Sign in to confirm you are not a bot', expected=True)
    wrapped = DownloadError('unrelated wrapper URL https://example.com/?token=secret',
                            exc_info=(ExtractorError, original, None))
    monkeypatch.setattr(YoutubeIE, '_real_extract', lambda *args: (_ for _ in ()).throw(wrapped))
    with pytest.raises(sources.SourceImportError, match='SOURCE_BOT_CHECK_REQUIRED'):
        sources._extract(sources.normalize_source('youtube', 'https://youtu.be/BaW_jenozKc'), sources._Transport(budget()))
    # Wrapper messages alone, cyclic chains, and reasons beyond the length cap
    # must remain generic, never inspect unlimited remote text or recurse.
    cycle = DownloadError('Sign in to confirm you are not a bot')
    cycle.exc_info = (DownloadError, cycle, None)
    assert sources._extractor_error_code(cycle) == 'SOURCE_UNAVAILABLE'
    assert sources._extractor_error_code(ExtractorError('x' * 4096 + ' captcha', expected=True)) == 'SOURCE_UNAVAILABLE'
    deep = original
    for _ in range(20):
        deep = DownloadError('wrapper', exc_info=(type(deep), deep, None))
    assert sources._extractor_error_code(deep) == 'SOURCE_UNAVAILABLE'


@pytest.mark.parametrize(('status', 'code'), [(403, 'SOURCE_ACCESS_DENIED'), (429, 'SOURCE_RATE_LIMITED'),
                                             (404, 'SOURCE_NOT_FOUND'), (410, 'SOURCE_NOT_FOUND'),
                                             (500, 'SOURCE_UNAVAILABLE')])
def test_final_extractor_http_errors_use_typed_status_not_response_text(monkeypatch, capsys, status, code):
    from yt_dlp.extractor.youtube import YoutubeIE
    network(monkeypatch, [Response(b'private response ?token=synthetic-secret', status=status)])
    monkeypatch.setattr(YoutubeIE, '_real_extract', lambda ie, url:
        ie._download_json('https://www.youtube.com/api?token=synthetic-secret', 'id'))
    with pytest.raises(sources.SourceImportError) as caught:
        sources._extract(sources.normalize_source('youtube', 'https://youtu.be/BaW_jenozKc'), sources._Transport(budget()))
    assert caught.value.code == code and str(caught.value) == code
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize(('status', 'code'), [(403, 'SOURCE_ACCESS_DENIED'), (429, 'SOURCE_RATE_LIMITED'),
                                             (404, 'SOURCE_NOT_FOUND'), (410, 'SOURCE_NOT_FOUND'),
                                             (500, 'SOURCE_UNAVAILABLE')])
@pytest.mark.parametrize('kind', ['direct', 'hls', 'dash'])
def test_final_media_and_manifest_http_statuses_are_safe(monkeypatch, tmp_path, status, code, kind):
    network(monkeypatch, [Response(b'upstream ?token=synthetic-secret', status=status)])
    transport = sources._Transport(budget())
    with pytest.raises(sources.SourceImportError) as caught:
        if kind == 'direct':
            transport.download('https://cdn.example.com/file?token=synthetic-secret', io.BytesIO())
        elif kind == 'hls':
            sources._hls_segments('https://cdn.example.com/list.m3u8', transport, {}, 120)
        else:
            sources._download_format({'url': 'https://cdn.example.com/file', 'protocol': 'http_dash_segments',
                                      'fragments': [{'path': 'segment.m4s'}]}, tmp_path / 'segment', transport, 120)
    assert caught.value.code == code and str(caught.value) == code
    assert transport.budget.media_bytes == 0


def test_failed_metadata_client_can_recover_without_poisoning_budget(monkeypatch):
    from yt_dlp.extractor.youtube import YoutubeIE
    requests, _ = network(monkeypatch, [Response(status=429), Response(b'{"duration":15,"formats":[]}')])
    transport = sources._Transport(budget())

    def extract(ie, url):
        assert ie._download_json('https://www.youtube.com/first-client', 'id', fatal=False) is False
        assert transport.budget.failure is None
        ie.report_warning("Sign in to confirm you're not a bot")
        return ie._download_json('https://www.youtube.com/second-client', 'id')

    monkeypatch.setattr(YoutubeIE, '_real_extract', extract)
    info = sources._extract(sources.normalize_source('youtube', 'https://youtu.be/BaW_jenozKc'), transport)
    assert info['duration'] == 15 and len(requests) == 2
    assert transport.budget.failure is None and transport.budget.media_bytes == 0


def test_extractor_restricted_reason_cannot_override_ssrf_budget_failure(monkeypatch):
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.utils import ExtractorError
    requests, _ = network(monkeypatch, [])

    def extract(ie, url):
        ie._download_json('https://127.0.0.1/?token=synthetic-secret', 'id', fatal=False)
        raise ExtractorError('Sign in to confirm you are not a bot', expected=True)

    monkeypatch.setattr(YoutubeIE, '_real_extract', extract)
    with pytest.raises(sources.SourceImportError, match='SOURCE_URL_UNSAFE'):
        sources._extract(sources.normalize_source('youtube', 'https://youtu.be/BaW_jenozKc'), sources._Transport(budget()))
    assert not requests


def test_extractor_restricted_reason_cannot_override_cancellation(monkeypatch):
    from yt_dlp.extractor.youtube import YoutubeIE
    from yt_dlp.utils import ExtractorError
    cancel = RuntimeError('synthetic cancellation')
    stopped = False

    def check():
        if stopped:
            raise cancel

    def extract(ie, url):
        nonlocal stopped
        stopped = True
        raise ExtractorError('Sign in to confirm you are not a bot', expected=True)

    monkeypatch.setattr(YoutubeIE, '_real_extract', extract)
    limits = budget(check=check)
    with pytest.raises(RuntimeError) as caught:
        sources._extract(sources.normalize_source('youtube', 'https://youtu.be/BaW_jenozKc'), sources._Transport(limits))
    assert caught.value is cancel and limits.cancel_error is cancel


def test_extracted_recording_over_limit_fails_before_media_download(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, '_extract', lambda *args: {'duration': 440})
    monkeypatch.setattr(sources, '_download_format', lambda *args: pytest.fail('Over-limit media was downloaded'))
    with pytest.raises(sources.SourceImportError, match='SOURCE_DURATION_EXCEEDED'):
        sources.download_source({'provider': 'youtube', 'url': 'https://youtu.be/BaW_jenozKc'}, tmp_path / 'output.mp4',
            max_bytes=50 * 1024 ** 2, max_duration=120, timeout=30, check_active=lambda: None)
    assert not list(tmp_path.iterdir())


@pytest.fixture(scope='module')
def sample_mp4(tmp_path_factory):
    path = tmp_path_factory.mktemp('source-fixture') / 'sample.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x240:r=25',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', '-movflags', '+faststart', str(path)], check=True, timeout=20)
    return path.read_bytes()


def test_direct_mp4_real_local_remux_and_hash(monkeypatch, tmp_path, sample_mp4):
    network(monkeypatch, [Response(sample_mp4)])
    path = tmp_path / 'output.mp4'
    result = sources.download_source({'provider': 'direct', 'url': 'https://media.example.com/video.mp4?token=synthetic'},
        path, max_bytes=1024 * 1024, max_duration=5, timeout=30, check_active=lambda: None)
    assert result == {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'name': 'direct-recording.mp4'}
    assert list(tmp_path.iterdir()) == [path]


def test_direct_media_duration_checked_after_download(monkeypatch, tmp_path, sample_mp4):
    network(monkeypatch, [Response(sample_mp4)])
    output = tmp_path / 'output.mp4'
    with pytest.raises(sources.SourceImportError, match='SOURCE_DURATION_EXCEEDED'):
        sources.download_source({'provider': 'direct', 'url': 'https://media.example.com/video.mp4'}, output,
            max_bytes=1024 * 1024, max_duration=.1, timeout=30, check_active=lambda: None)
    assert not list(tmp_path.iterdir())


def test_platform_short_preview_cannot_replace_full_recording(monkeypatch, tmp_path, sample_mp4):
    network(monkeypatch, [Response(sample_mp4)])
    monkeypatch.setattr(sources, '_extract', lambda *a: {'duration': 15, 'formats': [{
        'url': 'https://media.example.com/video.mp4', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'mp4a'}]})
    with pytest.raises(sources.SourceImportError, match='SOURCE_INCOMPLETE'):
        sources.download_source({'provider': 'youtube', 'url': 'https://www.youtube.com/watch?v=BaW_jenozKc'}, tmp_path / 'output.mp4',
            max_bytes=1024 * 1024, max_duration=20, timeout=30, check_active=lambda: None)
    assert not list(tmp_path.iterdir())


def test_hls_declared_duration_cannot_hide_missing_media(monkeypatch, tmp_path, sample_mp4):
    manifest = b'#EXTM3U\n#EXTINF:15,\nsegment.mp4\n#EXT-X-ENDLIST'
    network(monkeypatch, [Response(manifest), Response(sample_mp4)])
    monkeypatch.setattr(sources, '_extract', lambda *a: {'formats': [{
        'url': 'https://media.example.com/list.m3u8', 'protocol': 'm3u8_native', 'ext': 'mp4'}]})
    with pytest.raises(sources.SourceImportError, match='SOURCE_INCOMPLETE'):
        sources.download_source({'provider': 'youtube', 'url': 'https://www.youtube.com/watch?v=BaW_jenozKc'}, tmp_path / 'output.mp4',
            max_bytes=1024 * 1024, max_duration=20, timeout=30, check_active=lambda: None)
    assert not list(tmp_path.iterdir())


def test_text_playlist_cannot_be_passed_to_ffmpeg_as_local_input(monkeypatch, tmp_path):
    network(monkeypatch, [Response(b'#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n#EXT-X-ENDLIST')])
    monkeypatch.setattr(sources, '_execute', lambda *a, **kw: pytest.fail('No FFmpeg execution expected'))
    with pytest.raises(sources.SourceImportError, match='SOURCE_FORMAT_UNSUPPORTED'):
        sources.download_source({'provider': 'direct', 'url': 'https://media.example.com/video.mp4'}, tmp_path / 'output.mp4',
            max_bytes=1024 * 1024, max_duration=5, timeout=30, check_active=lambda: None)
    assert not list(tmp_path.iterdir())


def test_cancellation_and_partial_file_cleanup(monkeypatch, tmp_path, sample_mp4):
    network(monkeypatch, [Response(sample_mp4)])
    error = RuntimeError('worker lease lost')
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls >= 12:
            raise error

    with pytest.raises(RuntimeError) as caught:
        sources.download_source({'provider': 'direct', 'url': 'https://media.example.com/video.mp4'}, tmp_path / 'output.mp4',
            max_bytes=1024 * 1024, max_duration=5, timeout=30, check_active=check)
    assert caught.value is error and not list(tmp_path.iterdir())


def test_download_failure_never_exposes_url_or_external_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(sources, '_extract', lambda *a: (_ for _ in ()).throw(RuntimeError('https://example.com/?secret=abc')))
    with pytest.raises(sources.SourceImportError) as caught:
        sources.download_source({'provider': 'youtube', 'url': 'https://www.youtube.com/watch?v=BaW_jenozKc'}, tmp_path / 'output.mp4',
            max_bytes=1024, max_duration=5, timeout=30, check_active=lambda: None)
    assert str(caught.value) == 'SOURCE_UNAVAILABLE' and not list(tmp_path.iterdir())


def test_existing_output_is_preserved(tmp_path):
    output = tmp_path / 'output.mp4'
    output.write_bytes(b'existing')
    with pytest.raises(sources.SourceImportError, match='SOURCE_OUTPUT_EXISTS'):
        sources.download_source({'provider': 'direct', 'url': 'https://media.example.com/video.mp4'}, output,
            max_bytes=1024, max_duration=5, timeout=30, check_active=lambda: None)
    assert output.read_bytes() == b'existing'
