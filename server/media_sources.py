"""Import a public recording through a bounded, DNS-pinned HTTPS transport.

yt-dlp is used only for selected, built-in metadata extractors. Its downloaders,
plugins, cookies and default HTTP handlers are never used. Proxy-backed YouTube
extraction may use Node with the pinned, bundled EJS signature solver; remote
components stay disabled.
Media/segments are downloaded here, then FFmpeg only receives local binary files.
Network payload is capped at max_bytes + 16 MiB (metadata); temporary disk at
2 * max_bytes (downloaded inputs plus one output). No source URL is an error text.
"""
from contextlib import contextmanager
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import hashlib
import base64
import http.client
import importlib
import io
import ipaddress
import json
import logging
import math
from pathlib import Path
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
import uuid
import unicodedata
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit

import certifi

from .media_runtime import _execute
from .source_diagnostics import FAILURE_REASONS, PROXY_ERRORS, SourceFailure


_PROVIDERS = (
    ('youtube', 'YouTube', '공개 영상도 YouTube의 서버 접근 제한으로 가져오지 못할 수 있습니다.'),
    ('twitch', 'Twitch', '공개 다시보기·클립 · 구독 전용 영상은 지원하지 않습니다.'),
    ('facebook', 'Facebook', '공개 단일 영상 · 로그인 요구 시 직접 MP4 링크를 사용하세요.'),
    ('instagram', 'Instagram', '공개 동영상 게시물·릴스 · 로그인 요구 시 직접 MP4 링크를 사용하세요.'),
    ('tiktok', 'TikTok', '공개 단일 동영상 · 사진 게시물과 LIVE는 지원하지 않습니다.'),
    ('naver', '네이버 TV', '공개 단일 동영상 · 로그인·지역 제한 영상은 지원하지 않습니다.'),
    ('chzzk', '치지직', '공개 다시보기 · 만료되거나 접근 제한된 영상은 지원하지 않습니다.'),
    ('kick', 'Kick', '공개 다시보기·클립 · 플랫폼이 자동 접근을 막으면 가져올 수 없습니다.'),
    ('direct', '직접 MP4 링크', '로그인 없이 다운로드할 수 있는 HTTPS MP4 주소를 사용하세요.'),
)
_HOSTS = {
    'youtube': {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'},
    'twitch': {'twitch.tv', 'www.twitch.tv', 'm.twitch.tv', 'clips.twitch.tv'},
    'facebook': {'facebook.com', 'www.facebook.com', 'm.facebook.com', 'web.facebook.com', 'fb.watch'},
    'instagram': {'instagram.com', 'www.instagram.com'},
    'tiktok': {'tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'},
    'naver': {'tv.naver.com', 'tvcast.naver.com', 'm.tv.naver.com', 'm.tvcast.naver.com'},
    'chzzk': {'chzzk.naver.com'},
    'kick': {'kick.com', 'www.kick.com'},
}
_EXTRACTORS = {
    'youtube': ('youtube', ('YoutubeIE',)),
    'twitch': ('twitch', ('TwitchVodIE', 'TwitchClipsIE')),
    'facebook': ('facebook', ('FacebookIE', 'FacebookReelIE')),
    'instagram': ('instagram', ('InstagramIE',)),
    'tiktok': ('tiktok', ('TikTokIE', 'TikTokVMIE')),
    'naver': ('naver', ('NaverIE',)),
    'chzzk': ('chzzk', ('CHZZKVideoIE',)),
    'kick': ('kick', ('KickVODIE', 'KickClipIE')),
}
_HOST_PATTERN = re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_DNS_SLOTS = threading.BoundedSemaphore(8)
_CHUNK = 64 * 1024
_METADATA_LIMIT = 4 * 1024 * 1024
_METADATA_TOTAL = 16 * 1024 * 1024
_MAX_REQUESTS = 5000
_MAX_SEGMENTS = 4096
_SEGMENT_CONCURRENCY = 4
_REQUEST_RETRIES = 2
_IMPORT_REQUEST_RETRIES = 8


class SourceImportError(ValueError):
    def __init__(self, code='SOURCE_UNAVAILABLE', *, reason='unclassified', http_status=None, proxy_error=None):
        # Only our fixed codes are exposed; never include extractor exceptions.
        self.code = code if re.fullmatch(r'SOURCE_[A-Z_]+', str(code)) else 'SOURCE_UNAVAILABLE'
        self.reason = reason if isinstance(reason, str) and reason in FAILURE_REASONS else 'unclassified'
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.proxy_error = proxy_error if isinstance(proxy_error, str) and proxy_error in PROXY_ERRORS else None
        self.diagnostics = None
        super().__init__(self.code)


# Only exact, documented status-line identifiers cross the transport boundary.
# Raw reason phrases, headers, response bodies and credentials are never retained.
_PROXY_ERRORS = {
    'PORT_BLOCKED': (403, 'SOURCE_PROXY_ACCESS_DENIED'),
    'SITE_PERMANENTLY_BLOCKED': (403, 'SOURCE_PROXY_ACCESS_DENIED'),
    'HOST_BLOCKED': (403, 'SOURCE_PROXY_ACCESS_DENIED'),
    'NO_USER': (407, 'SOURCE_PROXY_CONFIGURATION'),
    'TRAFFIC_EXHAUSTED': (407, 'SOURCE_PROXY_QUOTA_EXHAUSTED'),
    'THREADS_EXHAUSTED': (407, 'SOURCE_PROXY_BUSY'),
    'PORT_NOT_ALLOWED': (407, 'SOURCE_PROXY_CONFIGURATION'),
    'USER_BLOCKED': (407, 'SOURCE_PROXY_CONFIGURATION'),
    'INTERNAL_SERVER_ERROR': (500, 'SOURCE_PROXY_UNAVAILABLE'),
    'NO_HOST_CONNECTION': (502, 'SOURCE_PROXY_CONNECT_FAILED'),
    'NO_RAY': (503, 'SOURCE_PROXY_POOL_EMPTY'),
}


def _proxy_rejection(header):
    line = bytes(header).split(b'\r\n', 1)[0]
    match = re.fullmatch(rb'HTTP/1\.[01] ([1-5][0-9]{2})(?: ([^\r\n]*))?', line)
    status = int(match[1]) if match else None
    label = (match[2] or b'').decode('ascii', errors='replace').strip() if match else ''
    expected, code = _PROXY_ERRORS.get(label, (None, 'SOURCE_PROXY_UNAVAILABLE'))
    if label == 'USER_RATE_LIMIT_EXCEEDED' and status in {407, 429}:
        code = 'SOURCE_PROXY_QUOTA_EXHAUSTED'
    elif expected is None or status != expected:
        label, code = '', 'SOURCE_PROXY_UNAVAILABLE'
    return SourceImportError(code, reason='proxy_rejected', http_status=status,
                             proxy_error=label or None)


def _http_error_code(status):
    return {403: 'SOURCE_ACCESS_DENIED', 429: 'SOURCE_RATE_LIMITED',
            404: 'SOURCE_NOT_FOUND', 410: 'SOURCE_NOT_FOUND'}.get(status, 'SOURCE_UNAVAILABLE')


def _extractor_error_code(error):
    """Classify a final extractor failure without exposing its message or URL.

    Only expected extractor reasons and typed HTTP errors are evidence. Warnings
    and intermediate client failures must not poison a successful extraction.
    """
    from yt_dlp.networking.exceptions import HTTPError
    from yt_dlp.utils import DownloadError, ExtractorError

    pending, seen = [error], set()
    restricted, http_code = False, 'SOURCE_UNAVAILABLE'
    while pending and len(seen) < 16:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, HTTPError):
            if http_code == 'SOURCE_UNAVAILABLE':
                http_code = _http_error_code(current.status)
        if isinstance(current, ExtractorError) and current.expected is True:
            reason = current.orig_msg
            if isinstance(reason, str):
                # Restriction words inside URLs are not access-control evidence.
                reason = re.sub(r'https?://\S+', '', reason[:4096].lower()).replace('\u2019', "'")
                if (re.search(r'\bconfirm you(?:\'re| are) not a bot\b', reason)
                        or re.search(r'\bcaptcha\b', reason)):
                    return 'SOURCE_BOT_CHECK_REQUIRED'
                if re.search(r'\b(?:sign[ -]in|log[ -]in|login required|requires login|login to|'
                             r'authentication required|private video|video is private|'
                             r'age[ -]restricted|age[ -]verification|confirm your age)\b', reason):
                    restricted = True
        if isinstance(current, (DownloadError, ExtractorError)):
            info = current.exc_info
            if isinstance(info, tuple) and len(info) == 3 and isinstance(info[1], BaseException):
                pending.append(info[1])
            if isinstance(current, ExtractorError) and isinstance(current.cause, BaseException):
                pending.append(current.cause)
            # Inspect only typed wrappers; arbitrary exception text is never read.
            for nested in (current.__cause__, current.__context__):
                if isinstance(nested, BaseException):
                    pending.append(nested)
    return 'SOURCE_RESTRICTED' if restricted else http_code


