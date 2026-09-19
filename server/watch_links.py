"""Validate user-supplied viewing links without fetching or resolving their hosts.

These links are UI metadata, never media sources or RTMP destinations. Custom
hosts are syntactically public-looking; their DNS answers are not certified here.
"""
import re
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from .stream_targets import STREAM_TARGETS, StreamTargetError, public_ipv4


class WatchLinkError(ValueError):
    code = 'WATCH_LINK_INVALID'

    def __init__(self):
        super().__init__('시청 링크를 확인하세요. 선택한 플랫폼의 HTTPS 채널 또는 방송 주소가 필요합니다.')


_HOSTS = {
    'youtube': ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'),
    'twitch': ('twitch.tv', 'www.twitch.tv'),
    'facebook': ('facebook.com', 'www.facebook.com', 'm.facebook.com'),
    'instagram': ('instagram.com', 'www.instagram.com'),
    'tiktok': ('tiktok.com', 'www.tiktok.com'),
    'naver': ('tv.naver.com',),
    'chzzk': ('chzzk.naver.com',),
    'kick': ('kick.com', 'www.kick.com'),
}
_CANONICAL_HOSTS = {
    'youtube': 'www.youtube.com', 'twitch': 'www.twitch.tv', 'facebook': 'www.facebook.com',
    'instagram': 'www.instagram.com', 'tiktok': 'www.tiktok.com', 'naver': 'tv.naver.com',
    'chzzk': 'chzzk.naver.com', 'kick': 'kick.com',
}
_RESERVED = {
    'youtube': {'watch', 'live', 'shorts'},
    'twitch': {'videos', 'directory', 'downloads', 'settings', 'login', 'signup', 'search',
               'subscriptions', 'wallet', 'inventory', 'moderator', 'p'},
    'facebook': {'watch', 'video.php', 'profile.php', 'login', 'groups', 'share', 'sharer.php', 'dialog'},
    'instagram': {'accounts', 'p', 'reel', 'reels', 'explore', 'direct', 'stories', 'live'},
    'tiktok': {'login', 'signup', 'explore'},
    'naver': {'v', 'l'},
    'kick': {'videos', 'video', 'categories', 'search', 'settings', 'login', 'register'},
}
_TRACKING = {'si', 'feature', 't', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content',
             'utm_term', 'fbclid', 'igsh', 'mibextid'}
_HOST = re.compile(r'(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z')
_VIDEO_ID = r'[A-Za-z0-9_-]{11}'
_UUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'


def _unsafe(value):
    return any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in '\\%#?/' for char in value)


def _channel_name(target, value):
    patterns = {'twitch': r'[A-Za-z0-9_]{1,25}', 'instagram': r'[A-Za-z0-9._]+',
                'tiktok': r'[A-Za-z0-9._]+', 'kick': r'[A-Za-z0-9_-]+'}
    return bool(re.fullmatch(patterns.get(target, r'[A-Za-z0-9._-]+'), value)
                and value.lower() not in _RESERVED.get(target, set()))


