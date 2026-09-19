"""Validated RTMP destinations and per-worker IPv4 pins; never logs credentials.

The API encrypts normalized destinations. DNS is checked again at claim time.
Only the dispatcher installs hosts pins, inside a new single-job Sandbox VM.
"""
import ipaddress
from pathlib import Path
import queue
import re
import socket
import threading
from urllib.parse import unquote, urlsplit, urlunsplit


_PLATFORMS = (
    ('local', '파일 테스트', None, '파일을 생성하며 외부 플랫폼에 송출하지 않습니다.', None),
    ('youtube', 'YouTube Live', 'rtmps://a.rtmps.youtube.com:443/live2',
     'YouTube Studio에서 라이브 권한과 스트림 키를 준비하세요.',
     'https://support.google.com/youtube/answer/10364924'),
    ('twitch', 'Twitch', 'rtmp://live.twitch.tv:1935/app',
     'Twitch 대시보드의 스트림 키를 사용합니다. 기본 RTMP 연결은 암호화되지 않습니다.',
     'https://dev.twitch.tv/docs/video-broadcast/'),
    ('facebook', 'Facebook Live', None,
     'Live Producer에서 발급된 서버 URL과 키가 필요합니다. 계정 권한과 송출 호환성을 확인하세요.', None),
    ('instagram', 'Instagram Live', None,
     'Live Producer 이용 권한과 현재 방송의 서버 URL·키가 필요합니다.', None),
    ('tiktok', 'TikTok LIVE', None,
     '외부 인코더 이용 권한이 필요합니다. 방송마다 발급된 최신 서버 URL과 키를 입력하세요.',
     'https://webcast.tiktokv.com/falcon/webcast_mt/page/obs_intro/index.html'),
    ('naver', '네이버 TV', None,
     '네이버 TV 라이브 권한과 발급된 서버 URL·키가 필요합니다. 네이버 쇼핑라이브는 지원 대상이 아닙니다.',
     'https://help.naver.com/service/17223/contents/11334?lang=ko&osType=PC'),
    ('chzzk', '치지직', None,
     '치지직 스튜디오의 서버 URL과 키를 사용하세요. 플랫폼별 인코더 권장 설정과 호환 시험이 필요합니다.',
     'https://help.naver.com/service/30044/contents/22587?lang=ko&osType=COMMONOS'),
    ('kick', 'Kick', 'rtmps://fa723fc1b171.global-contribute.live-video.net:443/app',
     'Kick 대시보드에서 스트림 키를 준비하세요.',
     'https://help.kick.com/en/articles/7066931-how-to-stream-on-kick-com'),
    ('custom', '기타 RTMP/RTMPS', None,
     '외부 인코더를 지원하는 서비스의 공인 IPv4 서버 URL과 스트림 키를 입력하세요.', None),
)
STREAM_TARGETS = frozenset(item[0] for item in _PLATFORMS)
LIVE_TARGETS = STREAM_TARGETS - {'local'}
_DEFAULTS = {item[0]: item[2] for item in _PLATFORMS}
_HOST = re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_SAFE_PATH = re.compile(r'/[A-Za-z0-9._~!$&()*+,;=:@%/-]*\Z')
_SAFE_KEY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~!$&()*+,;=:@?%/-]{0,1023}\Z')
_DNS_SLOTS = threading.BoundedSemaphore(8)


class StreamTargetError(ValueError):
    def __init__(self, code='STREAM_DESTINATION_INVALID', message='송출 서버 URL 또는 스트림 키를 확인하세요.'):
        self.code = code
        super().__init__(message)


def platform_metadata():
    return [dict(id=id, label=label, default_server_url=default,
                 requires_server_url=id != 'local' and default is None,
                 requires_stream_key=id != 'local', note=note, setup_url=setup)
            for id, label, default, note, setup in _PLATFORMS]


