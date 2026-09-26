"""Proxy credentials never change the source authority or tenant boundaries."""
import base64
from pathlib import Path
import subprocess

import pytest

from server import media_sources as sources


def test_request_bytes_are_bounded_before_socket_write():
    class Socket:
        sent = []

        def sendall(self, data):
            self.sent.append(data)

    raw = Socket()
    budget = sources._Budget(1000, 10, lambda: None)
    sock = sources._BudgetedSocket(raw, budget)
    sock.sendall(b'GET / HTTP/1.1\r\n')
    assert budget.wire_bytes == len(raw.sent[0])
    budget.wire_bytes = 1000 + sources._METADATA_TOTAL + 4 * 1024 * 1024
    with pytest.raises(sources.SourceImportError, match='^SOURCE_TOO_LARGE$'):
        sock.sendall(b'x')
    assert len(raw.sent) == 1


def test_cancellation_prevents_socket_write():
    class Socket:
        def sendall(self, data):
            pytest.fail('cancelled request was sent')

    def cancelled():
        raise RuntimeError('lease cancelled')

    sock = sources._BudgetedSocket(Socket(), sources._Budget(1000, 10, cancelled))
    with pytest.raises(RuntimeError, match='lease cancelled'):
        sock.sendall(b'x')


@pytest.mark.parametrize('value', [
    'http://u:p@localhost:823', 'https://u:p@gw.dataimpulse.com:823',
    'http://u:p@gw.dataimpulse.com:80', 'http://u:p@gw.dataimpulse.com:823/?password=x',
    'http://u%0d:p@gw.dataimpulse.com:823', 'http://u:p@gw.dataimpulse.com.evil:823',
])
def test_bad_proxy_is_rejected_without_echo(value):
    with pytest.raises(sources.SourceImportError, match='^SOURCE_PROXY_UNAVAILABLE$'):
        sources._proxy_authorization(value)


def test_proxy_session_is_fixed_for_all_requests_of_one_import():
    transport = sources._Transport(sources._Budget(1000, 10, lambda: None),
                                   'http://synthetic:secret@gw.dataimpulse.com:823')
    auth = base64.b64decode(transport.proxy).decode()
    assert auth.startswith('synthetic__sessid.') and auth.endswith(':secret')
    assert sources._Transport(sources._Budget(1000, 10, lambda: None),
        'http://synthetic:secret@gw.dataimpulse.com:823').proxy != transport.proxy


@pytest.mark.parametrize('host', ['localhost', '169.254.169.254', 'youtube.com.evil.example',
                                  'media.example.com', 'youtube.com.'])
def test_proxy_cannot_reach_arbitrary_or_private_authority(host):
    with pytest.raises(sources.SourceImportError, match='^SOURCE_URL_UNSAFE$'):
        sources._ProxyHTTPSConnection(host, '8.8.8.8', 1,
            budget=sources._Budget(1000, 10, lambda: None), proxy='synthetic')


def test_webm_vp9_opus_becomes_browser_playable_h264_aac(tmp_path):
    source, output = tmp_path / 'source.webm', tmp_path / 'output.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=15',
        '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-c:v', 'libvpx-vp9',
        '-threads', '2', '-c:a', 'libopus', str(source)], check=True)
    budget = sources._Budget(2 * 1024**2, 30, lambda: None)
    sources._make_mp4([source], output, budget, 2, expected_duration=1, transcode=True)
    result = sources._probe(output, budget, 2)
    assert {stream['codec_name'] for stream in result['streams']} == {'h264', 'aac'}
    assert result['duration'] >= 1
