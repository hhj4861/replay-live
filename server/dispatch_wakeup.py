"""Notify a durable dispatcher without exposing job data or tenant credentials."""
import threading
import time

import httpx


class DispatchWakeup:
    def __init__(self, settings, *, client=None, clock=time.monotonic):
        self.enabled = settings.dispatch_mode in ('queue', 'cloudflare')
        self.url = settings.dispatch_wakeup_url
        self._token = settings.control_token
        self._clock = clock
        self._last_health = float('-inf')
        self._lock = threading.Lock()
        self._owned = client is None
        self._client = client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)

    def notify(self, *, health=False):
        if not self.enabled:
            return True
        if health:
            with self._lock:
                now = self._clock()
                if now - self._last_health < 30:
                    return True
                self._last_health = now
        try:
            response = self._client.post(self.url, json={'v': 1}, headers={'Authorization': f'Bearer {self._token}'})
            return response.status_code == 202
        except (httpx.HTTPError, ValueError):
            return False

    def close(self):
        if self._owned:
            self._client.close()