def public_ipv4(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        ip = ipaddress.ip_address(value)
        if ip.version != 4 or not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise ValueError()
        return str(ip)
    except (ValueError, TypeError):
        raise StreamTargetError('STREAM_DESTINATION_UNSAFE', '공인 IPv4 송출 서버만 사용할 수 있습니다.') from None


def _safe_encoding(value):
    if re.search(r'%(?![a-fA-F0-9]{2})', value):
        return False
    decoded = unquote(value)
    return not any(ord(char) < 33 or ord(char) > 126 or char in '\\#\"\'' for char in decoded)


def validate_destination(target, server_url='', stream_key=''):
    if not isinstance(target, str) or target not in STREAM_TARGETS:
        raise StreamTargetError()
    if target == 'local':
        if server_url or stream_key:
            raise StreamTargetError()
        return None
    if not isinstance(stream_key, str) or not _SAFE_KEY.fullmatch(stream_key) or not _safe_encoding(stream_key) or '://' in stream_key:
        raise StreamTargetError()
    if not isinstance(server_url, str):
        raise StreamTargetError()
    value = server_url or _DEFAULTS[target]
    if not value or len(value) > 2048 or any(ord(char) < 33 or ord(char) > 126 for char in value) or '\\' in value:
        raise StreamTargetError()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        port = parsed.port or (443 if parsed.scheme == 'rtmps' else 1935)
        if (parsed.scheme not in ('rtmp', 'rtmps') or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or '%' in host or host.endswith('.')
                or port not in ({1935} if parsed.scheme == 'rtmp' else {443, 1935})):
            raise StreamTargetError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not _HOST.fullmatch(host) or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home')):
                raise StreamTargetError('STREAM_DESTINATION_UNSAFE')
            # Reject legacy numeric forms resolved by libc (127.1, octal, hex).
            if re.fullmatch(r'[0-9.]+', host) or any(label.startswith('0x') for label in host.split('.')):
                raise StreamTargetError('STREAM_DESTINATION_UNSAFE')
        else:
            host = public_ipv4(host)
        path = parsed.path.rstrip('/') or '/'
        if not _SAFE_PATH.fullmatch(path) or not _safe_encoding(path) or any(part in ('.', '..') for part in unquote(path).split('/')):
            raise StreamTargetError()
        normalized = urlunsplit((parsed.scheme, f'{host}:{port}', path.rstrip('/'), '', ''))
        # FFmpeg's RTMP app/tcurl fields each have a 1024-byte capacity.
        if len(normalized) >= 1024:
            raise StreamTargetError()
        return dict(target=target, server_url=normalized, stream_key=stream_key,
                    hostname=host, port=port, protocol=parsed.scheme)
    except StreamTargetError:
        raise
    except (ValueError, TypeError):
        raise StreamTargetError() from None


def _resolve(host, port):
    """Bound caller wait and outstanding libc resolutions; stuck DNS cannot queue forever."""
    if not _DNS_SLOTS.acquire(blocking=False):
        raise StreamTargetError('STREAM_DNS_UNAVAILABLE', '송출 서버 확인이 지연되고 있습니다. 잠시 후 다시 시도하세요.')
    answer = queue.Queue(maxsize=1)
    def lookup():
        try:
            answer.put((True, [row[4][0] for row in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)]))
        except Exception:
            answer.put((False, None))
        finally:
            _DNS_SLOTS.release()
    threading.Thread(target=lookup, name='stream-dns', daemon=True).start()
    try:
        ok, values = answer.get(timeout=5)
        if ok:
            return values
    except queue.Empty:
        pass
    raise StreamTargetError('STREAM_DNS_UNAVAILABLE', '송출 서버의 공인 IPv4 주소를 확인할 수 없습니다.')


def pin_destination(destination, *, resolver=None):
    if not isinstance(destination, dict):
        raise StreamTargetError()
    normalized = validate_destination(destination.get('target'), destination.get('server_url'), destination.get('stream_key'))
    if normalized is None:
        raise StreamTargetError()
    try:
        addresses = (resolver or _resolve)(normalized['hostname'], normalized['port'])
        if not isinstance(addresses, (list, tuple)) or not 1 <= len(addresses) <= 64:
            raise StreamTargetError()
        addresses = sorted({public_ipv4(value) for value in addresses})
        if len(addresses) > 16:
            raise StreamTargetError()
    except StreamTargetError:
        raise
    except Exception:
        raise StreamTargetError('STREAM_DNS_UNAVAILABLE', '송출 서버 주소를 확인할 수 없습니다.') from None
    return {**normalized, 'addresses': addresses}


def validate_pinned_destination(destination):
    if not isinstance(destination, dict):
        raise StreamTargetError()
    normalized = validate_destination(destination.get('target'), destination.get('server_url'), destination.get('stream_key'))
    if normalized is None or any(destination.get(key) != normalized[key] for key in ('hostname', 'port', 'protocol')):
        raise StreamTargetError()
    addresses = destination.get('addresses')
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 16:
        raise StreamTargetError('STREAM_PIN_MISSING', '송출 서버의 네트워크 고정 정보가 없습니다.')
    pinned = sorted({public_ipv4(value) for value in addresses})
    try:
        ipaddress.ip_address(normalized['hostname'])
    except ValueError:
        pass
    else:
        if pinned != [normalized['hostname']]:
            raise StreamTargetError()
    return {**normalized, 'addresses': pinned}


def install_host_pin(hostname, addresses, *, hosts_path='/etc/hosts'):
    """Dispatcher-only setup. Call with sudo inside the new VM, before job secrets."""
    destination = validate_pinned_destination(dict(target='custom', server_url=f'rtmp://{hostname}:1935/app',
        stream_key='pin-check', hostname=hostname, port=1935, protocol='rtmp', addresses=addresses))
    path = Path(hosts_path)
    lines = []
    for line in path.read_text().splitlines():
        fields = line.split('#', 1)[0].split()
        if hostname not in fields[1:]:
            lines.append(line)
    lines.extend(f'{ip}\t{hostname}' for ip in destination['addresses'])
    path.write_text('\n'.join(lines) + '\n')


def verify_host_pin(destination, *, hosts_path='/etc/hosts', resolver=None):
    pinned = validate_pinned_destination(destination)
    host = pinned['hostname']
    if host in pinned['addresses']:
        return pinned
    entries = []
    for line in Path(hosts_path).read_text().splitlines():
        fields = line.split('#', 1)[0].split()
        if len(fields) > 1 and host in fields[1:]:
            entries.append(fields[0])
    if set(entries) != set(pinned['addresses']):
        raise StreamTargetError('STREAM_PIN_MISSING', '송출 서버의 네트워크 고정 설정이 일치하지 않습니다.')
    # The immutable VM's NSS configuration must actually honor the hosts file.
    # A bad resolver order or stale extra mapping fails before FFmpeg starts.
    resolved = {public_ipv4(value) for value in (resolver or _resolve)(host, pinned['port'])}
    if resolved != set(pinned['addresses']):
        raise StreamTargetError('STREAM_PIN_MISSING', '송출 서버의 주소가 고정된 주소와 일치하지 않습니다.')
    return pinned


def destination_url(destination):
    pinned = validate_pinned_destination(destination)
    return pinned['server_url'].rstrip('/') + '/' + pinned['stream_key']
