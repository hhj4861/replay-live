import asyncio
import json
import logging

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from server.request_boundary import BoundaryMiddleware, ScopedRateLimiter


def test_actual_bytes_enforced_even_when_framework_catches_parse_error():
    app = FastAPI()
    @app.post('/payload')
    async def payload(request: Request):
        return await request.json()
    with TestClient(BoundaryMiddleware(app, max_body_bytes=8)) as client:
        response = client.post('/payload', content=b'{"large":"body"}', headers={'Content-Length': '1'})
        assert response.status_code == 413
        assert response.headers['x-request-id'] == response.json()['request_id']


def test_chunked_limits_timeouts_and_ambiguous_headers():
    async def scenario(headers, chunks, wait=0):
        sent = []
        async def app(scope, receive, send):
            while (await receive()).get('more_body', False):
                pass
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        async def receive():
            await asyncio.sleep(wait)
            return chunks.pop(0)
        async def send(message):
            sent.append(message)
        await BoundaryMiddleware(app, max_body_bytes=4, body_timeout=.02)(
            {'type': 'http', 'method': 'POST', 'path': '/payload', 'headers': headers}, receive, send)
        return sent[0]['status']
    chunks = [{'type': 'http.request', 'body': b'ab', 'more_body': True}, {'type': 'http.request', 'body': b'cde', 'more_body': False}]
    assert asyncio.run(scenario([(b'transfer-encoding', b'chunked')], chunks)) == 413
    assert asyncio.run(scenario([(b'content-length', b'1'), (b'transfer-encoding', b'chunked')], [])) == 400
    assert asyncio.run(scenario([], [{'type': 'http.request', 'body': b'a'}], wait=.05)) == 408


def test_path_limit_request_id_and_structured_logs_do_not_capture_secrets(caplog):
    app = FastAPI()
    @app.post('/uploads/{item_id}')
    async def upload(item_id: str, request: Request):
        return {'bytes': len(await request.body()), 'request_id': request.state.request_id}
    logger = logging.getLogger('replay.boundary-test')
    with caplog.at_level(logging.INFO, logger=logger.name):
        with TestClient(BoundaryMiddleware(app, max_body_bytes=4, path_limits={'/uploads/synthetic-private-id': 32}, logger=logger)) as client:
            response = client.post('/uploads/synthetic-private-id?token=private-query', content=b'private-body',
                                   headers={'Authorization': 'Bearer private-header', 'X-Request-ID': 'untrusted-client-value'})
    assert response.status_code == 200 and response.json()['bytes'] == 12
    assert response.headers['x-request-id'] == response.json()['request_id']
    assert response.headers['x-request-id'] != 'untrusted-client-value'
    messages = [record.message for record in caplog.records if record.name == logger.name]
    record = json.loads(messages[0])
    assert record['route'] == '/uploads/{item_id}' and record['status'] == 200
    assert all(value not in messages[0] for value in ('private-query', 'private-body', 'private-header', 'synthetic-private-id'))


def test_limiter_scopes_clients_bounds_memory_and_expires():
    now = [0.0]
    limiter = ScopedRateLimiter(limit=2, window=10, max_clients=3, clock=lambda: now[0])
    limiter.record('attacker')
    limiter.record('attacker')
    assert not limiter.check('attacker') and limiter.check('customer')
    for index in range(10):
        limiter.record(f'client-{index}')
    assert limiter.client_count == 3
    now[0] = 11
    assert limiter.client_count == 0 and limiter.check('attacker')


def test_body_timeout_does_not_interrupt_stream_disconnect_after_body_is_complete():
    async def scenario():
        sent = []
        calls = 0
        async def receive():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await asyncio.sleep(.02)
            return {'type': 'http.disconnect'}
        async def app(scope, receive, send):
            assert (await receive())['type'] == 'http.request'
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            assert (await receive())['type'] == 'http.disconnect'
            await send({'type': 'http.response.body', 'body': b'complete'})
        async def send(message):
            sent.append(message)
        await BoundaryMiddleware(app, body_timeout=.01)(
            {'type': 'http', 'method': 'GET', 'path': '/stream', 'headers': []}, receive, send)
        assert sent[-1]['body'] == b'complete'
    asyncio.run(scenario())