def source_platforms():
    return [dict(id=id, label=label, note=note) for id, label, note in _PROVIDERS]


def _public_ipv4(value):
    try:
        address = ipaddress.ip_address(value)
        if address.version != 4 or not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError()
        return str(address)
    except (ValueError, TypeError):
        raise SourceImportError('SOURCE_URL_UNSAFE') from None


def _https_url(value):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in value) or '\\' in value
            or re.search(r'%(?![a-fA-F0-9]{2})', value)):
        raise SourceImportError('SOURCE_URL_INVALID')
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        if (parsed.scheme != 'https' or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or '%' in host or host.endswith('.')
                or any(ord(c) < 32 or ord(c) == 127 for c in unquote(value))):
            raise SourceImportError('SOURCE_URL_INVALID')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (not _HOST_PATTERN.fullmatch(host) or re.fullmatch(r'[0-9.]+', host)
                    or any(label.startswith('0x') for label in host.split('.'))
                    or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home', '.test', '.invalid'))):
                raise SourceImportError('SOURCE_URL_UNSAFE')
        else:
            host = _public_ipv4(host)
        return urlunsplit(('https', host, parsed.path or '/', parsed.query, ''))
    except (ValueError, TypeError) as error:
        if isinstance(error, SourceImportError):
            raise
        raise SourceImportError('SOURCE_URL_INVALID') from None


def normalize_source(provider, url):
    if not isinstance(provider, str) or provider not in {p[0] for p in _PROVIDERS}:
        raise SourceImportError('SOURCE_PROVIDER_INVALID')
    normalized = _https_url(url)
    parsed = urlsplit(normalized)
    if provider != 'direct' and parsed.hostname not in _HOSTS[provider]:
        raise SourceImportError('SOURCE_PROVIDER_MISMATCH')
    # Reject obvious collections/live pages before fetching any metadata.
    path = parsed.path
    if provider == 'youtube' and ('list' in parse_qs(parsed.query)
            or not (path == '/watch' and 'v' in parse_qs(parsed.query)
                    or re.fullmatch(r'/(?:shorts|embed)/[\w-]+/?', path)
                    or parsed.hostname == 'youtu.be' and re.fullmatch(r'/[\w-]+/?', path))):
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    if provider == 'twitch' and not (re.fullmatch(r'/videos/\d+/?', path)
            or '/clip/' in path or parsed.hostname == 'clips.twitch.tv' and path != '/'):
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    if provider == 'instagram' and not re.fullmatch(r'/(?:p|reel|reels|tv)/[\w-]+/?', path):
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    if provider == 'tiktok' and parsed.hostname not in {'vm.tiktok.com', 'vt.tiktok.com'} and not re.fullmatch(r'/@[^/]+/video/\d+/?', path):
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    if provider == 'naver' and not re.fullmatch(r'/(?:v|embed)/\d+/?', path):
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    if provider == 'chzzk' and not re.fullmatch(r'/video/\d+/?', path):
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    if provider == 'kick' and not ('/videos/' in path or '/clips/' in path or 'clip' in parse_qs(parsed.query)):
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    return {'provider': provider, 'url': normalized}


class _Budget:
    def __init__(self, max_bytes, timeout, check_active):
        self.max_bytes = max_bytes
        self.deadline = time.monotonic() + timeout
        self.check_active = check_active
        self.media_bytes = self.metadata_bytes = self.requests = 0
        self.wire_bytes = 0
        self.retries = 0
        self.request_retries = 0
        self.failure = None
        self.cancel_error = None
        self._lock = threading.RLock()
        self._transfer_error = None

    def check(self):
        with self._lock:
            try:
                self.check_active()
            except Exception as error:
                self.cancel_error = error
                raise
            if time.monotonic() >= self.deadline:
                raise SourceImportError('SOURCE_TIMEOUT')
            if self.failure:
                raise self.failure
            if self._transfer_error:
                raise self._transfer_error

    def request(self):
        with self._lock:
            self.check()
            self.requests += 1
            if self.requests > _MAX_REQUESTS:
                raise SourceImportError('SOURCE_TOO_COMPLEX')

    def abort_transfer(self, error):
        with self._lock:
            if self._transfer_error is None:
                self._transfer_error = error

    def finish_transfer(self):
        # Only after every segment thread has exited; import-level recovery
        # can then reuse this budget without resetting any usage counters.
        with self._lock:
            self._transfer_error = None

    def remaining(self):
        self.check()
        return max(.001, self.deadline - time.monotonic())

    def retry(self):
        with self._lock:
            self.check()
            # One recovery across the entire import, including manifests/segments.
            # Keep the original byte, wire, request and wall-clock budgets.
            if self.retries >= 1:
                return False
            self.retries += 1
            return True

    def retry_request(self):
        with self._lock:
            self.check()
            if self.request_retries >= _IMPORT_REQUEST_RETRIES:
                return False
            self.request_retries += 1
            return True

    def consume(self, count, *, metadata):
        with self._lock:
            self.check()
            if metadata:
                self.metadata_bytes += count
                if self.metadata_bytes > _METADATA_TOTAL:
                    raise SourceImportError('SOURCE_METADATA_TOO_LARGE')
            else:
                self.media_bytes += count
                if self.media_bytes > self.max_bytes:
                    raise SourceImportError('SOURCE_TOO_LARGE')

    def consume_wire(self, count):
        with self._lock:
            self.wire_bytes += count
            # Includes HTTP headers and chunk framing in addition to body budgets.
            if self.wire_bytes > self.max_bytes + _METADATA_TOTAL + 4 * 1024 * 1024:
                raise SourceImportError('SOURCE_TOO_LARGE')


def _resolve(host, budget):
    try:
        return [_public_ipv4(host)]
    except SourceImportError:
        # Invalid IP literals were already rejected by _https_url.
        pass
    if not _DNS_SLOTS.acquire(blocking=False):
        raise SourceImportError('SOURCE_DNS_UNAVAILABLE')
    answers = queue.Queue(maxsize=1)

    def lookup():
        try:
            answers.put(socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM))
        except Exception:
            answers.put(None)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=lookup, name='source-dns', daemon=True).start()
    end = time.monotonic() + min(5, budget.remaining())
    while time.monotonic() < end:
        budget.check()
        try:
            rows = answers.get(timeout=min(.1, max(.001, end - time.monotonic())))
        except queue.Empty:
            continue
        if not rows or len(rows) > 64:
            break
        addresses = sorted({_public_ipv4(row[4][0]) for row in rows})
        if 1 <= len(addresses) <= 16:
            return addresses
        break
    raise SourceImportError('SOURCE_DNS_UNAVAILABLE')