def _platform_path(target, kind, host, path, query):
    parts = path.lstrip('/').split('/')
    if target == 'youtube':
        if kind == 'broadcast':
            if host == 'youtu.be' and re.fullmatch('/' + _VIDEO_ID, path) and not query:
                return '/watch', {'v': path[1:]}
            if path == '/watch' and set(query) == {'v'} and re.fullmatch(_VIDEO_ID, query['v']):
                return path, query
            if re.fullmatch(r'/(?:live|shorts)/' + _VIDEO_ID, path) and not query:
                return path, query
        elif host != 'youtu.be' and not query:
            if parts[-1] == 'live' and len(parts) > 1:
                parts.pop()
            if len(parts) == 1 and parts[0].startswith('@'):
                handle = parts[0][1:]
                if handle and all(char in '._-' or unicodedata.category(char)[0] in 'LN' for char in handle):
                    return '/' + parts[0], query
            if (len(parts) == 2 and parts[0] in ('channel', 'c', 'user') and _channel_name(target, parts[1])
                    and (parts[0] != 'channel' or re.fullmatch(r'[A-Za-z0-9_-]+', parts[1]))):
                return '/' + '/'.join(parts), query
    elif target == 'facebook':
        if kind == 'channel' and path == '/profile.php' and set(query) == {'id'} and re.fullmatch(r'[0-9]+', query['id']):
            return path, query
        if kind == 'broadcast' and path in ('/watch', '/video.php') and set(query) == {'v'} and re.fullmatch(r'[0-9]+', query['v']):
            return path, query
        if (not query and kind == 'broadcast' and re.fullmatch(r'/[A-Za-z0-9._-]+/videos/[0-9]+', path)
                and _channel_name(target, parts[0])):
            return path, query
    elif target == 'twitch' and kind == 'broadcast' and not query and re.fullmatch(r'/videos/[0-9]+', path):
        return path, query
    elif target == 'instagram' and kind == 'broadcast' and not query:
        if re.fullmatch(r'/(?:p|reel)/[A-Za-z0-9_-]+', path) or (len(parts) == 2 and parts[1] == 'live' and _channel_name(target, parts[0])):
            return path, query
    elif target == 'tiktok' and not query:
        if parts[0].startswith('@') and _channel_name(target, parts[0][1:]):
            if kind == 'channel' and len(parts) == 1:
                return path, query
            if kind == 'broadcast' and (len(parts) == 2 and parts[1] == 'live'
                    or len(parts) == 3 and parts[1] == 'video' and re.fullmatch(r'[0-9]+', parts[2])):
                return path, query
    elif target == 'naver' and kind == 'broadcast' and not query and re.fullmatch(r'/(?:v|l)/[0-9]+', path):
        return path, query
    elif target == 'chzzk' and not query:
        if kind == 'channel' and re.fullmatch(r'/[0-9a-fA-F]{32}', path):
            return path.lower(), query
        if kind == 'broadcast' and (re.fullmatch(r'/live/[0-9a-fA-F]{32}', path) or re.fullmatch(r'/video/[0-9]+', path)):
            return path.lower(), query
    elif target == 'kick' and kind == 'broadcast' and not query:
        if (re.fullmatch(r'/(?:[A-Za-z0-9_-]+/videos|video)/' + _UUID, path)
                and (len(parts) == 2 or _channel_name(target, parts[0]))):
            return path, query
    elif target == 'custom':
        if set(query) <= {'id', 'v'} and all(re.fullmatch(r'[A-Za-z0-9._-]+', value) for value in query.values()):
            return path or '/', query
    if target in ('twitch', 'facebook', 'instagram', 'naver', 'kick') and kind == 'channel' and not query:
        if len(parts) == 1 and _channel_name(target, parts[0]):
            return path, query
    raise WatchLinkError()


def normalize_watch_url(target, value='', kind='channel'):
    """Canonical HTTPS viewing URL or empty string; errors never echo inputs."""
    if not isinstance(target, str) or target not in STREAM_TARGETS or kind not in ('channel', 'broadcast') or not isinstance(value, str):
        raise WatchLinkError()
    if len(value) > 2048 or any(ord(char) < 32 or 127 <= ord(char) <= 159 or char == '\\' for char in value):
        raise WatchLinkError()
    value = value.strip()
    if not value:
        return ''
    if target == 'local' or len(value) > 2048 or '#' in value or re.search(r'%(?![A-Fa-f0-9]{2})', value):
        raise WatchLinkError()
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not re.fullmatch(r'[A-Za-z0-9.-]+(?::443)?', parsed.netloc):
            raise WatchLinkError()
        host = parsed.hostname
        if target == 'custom':
            if not _HOST.fullmatch(host or '') or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home')):
                raise WatchLinkError()
            if re.fullmatch(r'[0-9.]+', host):
                public_ipv4(host)
            elif any(label.startswith('0x') for label in host.split('.')):
                raise WatchLinkError()
        elif host not in _HOSTS[target]:
            raise WatchLinkError()
        segments = [unquote(part, errors='strict') for part in parsed.path.split('/')]
        if any(part in ('.', '..') or _unsafe(part) for part in segments):
            raise WatchLinkError()
        path = '/'.join(segments).rstrip('/')
        if target != 'custom' and any(not part for part in path.split('/')[1:]):
            raise WatchLinkError()
        if parsed.query and any(not part for part in parsed.query.split('&')):
            raise WatchLinkError()
        query, seen = {}, set()
        for key, item in parse_qsl(parsed.query, keep_blank_values=True, errors='strict', max_num_fields=20):
            if key in seen or _unsafe(key) or _unsafe(item):
                raise WatchLinkError()
            seen.add(key)
            if key not in _TRACKING:
                query[key] = item
        path, query = _platform_path(target, kind, host, path, query)
        encoded = quote(path, safe='/@._-')
        result = 'https://' + _CANONICAL_HOSTS.get(target, host) + encoded
        if query:
            result += '?' + urlencode(sorted(query.items()))
        if len(result) > 2048:
            raise WatchLinkError()
        return result
    except (ValueError, TypeError, KeyError, StreamTargetError):
        raise WatchLinkError() from None


def normalize_watch_links(target, *, channel_url='', broadcast_url=''):
    return {'channel_url': normalize_watch_url(target, channel_url, 'channel'),
            'broadcast_url': normalize_watch_url(target, broadcast_url, 'broadcast')}
