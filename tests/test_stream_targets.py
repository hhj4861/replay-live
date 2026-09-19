"""Destination security checks use synthetic DNS and temporary hosts files only."""
import pytest

from server.media_runtime import stream_command
from server.stream_targets import (LIVE_TARGETS, STREAM_TARGETS, StreamTargetError,
    destination_url, install_host_pin, pin_destination, platform_metadata,
    validate_destination, validate_pinned_destination, verify_host_pin)


def destination(target='custom', url='rtmps://ingest.example.com:443/live', key='synthetic-key?token=abc%2Bdef&x=1'):
    return validate_destination(target, url, key)


def test_platform_metadata_has_ten_targets_and_only_confirmed_default_hosts():
    values = platform_metadata()
    assert {value['id'] for value in values} == STREAM_TARGETS
    assert len(LIVE_TARGETS) == 9
    assert {value['id'] for value in values if value['default_server_url']} == {'youtube', 'twitch', 'kick'}
    assert all(value['note'] for value in values)
    assert validate_destination('local') is None
    assert validate_destination('youtube', stream_key='synthetic-key')['hostname'] == 'a.rtmps.youtube.com'


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'http://ingest.example.com/live', 'rtsp://ingest.example.com/live',
    'rtmp://ingest.example.com:443/live', 'rtmps://ingest.example.com:80/live',
    'rtmps://user:pass@ingest.example.com/live', 'rtmp://localhost/live',
    'rtmp://127.0.0.1/live', 'rtmp://127.1/live', 'rtmp://2130706433/live',
    'rtmp://0x7f000001/live', 'rtmp://0177.0.0.1/live', 'rtmp://10.0.0.1/live',
    'rtmp://169.254.169.254/live', 'rtmp://100.100.100.200/live', 'rtmp://[::1]/live',
    'rtmp://metadata.google.internal/live', 'rtmp://ingest.example.com/live#fragment',
    'rtmp://ingest.example.com/live?token=secret', 'rtmp://ingest.example.com/a/../live',
    'rtmp://ingest.example.com/%0a-live', 'rtmp://ingest.example.com/live\n-option',
    'rtmp://ingest.example.com\\@127.0.0.1/live',
])
def test_unsafe_server_forms_fail_without_dns_or_secret_disclosure(url):
    with pytest.raises(StreamTargetError) as error:
        destination(url=url)
    assert 'synthetic-key' not in str(error.value) and url not in str(error.value)


@pytest.mark.parametrize('key', ['', 'x' * 1025, 'secret key', 'key\n-option', 'key%0D%0AHost=x',
                               'key#fragment', 'key\\path', 'key"quoted', 'rtmp://other.example.com/key'])
def test_key_rejects_injection_but_accepts_query_tokens(key):
    with pytest.raises(StreamTargetError):
        destination(key=key)


def test_dns_pin_rejects_any_private_answer_and_never_follows_later_dns_changes(tmp_path):
    normalized = destination()
    pinned = pin_destination(normalized, resolver=lambda host, port: ['8.8.8.8', '1.1.1.1', '8.8.8.8'])
    assert pinned['addresses'] == ['1.1.1.1', '8.8.8.8']
    assert destination_url(pinned).endswith('/live/synthetic-key?token=abc%2Bdef&x=1')
    for answers in [['8.8.8.8', '10.0.0.1'], ['169.254.169.254'], ['::1'], ['2606:4700:4700::1111'], []]:
        with pytest.raises(StreamTargetError):
            pin_destination(normalized, resolver=lambda host, port: answers)
    hosts = tmp_path / 'hosts'
    hosts.write_text('127.0.0.1 localhost\n10.0.0.1 ingest.example.com\n')
    install_host_pin(pinned['hostname'], pinned['addresses'], hosts_path=hosts)
    assert '10.0.0.1' not in hosts.read_text()
    assert '127.0.0.1 localhost' in hosts.read_text()
    assert verify_host_pin(pinned, hosts_path=hosts, resolver=lambda host, port: pinned['addresses']) == pinned
    with pytest.raises(StreamTargetError):
        verify_host_pin(pinned, hosts_path=hosts, resolver=lambda host, port: ['10.0.0.1'])
    hosts.write_text('127.0.0.1 localhost\n')
    with pytest.raises(StreamTargetError):
        verify_host_pin(pinned, hosts_path=hosts, resolver=lambda host, port: pinned['addresses'])


def test_literal_address_cannot_claim_a_different_pin():
    value = pin_destination(destination(url='rtmp://8.8.8.8:1935/live'), resolver=lambda host, port: ['8.8.8.8'])
    assert verify_host_pin(value, hosts_path='/path-not-read-for-literal') == value
    value['addresses'] = ['1.1.1.1']
    with pytest.raises(StreamTargetError):
        validate_pinned_destination(value)


def test_full_length_key_has_explicit_playpath_instead_of_fixed_size_url_parser():
    key = 'x' * 1024
    value = pin_destination(destination(key=key), resolver=lambda host, port: ['8.8.8.8'])
    command = stream_command('synthetic.mp4', destination_url(value), rtmp_app='live',
        rtmp_playpath=value['stream_key'], rtmp_tcurl=value['server_url'])
    assert command[command.index('-rtmp_playpath') + 1] == key
    assert len(command[-1]) > 1024
    with pytest.raises(StreamTargetError):
        destination(url='rtmp://ingest.example.com/' + 'a' * 1024)