class _CheckedSocketReader(io.RawIOBase):
    def __init__(self, raw, sock, budget):
        super().__init__()
        self.raw, self.sock, self.budget = raw, sock, budget

    def readable(self):
        return True

    def readinto(self, buffer):
        # Checking only HTTPResponse.read1 is insufficient: a slow header line
        # can otherwise keep http.client.getresponse alive beyond the deadline.
        self.sock.settimeout(min(5, self.budget.remaining()))
        count = self.raw.readinto(buffer)
        if count:
            # These bytes already crossed the wire even if a sibling failed
            # while this socket was blocked in readinto.
            self.budget.consume_wire(count)
        self.budget.check()
        return count

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _BudgetedSocket:
    def __init__(self, sock, budget):
        self.sock, self.budget = sock, budget

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def sendall(self, data, *args, **kwargs):
        self.budget.check()
        self.budget.consume_wire(len(data))
        return self.sock.sendall(data, *args, **kwargs)

    def makefile(self, mode):
        # The unbuffered SocketIO preserves socket ownership/reference counting.
        return io.BufferedReader(_CheckedSocketReader(self.sock.makefile(mode, buffering=0), self.sock, self.budget))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout, *, budget=None):
        # Explicit CA bundle avoids ambient SSL_CERT_FILE / SSL_CERT_DIR changes.
        context = ssl.create_default_context(cafile=certifi.where())
        super().__init__(host, port=443, timeout=timeout, context=context)
        self.address = address
        self.budget = budget

    def connect(self):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            raw.connect((self.address, 443))  # Numeric IPv4 only: no second DNS lookup.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            if self.budget is not None:
                self.sock = _BudgetedSocket(self.sock, self.budget)
        except BaseException:
            raw.close()
            raise


class _ProxyHTTPSConnection(_PinnedHTTPSConnection):
    """Trusted gateway, fixed public YouTube destinations, end-to-end TLS.

    The gateway resolves only Google-owned hostnames. Arbitrary source URLs,
    redirects and other providers must use the original DNS-pinned transport.
    No proxy headers are forwarded inside the TLS tunnel.
    """
    def __init__(self, host, address, timeout, *, budget, proxy):
        if not any(host == domain or host.endswith('.' + domain)
                   for domain in ('youtube.com', 'googlevideo.com', 'ytimg.com')):
            raise SourceImportError('SOURCE_URL_UNSAFE')
        super().__init__(host, address, timeout, budget=budget)
        self.proxy = proxy

    def connect(self):
        addresses = _resolve('gw.dataimpulse.com', self.budget)
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.settimeout(min(5, self.budget.remaining()))
            raw.connect((addresses[0], 823))
            authority = f'{self.host}:443'
            request = (f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n'
                       f'Proxy-Authorization: Basic {self.proxy}\r\n\r\n').encode('ascii')
            self.budget.consume_wire(len(request))
            raw.sendall(request)
            header = bytearray()
            # Read exactly the CONNECT header, never consume TLS bytes.
            while not header.endswith(b'\r\n\r\n'):
                self.budget.check()
                raw.settimeout(min(5, self.budget.remaining()))
                chunk = raw.recv(1)
                if not chunk or len(header) >= 8192:
                    raise SourceImportError('SOURCE_PROXY_UNAVAILABLE')
                header.extend(chunk)
                self.budget.consume_wire(1)
            if not re.match(rb'HTTP/1\.[01] 200(?: |\r)', header):
                raise _proxy_rejection(header)
            self.sock = _BudgetedSocket(self._context.wrap_socket(raw, server_hostname=self.host), self.budget)
        except BaseException:
            raw.close()
            raise


def _proxy_authorization(value, *, renew_session=False):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'http' or parsed.hostname != 'gw.dataimpulse.com' or parsed.port != 823
                or not parsed.username or not parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/') or len(value) > 2048):
            raise ValueError()
        login, password = unquote(parsed.username), unquote(parsed.password)
        if any(ord(c) < 33 or ord(c) > 126 for c in login + password) or ':' in login:
            raise ValueError()
        # A single egress session covers metadata, signatures and media bytes.
        # Recovery replaces only the session; account and targeting stay intact.
        account, separator, parameters = login.partition('__')
        parts = parameters.split(';') if separator else []
        if renew_session:
            parts = [part for part in parts if not part.startswith('sessid.')]
        if not any(part.startswith('sessid.') for part in parts):
            parts.append('sessid.' + uuid.uuid4().hex)
        login = account + '__' + ';'.join(parts)
        return base64.b64encode(f'{login}:{password}'.encode()).decode('ascii')
    except (ValueError, TypeError):
        raise SourceImportError('SOURCE_PROXY_UNAVAILABLE') from None


