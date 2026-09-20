"""Private R2 binding behind the Cloudflare Worker transfer gateway.

Reuse the bounded HTTP bridge/stream-integrity implementation, but accept only
our fixed Cloudflare origin and object capabilities. No Vercel calls or Blob
credentials are used by this adapter.
"""
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .blob_storage import VercelBlobStorage, _https_url
from .storage import StorageError


class R2Storage(VercelBlobStorage):
    # Stay below Cloudflare's 100 MB HTTP request ceiling, including output.
    max_put_bytes = 64 * 1024**2

    def __init__(self, control_url, control_token, *, client=None, prefix='replay', clock=time.monotonic):
        super().__init__(control_url, control_token, client=client, prefix=prefix, max_put_bytes=self.max_put_bytes)
        control = urlsplit(control_url)
        self.origin = (control.scheme, control.netloc)
        self._clock = clock
        self._health_until = 0
        self._health_lock = threading.Lock()

    def _signed(self, value, key, method, expires, *, size=None, content_type=None):
        if (not isinstance(value, dict) or value.get('method') != method
                or value.get('object_key', key) != key or not isinstance(value.get('headers'), dict)
                or isinstance(value.get('expires_in'), bool) or not isinstance(value.get('expires_in'), int)
                or not 1 <= value['expires_in'] <= expires):
            raise StorageError('Invalid R2 object authorization')
        parsed = _https_url(value.get('url'))
        if (parsed.scheme, parsed.netloc) != self.origin or parsed.path != '/objects/' + key:
            raise StorageError('R2 transfer origin or object does not match')
        try:
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        except ValueError:
            raise StorageError('Invalid R2 object authorization') from None
        if (set(query) != {'grant'} or len(query['grant']) != 1 or len(query['grant'][0]) > 2048
                or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}', query['grant'][0])):
            raise StorageError('Invalid R2 object authorization')
        clean = {'Content-Type': content_type, 'Content-Length': str(size)} if method == 'PUT' else {}
        if value['headers'] != clean:
            raise StorageError('R2 transfer constraints do not match')
        return {'url': value['url'], 'method': method, 'headers': clean, 'expires_in': value['expires_in'],
                **({'object_key': key} if method == 'PUT' else {})}

    def health(self):
        # Concurrent browser polls share a bounded successful probe, not a GET
        # of a video or a fresh provider operation every three seconds.
        with self._health_lock:
            if self._clock() < self._health_until:
                return True
            result = super().health()
            self._health_until = self._clock() + 60
            return result
