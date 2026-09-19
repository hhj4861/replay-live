"""ASGI ingress limits and request correlation without body/header/secret logging."""
import asyncio
from collections import OrderedDict, deque
import hashlib
import json
import logging
import math
import threading
import time
import uuid


class ScopedRateLimiter:
    """Bounded, per-client sliding windows. Shared deployment limits belong at ingress."""
    def __init__(self, limit=20, window=60.0, max_clients=4096, clock=time.monotonic):
        if limit < 1 or window <= 0 or max_clients < 1:
            raise ValueError('Rate limiter configuration must be positive')
        self.limit, self.window, self.max_clients, self.clock = limit, window, max_clients, clock
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now):
        # Ordered by most recent activity, so old clients are removed first.
        while self._entries:
            key, values = next(iter(self._entries.items()))
            if values and values[-1] > now - self.window:
                break
            self._entries.pop(key)

    def check(self, client_key):
        key = hashlib.sha256(str(client_key).encode()).digest()
        with self._lock:
            now = self.clock()
            self._prune(now)
            values = self._entries.get(key)
            if values is None:
                return True
            while values and values[0] <= now - self.window:
                values.popleft()
            return len(values) < self.limit

    def record(self, client_key):
        key = hashlib.sha256(str(client_key).encode()).digest()
        with self._lock:
            now = self.clock()
            self._prune(now)
            values = self._entries.setdefault(key, deque(maxlen=self.limit))
            values.append(now)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_clients:
                self._entries.popitem(last=False)

    def clear(self, client_key):
        key = hashlib.sha256(str(client_key).encode()).digest()
        with self._lock:
            self._entries.pop(key, None)

    @property
    def client_count(self):
        with self._lock:
            self._prune(self.clock())
            return len(self._entries)


class _BodyViolation(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail


class BoundaryMiddleware:
    def __init__(self, app, *, max_body_bytes=1024 * 1024, path_limits=None, body_timeout=30.0, logger=None):
        if max_body_bytes < 0 or not math.isfinite(body_timeout) or body_timeout <= 0:
            raise ValueError('Invalid request boundary limits')
        self.app, self.max_body_bytes = app, max_body_bytes
        self.path_limits = dict(path_limits or {})
        if any(not isinstance(value, int) or value < 0 for value in self.path_limits.values()):
            raise ValueError('Path body limits must be nonnegative byte counts')
        self.body_timeout = body_timeout
        self.logger = logger or logging.getLogger('replay.requests')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        scope.setdefault('state', {})['request_id'] = request_id
        maximum = self.path_limits.get(scope.get('path', ''), self.max_body_bytes)
        received = 0
        status = 500
        response_started = False
        response_finished = False
        body_finished = False
        violation = None

        async def reject(problem):
            nonlocal status, response_started, response_finished
            if response_finished:
                return
            if response_started:
                await send({'type': 'http.response.body', 'body': b'', 'more_body': False})
            else:
                status = problem.status
                body = json.dumps({'detail': problem.detail, 'request_id': request_id}, ensure_ascii=False).encode()
                await send({'type': 'http.response.start', 'status': status, 'headers': [
                    (b'content-type', b'application/json; charset=utf-8'),
                    (b'content-length', str(len(body)).encode()), (b'cache-control', b'no-store'),
                    (b'x-request-id', request_id.encode()), (b'x-content-type-options', b'nosniff')]})
                response_started = True
                await send({'type': 'http.response.body', 'body': body})
            response_finished = True

        async def limited_receive():
            nonlocal received, violation, body_finished
            if body_finished:
                return await receive()
            remaining = self.body_timeout - (time.monotonic() - started)
            try:
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError:
                violation = _BodyViolation(408, '요청 본문 수신 시간이 초과되었습니다.')
                raise violation from None
            if message['type'] == 'http.request':
                received += len(message.get('body', b''))
                if received > maximum:
                    violation = _BodyViolation(413, '요청 본문이 허용된 크기를 초과했습니다.')
                    raise violation
                body_finished = not message.get('more_body', False)
            return message

        async def correlated_send(message):
            nonlocal status, response_started, response_finished
            # Some frameworks turn body-read errors into their own 400 response.
            # Preserve the ingress status even when that error was caught inside.
            if violation:
                await reject(violation)
                return
            if message['type'] == 'http.response.start':
                status = message['status']
                response_started = True
                headers = [(key, value) for key, value in message.get('headers', []) if key.lower() != b'x-request-id']
                message = dict(message, headers=headers + [(b'x-request-id', request_id.encode())])
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                response_finished = True
            await send(message)

        try:
            lengths = [value for key, value in scope.get('headers', []) if key.lower() == b'content-length']
            encodings = [value for key, value in scope.get('headers', []) if key.lower() == b'transfer-encoding']
            if len(lengths) > 1 or (lengths and encodings):
                raise _BodyViolation(400, '모호한 요청 길이 헤더는 허용하지 않습니다.')
            if lengths:
                if not lengths[0].isdigit():
                    raise _BodyViolation(400, '요청 길이 헤더가 올바르지 않습니다.')
                if int(lengths[0]) > maximum:
                    raise _BodyViolation(413, '요청 본문이 허용된 크기를 초과했습니다.')
            await self.app(scope, limited_receive, correlated_send)
        except _BodyViolation as problem:
            await reject(problem)
        except Exception:
            await reject(_BodyViolation(500, '요청 처리 중 오류가 발생했습니다. 요청 ID로 문의하세요.'))
        finally:
            route = getattr(scope.get('route'), 'path', None)
            record = {'event': 'http_request', 'request_id': request_id, 'method': scope.get('method'),
                      'route': route or '<unmatched>', 'status': status,
                      'duration_ms': round((time.monotonic() - started) * 1000, 2), 'body_bytes': received}
            # Never log URL queries, raw paths, headers, request/response bodies or exceptions.
            self.logger.info(json.dumps(record, separators=(',', ':')))