def _headers(values, *, metadata):
    # Extractors may add public client identifiers. They cannot pass credentials,
    # connection controls, proxy headers, arbitrary Host, or browser cookies.
    allowed = {'user-agent', 'accept', 'accept-language', 'content-type', 'origin', 'referer',
               'client-id', 'x-device-id', 'x-asbd-id', 'x-ig-app-id', 'x-instagram-ajax',
               'x-youtube-client-name', 'x-youtube-client-version', 'x-goog-visitor-id'}
    result = {'user-agent': 'Mozilla/5.0 ReplayLive/1.0', 'accept-encoding': 'identity'}
    if not isinstance(values, dict):
        values = dict(values or {})
    if len(values) > 64:
        raise SourceImportError('SOURCE_UNAVAILABLE')
    for name, value in values.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise SourceImportError('SOURCE_UNAVAILABLE')
        if name.lower() not in allowed:
            continue
        if len(value) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise SourceImportError('SOURCE_UNAVAILABLE')
        if name.lower() in {'referer', 'origin'}:
            _https_url(value)
        result[name.lower()] = value
    return {name.title(): value for name, value in result.items()}


def _media_headers(*layers):
    """Reproduce public header inheritance skipped by extract_info(process=False).

    Do not call yt-dlp's _calc_headers: it also attaches cookies and geographic
    overrides. Keep case-insensitive format > recording > default precedence.
    """
    merged = {}
    for values in layers:
        if values is None:
            continue
        if not isinstance(values, dict):
            values = dict(values)
        for key, value in values.items():
            if not isinstance(key, str):
                raise SourceImportError('SOURCE_UNAVAILABLE')
            merged[key.lower()] = value
    return _headers(merged, metadata=False)


def _retryable_proxy_failure(error):
    return (error.code == 'SOURCE_INCOMPLETE'
            or (error.code == 'SOURCE_UNAVAILABLE' and error.reason in {
                'network_timeout', 'connection_reset', 'connection_error'})
            or (error.code in {'SOURCE_PROXY_UNAVAILABLE', 'SOURCE_PROXY_CONNECT_FAILED',
                               'SOURCE_PROXY_POOL_EMPTY'} and error.reason == 'proxy_rejected'
                and error.http_status in {502, 503, 504}))


class _Transport:
    def __init__(self, budget, proxy_url=None, *, renew_session=False):
        self.budget = budget
        self.proxy = _proxy_authorization(proxy_url, renew_session=renew_session) if proxy_url else None
        self._idle = []
        self._pool_lock = threading.Lock()

    def close_idle(self):
        with self._pool_lock:
            idle, self._idle = self._idle, []
        for _, connection in idle:
            connection.close()

    def _connection(self, host, address, reuse):
        if reuse:
            with self._pool_lock:
                for index, (origin, connection) in enumerate(self._idle):
                    if origin == host:
                        self._idle.pop(index)
                        return connection
                # Do not retain idle tunnels to other origins while opening new
                # ones. At most the four segment workers own open connections.
                stale = self._idle.pop()[1] if self._idle else None
            if stale is not None:
                stale.close()
        cls = _ProxyHTTPSConnection if self.proxy else _PinnedHTTPSConnection
        kwargs = {'proxy': self.proxy} if self.proxy else {}
        return cls(host, address, min(5, self.budget.remaining()), budget=self.budget, **kwargs)

    def _retry_request(self, error, attempt):
        if (not self.proxy or error is self.budget.cancel_error
                or not _retryable_proxy_failure(error) or attempt >= _REQUEST_RETRIES
                or not self.budget.retry_request()):
            return False
        # Short, cancellable backoff. The original deadline and byte/request
        # budgets cover every retry, including partial and failed responses.
        end = time.monotonic() + min(.2 * (2 ** attempt), 1)
        while time.monotonic() < end:
            self.budget.check()
            time.sleep(min(.05, max(0, end - time.monotonic())))
        self.budget.check()
        logging.getLogger(__name__).info(json.dumps({'event': 'source_request_retry',
            'reason': error.reason, 'http_status': error.http_status, 'proxy_error': error.proxy_error,
            'request_retries': self.budget.request_retries}))
        return True

    @contextmanager
    def response(self, url, *, headers=None, method='GET', data=None, reuse=False):
        if method not in {'GET', 'POST', 'HEAD'} or (data is not None and (not isinstance(data, bytes) or len(data) > _METADATA_LIMIT)):
            raise SourceImportError('SOURCE_UNAVAILABLE')
        current = _https_url(url)
        request_headers = _headers(headers or {}, metadata=True)
        reuse = bool(reuse and self.proxy and method == 'GET' and data is None)
        connection = response = None
        try:
            for redirect in range(6):
                self.budget.request()
                parsed = urlsplit(current)
                addresses = _resolve(parsed.hostname, self.budget)
                connection = self._connection(parsed.hostname, addresses[0], reuse)
                path = urlunsplit(('', '', parsed.path, parsed.query, ''))
                connection.request(method, path, body=data, headers=request_headers)
                response = connection.getresponse()
                self.budget.check()
                if response.status not in {301, 302, 303, 307, 308}:
                    yield response, current
                    # Only fully consumed successful HTTP/1.1 responses can
                    # return to the pool. Errors, redirects, cancellation,
                    # truncation and Connection: close always discard it.
                    if (reuse and response.status == 200
                            and getattr(response, 'will_close', True) is False
                            and response.isclosed() and connection.sock is not None):
                        response.close()
                        response = None
                        with self._pool_lock:
                            if len(self._idle) < _SEGMENT_CONCURRENCY:
                                self._idle.append((parsed.hostname, connection))
                                connection = None
                    return
                location = response.getheader('Location')
                if not location or redirect == 5:
                    raise SourceImportError('SOURCE_REDIRECT_INVALID')
                next_url = _https_url(urljoin(current, location))
                next_host = urlsplit(next_url).hostname
                # Never forward metadata POST bodies or platform identifiers to another host.
                if next_host != parsed.hostname:
                    request_headers = _headers({}, metadata=True)
                    if method == 'POST':
                        method, data = 'GET', None
                if response.status == 303 or response.status in {301, 302} and method == 'POST':
                    method, data = 'GET', None
                response.close()
                connection.close()
                response = connection = None
                current = next_url
        except SourceImportError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as error:
            self.budget.check()
            reason = ('network_timeout' if isinstance(error, TimeoutError) else
                      'tls_error' if isinstance(error, ssl.SSLError) else
                      'connection_reset' if isinstance(error, (ConnectionResetError, BrokenPipeError)) else
                      'connection_error')
            raise SourceImportError('SOURCE_UNAVAILABLE', reason=reason) from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()

    def _read(self, response, *, metadata, limit):
        length = response.getheader('Content-Length')
        if length is not None:
            try:
                if not 0 <= int(length) <= limit:
                    raise ValueError()
            except (ValueError, TypeError):
                raise SourceImportError('SOURCE_METADATA_TOO_LARGE' if metadata else 'SOURCE_TOO_LARGE') from None
        if response.getheader('Content-Encoding', 'identity').lower() not in {'', 'identity'}:
            raise SourceImportError('SOURCE_ENCODING_UNSUPPORTED')
        received = 0
        while True:
            self.budget.check()
            # read1 returns after a socket read, so cancellation is checked between chunks.
            chunk = response.read1(min(_CHUNK, limit - received + 1))
            if not chunk:
                break
            received += len(chunk)
            self.budget.consume(len(chunk), metadata=metadata)
            if received > limit:
                raise SourceImportError('SOURCE_METADATA_TOO_LARGE' if metadata else 'SOURCE_TOO_LARGE')
            yield chunk
        if length is not None and received != int(length):
            logging.getLogger(__name__).info(json.dumps({'event': 'source_incomplete',
                'reason': 'response_body_short', 'metadata': metadata,
                'received_bytes': received, 'expected_bytes': int(length)}))
            raise SourceImportError('SOURCE_INCOMPLETE')

    def metadata(self, url, *, headers=None, method='GET', data=None):
        attempt = 0
        while True:
            try:
                with self.response(url, headers=headers, method=method, data=data) as (response, final_url):
                    payload = b'' if method == 'HEAD' else b''.join(self._read(response, metadata=True, limit=_METADATA_LIMIT))
                    return response.status, dict(response.getheaders()), payload, final_url
            except SourceImportError as error:
                if method in {'GET', 'HEAD'} and self._retry_request(error, attempt):
                    attempt += 1
                    continue
                # Let the import restart metadata and media together on a new IP.
                if self.proxy and _retryable_proxy_failure(error):
                    raise
                if (method not in {'GET', 'HEAD'} or error.code not in {'SOURCE_INCOMPLETE', 'SOURCE_UNAVAILABLE'}
                        or not self.budget.retry()):
                    raise

    def download(self, url, file, *, headers=None, reuse=False):
        offset = file.tell()
        attempt = 0
        while True:
            try:
                with self.response(url, headers=headers, reuse=reuse) as (response, _):
                    if response.status != 200:
                        raise SourceImportError(_http_error_code(response.status), reason='http_error',
                                                http_status=response.status)
                    for chunk in self._read(response, metadata=False, limit=self.budget.max_bytes - self.budget.media_bytes):
                        file.write(chunk)
                return
            except SourceImportError as error:
                # Preserve completed earlier segments, never append a second
                # copy of the partial segment. Failed bytes still count as traffic.
                file.seek(offset)
                file.truncate()
                if self._retry_request(error, attempt):
                    attempt += 1
                    continue
                if self.proxy and _retryable_proxy_failure(error):
                    raise
                if error.code not in {'SOURCE_INCOMPLETE', 'SOURCE_UNAVAILABLE'} or not self.budget.retry():
                    raise


class _QuietLogger:
    def debug(self, *_args, **_kwargs):
        pass

    info = warning = error = stdout = stderr = debug


def _extract(source, transport):
    # Imports are deliberately lazy: direct MP4 imports do not need yt-dlp.
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.globals import all_plugins_loaded, plugin_dirs, plugin_ies, plugin_pps, plugin_ies_overrides
        from yt_dlp.networking.common import RequestDirector, RequestHandler, Response
        from yt_dlp.networking.exceptions import HTTPError, RequestError
    except ImportError:
        raise SourceImportError('SOURCE_EXTRACTOR_UNAVAILABLE') from None
    # This service exclusively owns yt-dlp usage in each worker. Prevent its
    # implicit API constructor hook from loading ambient Python plugins.
    if plugin_ies.value or plugin_pps.value or plugin_ies_overrides.value:
        raise SourceImportError('SOURCE_EXTRACTOR_UNAVAILABLE')
    plugin_dirs.value = []
    all_plugins_loaded.value = True

    class PinnedRH(RequestHandler):
        _SUPPORTED_URL_SCHEMES = ('https',)
        _SUPPORTED_PROXY_SCHEMES = ()

        def _check_extensions(self, extensions):
            # No browser impersonation, alternate cookies, or custom transports.
            for key in ('timeout', 'keep_header_casing'):
                extensions.pop(key, None)

        def _send(self, request):
            try:
                status, headers, payload, url = transport.metadata(request.url,
                    headers=self._get_headers(request), method=request.method, data=request.data)
            except Exception as error:
                transport.budget.failure = error
                raise RequestError('Public recording request failed') from None
            result = Response(io.BytesIO(payload), url, headers, status=status)
            if not 200 <= status < 300:
                raise HTTPError(result)
            return result

    class PinnedYoutubeDL(YoutubeDL):
        def build_request_director(self, handlers, preferences=None):
            director = RequestDirector(_QuietLogger())
            director.add_handler(PinnedRH(logger=_QuietLogger(), headers=self.params['http_headers'], proxies={}))
            return director

        def get_info_extractor(self, key):
            if key not in self._ies:
                raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
            return super().get_info_extractor(key)

    params = dict(quiet=True, no_warnings=True, logger=_QuietLogger(), cachedir=False,
                  proxy='', geo_verification_proxy=None, cookiefile=None, cookiesfrombrowser=None,
                  usenetrc=False, noplaylist=True, skip_download=True, extract_flat=False,
                  socket_timeout=5, retries=0, extractor_retries=0, ignoreerrors=False,
                  enable_file_urls=False, js_runtimes={}, remote_components=set(),
                  allowed_extractors=[], postprocessors=[], format='best',
                  extractor_args={'youtube': {'player_client': ['android_vr', 'web_safari']}})
    if transport.proxy:
        # Pinned, bundled EJS and Node execute signature challenges locally.
        # All HTTP still goes through PinnedRH; remote components remain off.
        params.update(js_runtimes={'node': {}}, extractor_args={})
    try:
        module, names = _EXTRACTORS[source['provider']]
        with PinnedYoutubeDL(params, auto_init=False) as ydl:
            for name in names:
                cls = getattr(importlib.import_module(f'yt_dlp.extractor.{module}'), name, None)
                if cls is not None:
                    ydl.add_info_extractor(cls())
            url = source['url']
            for _ in range(3):
                transport.budget.check()
                candidates = [ie for ie in ydl._ies.values() if ie.suitable(url)]
                if not candidates:
                    raise SourceImportError('SOURCE_RECORDING_REQUIRED')
                info = ydl.extract_info(url, download=False, process=False, ie_key=candidates[0].ie_key())
                transport.budget.check()
                if not isinstance(info, dict):
                    raise SourceImportError()
                if info.get('_type', 'video') == 'video':
                    defaults = ydl.params['http_headers']
                    recording_headers = info.get('http_headers')
                    info['http_headers'] = _media_headers(defaults, recording_headers)
                    for fmt in info.get('formats') or []:
                        if isinstance(fmt, dict):
                            fmt['http_headers'] = _media_headers(defaults, recording_headers, fmt.get('http_headers'))
                    return info
                if info.get('_type') not in {'url', 'url_transparent'}:
                    raise SourceImportError('SOURCE_RECORDING_REQUIRED')
                # Short links may only resolve to another recording of the same provider.
                url = normalize_source(source['provider'], info.get('url'))['url']
            raise SourceImportError('SOURCE_REDIRECT_INVALID')
    except Exception as error:
        transport.budget.check()
        if isinstance(error, SourceImportError):
            raise
        raise SourceImportError(_extractor_error_code(error), reason='extractor_error') from None


def _recording(info, max_duration):
    if (info.get('_type', 'video') != 'video' or info.get('is_live')
            or info.get('live_status') in {'is_live', 'is_upcoming', 'post_live'}):
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    if info.get('has_drm') or info.get('availability') in {'private', 'premium_only', 'subscriber_only', 'needs_auth'}:
        raise SourceImportError('SOURCE_RESTRICTED')
    if info.get('duration') is not None:
        try:
            duration = float(info['duration'])
            if not math.isfinite(duration) or duration <= 0 or (max_duration and duration > max_duration):
                raise ValueError()
        except (ValueError, TypeError):
            raise SourceImportError('SOURCE_DURATION_EXCEEDED') from None


def _select_formats(info, *, transcode=False):
    formats = info.get('formats') or [info]
    if not isinstance(formats, list) or len(formats) > 1000:
        raise SourceImportError('SOURCE_TOO_COMPLEX')
    candidates = []
    for fmt in formats:
        if not isinstance(fmt, dict) or fmt.get('has_drm'):
            continue
        protocol = fmt.get('protocol') or ('https' if str(fmt.get('url', '')).startswith('https:') else '')
        if protocol not in {'https', 'm3u8_native', 'm3u8', 'http_dash_segments'}:
            continue
        if fmt.get('ext') not in ({None, 'mp4', 'm4a', 'webm'} if transcode else {None, 'mp4', 'm4a'}):
            continue
        if (fmt.get('height') or 0) > 1080 or (fmt.get('width') or 0) > 1920 or (fmt.get('fps') or 0) > 60:
            continue
        video, audio = str(fmt.get('vcodec') or ''), str(fmt.get('acodec') or '')
        if video not in {'', 'none'} and not video.startswith(('avc', 'h264', 'av01', 'vp9') if transcode else ('avc', 'h264')):
            continue
        if audio not in {'', 'none'} and not audio.startswith(('mp4a', 'aac', 'opus') if transcode else ('mp4a', 'aac')):
            continue
        candidates.append({**fmt, 'protocol': protocol})
    # Prefer one complete file; otherwise combine a video rendition and AAC audio.
    def rank(fmt):
        return (fmt.get('height') or 0, fmt.get('tbr') or 0)
    combined = [f for f in candidates if f.get('vcodec') != 'none' and f.get('acodec') != 'none']
    if combined:
        return [max(combined, key=rank)]
    videos = [f for f in candidates if f.get('vcodec') != 'none' and f.get('acodec') == 'none']
    audios = [f for f in candidates if f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    if videos and audios:
        return [max(videos, key=rank), max(audios, key=rank)]
    raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')


def _hls_segments(url, transport, headers, max_duration):
    status, _, payload, final_url = transport.metadata(url, headers=headers)
    if status != 200:
        raise SourceImportError(_http_error_code(status))
    try:
        text = payload.decode('utf-8-sig')
    except UnicodeError:
        raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED') from None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != '#EXTM3U' or '#EXT-X-ENDLIST' not in lines:
        raise SourceImportError('SOURCE_RECORDING_REQUIRED')
    if any(line.startswith(('#EXT-X-STREAM-INF', '#EXT-X-MEDIA:', '#EXT-X-BYTERANGE',
                            '#EXT-X-DISCONTINUITY', '#EXT-X-GAP', '#EXT-X-PART', '#EXT-X-SESSION-KEY')) for line in lines):
        raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
    if any(line.startswith('#EXT-X-KEY:') and line != '#EXT-X-KEY:METHOD=NONE' for line in lines):
        raise SourceImportError('SOURCE_RESTRICTED')
    segments = []
    duration = 0.0
    pending_duration = False
    for line in lines:
        transport.budget.check()
        if line.startswith('#EXTINF:'):
            try:
                value = float(line.split(':', 1)[1].split(',', 1)[0])
                if not math.isfinite(value) or value <= 0 or pending_duration:
                    raise ValueError()
                duration += value
                if max_duration and duration > max_duration:
                    raise SourceImportError('SOURCE_DURATION_EXCEEDED')
                pending_duration = True
            except (ValueError, IndexError) as error:
                if isinstance(error, SourceImportError):
                    raise
                raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED') from None
        elif line.startswith('#EXT-X-MAP:'):
            match = re.fullmatch(r'#EXT-X-MAP:URI="([^"\r\n]+)"', line)
            if not match or segments:
                raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
            segments.append(_https_url(urljoin(final_url, match[1])))
        elif not line.startswith('#'):
            if not pending_duration:
                raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
            segments.append(_https_url(urljoin(final_url, line)))
            pending_duration = False
        if len(segments) > _MAX_SEGMENTS:
            raise SourceImportError('SOURCE_TOO_COMPLEX')
    if not segments or pending_duration or duration <= 0:
        raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
    return segments, duration


def _download_segments(urls, path, transport, headers):
    """Bound prefetch to four segments, then append in manifest order."""
    budget = transport.budget
    with tempfile.TemporaryDirectory(prefix='segments-', dir=path.parent) as directory:
        def fetch(index):
            part = Path(directory) / str(index)
            try:
                budget.check()
                with part.open('xb') as file:
                    transport.download(urls[index], file, headers=headers, reuse=True)
                return part
            except BaseException as error:
                budget.abort_transfer(error)
                raise

        pool = ThreadPoolExecutor(max_workers=_SEGMENT_CONCURRENCY, thread_name_prefix='source-segment')
        try:
            pending = deque()
            next_index = 0
            with path.open('xb') as output:
                while next_index < min(len(urls), _SEGMENT_CONCURRENCY):
                    pending.append(pool.submit(fetch, next_index))
                    next_index += 1
                while pending:
                    part = pending.popleft().result()
                    budget.check()
                    with part.open('rb') as file:
                        while chunk := file.read(_CHUNK):
                            budget.check()
                            output.write(chunk)
                    part.unlink()
                    if next_index < len(urls):
                        pending.append(pool.submit(fetch, next_index))
                        next_index += 1
        except BaseException as error:
            budget.abort_transfer(error)
            raise
        finally:
            # Stop siblings cooperatively through all budgeted socket reads.
            # Never delete their files or rotate sessions while they still run.
            pool.shutdown(wait=True, cancel_futures=True)
            transport.close_idle()
            budget.finish_transfer()


def _download_format(fmt, path, transport, max_duration):
    headers = fmt.get('http_headers') or {}
    protocol = fmt.get('protocol')
    url = _https_url(fmt.get('url'))
    duration = None
    if protocol in {'m3u8', 'm3u8_native'}:
        urls, duration = _hls_segments(url, transport, headers, max_duration)
    elif protocol == 'http_dash_segments':
        fragments = fmt.get('fragments')
        if not isinstance(fragments, list) or not 1 <= len(fragments) <= _MAX_SEGMENTS:
            raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
        urls = []
        declared_duration = 0.0
        base = _https_url(fmt.get('fragment_base_url') or url)
        for fragment in fragments:
            if not isinstance(fragment, dict) or set(fragment) - {'url', 'path', 'duration', 'index', 'fragment_count'}:
                raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
            fragment_url = fragment.get('url') or fragment.get('path')
            if not isinstance(fragment_url, str):
                raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
            urls.append(_https_url(urljoin(base, fragment_url)))
            if fragment.get('duration') is not None:
                value = float(fragment['duration'])
                if not math.isfinite(value) or value <= 0:
                    raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
                declared_duration += value
                if max_duration and declared_duration > max_duration:
                    raise SourceImportError('SOURCE_DURATION_EXCEEDED')
        duration = declared_duration or None
    else:
        urls = [url]
    started = time.monotonic()
    before_bytes = transport.budget.media_bytes
    parallel = bool(transport.proxy) and len(urls) > 1
    if parallel:
        _download_segments(urls, path, transport, headers)
    else:
        with path.open('xb') as file:
            for segment in urls:
                transport.budget.check()
                transport.download(segment, file, headers=headers)
    logging.getLogger(__name__).info(json.dumps({'event': 'source_transfer',
        'segments': len(urls), 'concurrency': min(len(urls), _SEGMENT_CONCURRENCY) if parallel else 1,
        'elapsed_ms': round((time.monotonic() - started) * 1000),
        'request_retries': transport.budget.request_retries,
        'media_bytes': transport.budget.media_bytes - before_bytes}))
    return duration


def _binary_format(path):
    with path.open('rb') as file:
        header = file.read(564)
    if len(header) >= 8 and header[4:8] in {b'ftyp', b'styp', b'moov', b'moof', b'free'}:
        return 'mov'
    if len(header) >= 377 and header[0] == header[188] == header[376] == 0x47:
        return 'mpegts'
    if header.startswith(b'\x1a\x45\xdf\xa3'):
        return 'matroska,webm'
    # Force a binary demuxer; never let FFmpeg interpret a text playlist with file references.
    raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')


def _local_input(path):
    fmt = _binary_format(path)
    return ['-protocol_whitelist', 'file,pipe', '-format_whitelist', fmt, '-f', fmt,
            *(['-enable_drefs', '0', '-use_absolute_path', '0'] if fmt == 'mov' else []), '-i', str(path)]


def _probe(path, budget, max_duration):
    payload = bytearray()

    def collect(chunk):
        budget.check()
        if len(payload) + len(chunk) > 256 * 1024:
            raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
        payload.extend(chunk)

    def active():
        budget.check()
        return False

    code, _, _, error, _, _ = _execute(['ffprobe', '-v', 'error', *_local_input(path),
        '-show_entries', 'format=duration:stream=codec_type,codec_name,pix_fmt,width,height', '-of', 'json'],
        timeout=min(30, budget.remaining()), should_stop=active, on_stdout=collect)
    if code != 0 or error:
        raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
    try:
        info = json.loads(payload)
        duration = float(info['format']['duration'])
        if not math.isfinite(duration) or duration <= 0 or (max_duration and duration > max_duration):
            raise SourceImportError('SOURCE_DURATION_EXCEEDED')
        return {'streams': info['streams'], 'duration': duration}
    except (KeyError, ValueError, TypeError) as error:
        if isinstance(error, SourceImportError):
            raise
        raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED') from None


def _require_complete(actual, expected, *, rounded_seconds=False):
    # YouTube's whole-second metadata can round a complete 16.533s Short to 17s.
    # Only that metadata gets a one-second allowance; manifests and remux checks
    # keep the stricter tolerance so missing segments are still rejected.
    tolerance = max(1.0 if rounded_seconds else .25, min(2.0, (expected or 0) * .01))
    if expected is not None and actual + tolerance < expected:
        logging.getLogger(__name__).info(json.dumps({'event': 'source_incomplete',
            'reason': 'duration_short', 'actual_seconds': actual, 'expected_seconds': expected}))
        raise SourceImportError('SOURCE_INCOMPLETE')


def _make_mp4(paths, output, budget, max_duration, *, expected_duration=None,
              rounded_duration=None, transcode=False):
    infos = [_probe(path, budget, max_duration) for path in paths]
    videos = [s for s in infos[0]['streams'] if s.get('codec_type') == 'video']
    audios = [s for s in infos[-1]['streams'] if s.get('codec_type') == 'audio']
    if (len(videos) != 1 or len(audios) != 1
            or videos[0].get('codec_name') not in ({'h264', 'av1', 'vp9'} if transcode else {'h264'})
            or audios[0].get('codec_name') not in ({'aac', 'opus'} if transcode else {'aac'})
            or not 0 < videos[0].get('width', 0) <= 1920 or not 0 < videos[0].get('height', 0) <= 1920):
        raise SourceImportError('SOURCE_FORMAT_UNSUPPORTED')
    for info in infos:
        _require_complete(info['duration'], expected_duration)
        _require_complete(info['duration'], rounded_duration, rounded_seconds=True)
    command = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-xerror', '-threads', '2']
    for path in paths:
        command.extend(_local_input(path))
    encode_video = transcode and (videos[0]['codec_name'] != 'h264' or videos[0].get('pix_fmt') != 'yuv420p')
    encode_audio = transcode and audios[0]['codec_name'] != 'aac'
    codecs = (['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p', '-threads', '2']
              if encode_video else ['-c:v', 'copy'])
    codecs += ['-c:a', 'aac', '-b:a', '128k'] if encode_audio else ['-c:a', 'copy']
    command.extend(['-map', '0:v:0', '-map', f'{len(paths) - 1}:a:0', *codecs, '-movflags', '+faststart',
                    '-f', 'mp4', '-y', str(output)])

    def active():
        budget.check()
        return False

    code, _, _, error, _, _ = _execute(command, timeout=budget.remaining(), should_stop=active,
        output_path=output, max_output_bytes=budget.max_bytes)
    budget.check()
    if code != 0 or error or not output.exists() or not 0 < output.stat().st_size < budget.max_bytes:
        raise SourceImportError('SOURCE_TOO_LARGE' if error == 'OUTPUT_LIMIT_EXCEEDED' else 'SOURCE_FORMAT_UNSUPPORTED')
    result = _probe(output, budget, max_duration)
    _require_complete(result['duration'], max(info['duration'] for info in infos))
    _require_complete(result['duration'], expected_duration)
    _require_complete(result['duration'], rounded_duration, rounded_seconds=True)


def download_source(source, output: Path, *, max_bytes: int, max_duration: float,
                    timeout: float, check_active, proxy_url=None):
    """Create one local H.264/AAC MP4, or remove partial files and raise a safe code."""
    if (not isinstance(source, dict) or isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
            or not 0 < max_bytes <= 100 * 1024 ** 3
            or not isinstance(max_duration, (int, float)) or not math.isfinite(max_duration) or not 0 <= max_duration <= 14400
            or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 14400
            or not callable(check_active)):
        raise SourceImportError('SOURCE_LIMIT_INVALID')
    normalized = normalize_source(source.get('provider'), source.get('url'))
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise SourceImportError('SOURCE_OUTPUT_EXISTS')
    budget = _Budget(max_bytes, timeout, check_active)
    use_proxy = bool(proxy_url and normalized['provider'] == 'youtube')
    transport = _Transport(budget, proxy_url if use_proxy else None)
    complete = False
    started = time.monotonic()
    source_title = None
    stage = 'metadata'
    stage_started = started

    def finish_stage():
        logging.getLogger(__name__).info(json.dumps({'event': 'source_import_stage', 'stage': stage,
            'elapsed_ms': round((time.monotonic() - stage_started) * 1000), 'retries': budget.retries}))

    def record_failure(error):
        error.diagnostics = SourceFailure(stage=stage, reason=error.reason,
            elapsed_ms=max(0, min(14_400_000, round((time.monotonic() - started) * 1000))),
            retries=budget.retries, http_status=error.http_status,
            request_retries=budget.request_retries if use_proxy else None, proxy_error=error.proxy_error)
        logging.getLogger(__name__).info(json.dumps({'event': 'source_import_failed',
            'code': error.code, **error.diagnostics.model_dump(exclude_none=True)}))

    try:
        budget.check()
        while True:
            try:
                with tempfile.TemporaryDirectory(prefix='source-', dir=output.parent) as directory:
                    rounded_duration = None
                    if normalized['provider'] == 'direct':
                        formats = [{'url': normalized['url'], 'protocol': 'https'}]
                        expected_duration = None
                    else:
                        info = _extract(normalized, transport)
                        _recording(info, max_duration)
                        if normalized['provider'] == 'youtube' and isinstance(info.get('title'), str):
                            # Display text only: never use a platform title as a filesystem path.
                            title = ''.join(' ' if unicodedata.category(c) in {'Cc', 'Cf'} else c
                                            for c in info['title'])
                            source_title = ' '.join(title.split())[:180].strip() or None
                        formats = _select_formats(info, transcode=use_proxy)
                        expected_duration = float(info['duration']) if info.get('duration') is not None else None
                        if (normalized['provider'] == 'youtube' and expected_duration is not None
                                and expected_duration.is_integer()):
                            rounded_duration, expected_duration = expected_duration, None
                    finish_stage()
                    stage, stage_started = 'download', time.monotonic()
                    paths = []
                    for index, fmt in enumerate(formats):
                        path = Path(directory) / f'input-{index}.media'
                        declared_duration = _download_format(fmt, path, transport, max_duration)
                        if declared_duration is not None:
                            expected_duration = max(expected_duration or 0, declared_duration)
                        paths.append(path)
                    finish_stage()
                    stage, stage_started = 'prepare_mp4', time.monotonic()
                    _make_mp4(paths, output, budget, max_duration, expected_duration=expected_duration,
                              rounded_duration=rounded_duration, transcode=use_proxy)
                    finish_stage()
                break
            except SourceImportError as error:
                if (not use_proxy or stage not in {'metadata', 'download'}
                        or error is budget.cancel_error or not _retryable_proxy_failure(error)):
                    raise
                # yt-dlp latches transport failures to stop its own fallbacks.
                # Clear only this recoverable error, retaining every usage limit.
                if budget.failure is error:
                    budget.failure = None
                if not budget.retry():
                    raise
                transport = _Transport(budget, proxy_url, renew_session=True)
                logging.getLogger(__name__).info(json.dumps({'event': 'source_proxy_rotated',
                    'stage': stage, 'reason': error.reason, 'http_status': error.http_status,
                    'proxy_error': error.proxy_error, 'retries': budget.retries}))
                stage, stage_started = 'metadata', time.monotonic()
        stage, stage_started = 'checksum', time.monotonic()
        digest = hashlib.sha256()
        with output.open('rb') as file:
            while chunk := file.read(_CHUNK):
                budget.check()
                digest.update(chunk)
        finish_stage()
        complete = True
        result = {'bytes': output.stat().st_size, 'sha256': digest.hexdigest(), 'name': f'{normalized["provider"]}-recording.mp4'}
        if source_title:
            result['source_title'] = source_title
        return result
    except SourceImportError as error:
        record_failure(error)
        raise
    except Exception as error:
        if error is budget.cancel_error:
            raise
        failure = SourceImportError('SOURCE_UNAVAILABLE', reason='unexpected_error')
        record_failure(failure)
        raise failure from None
    finally:
        if not complete:
            output.unlink(missing_ok=True)
