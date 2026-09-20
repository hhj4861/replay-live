import hashlib
import threading
import time
import uuid

from fastapi.testclient import TestClient
import pytest

from server.local_import_daemon import LocalImports, MAX_BYTES, create_local_import_app

ORIGIN = 'https://replay-live-poc.vercel.app'
SECOND_ORIGIN = 'http://127.0.0.1:13101'
CODE = 'SYNTHETIC-CODE'
VIDEO = b'\x00\x00\x00\x18ftypisom' + b'fixture' * 32


def downloaded(source, output, **kwargs):
    kwargs['check_active']()
    output.write_bytes(VIDEO)
    return {'bytes': len(VIDEO), 'sha256': hashlib.sha256(VIDEO).hexdigest()}


@pytest.fixture
def daemon(tmp_path):
    manager = LocalImports(root=tmp_path, downloader=downloaded, pairing_code=CODE)
    app = create_local_import_app(manager=manager)
    with TestClient(app, base_url='http://127.0.0.1:17833',
                    headers={'Origin': ORIGIN, 'X-Replay-Local': '1'}) as client:
        yield client, manager
    assert not manager.root.exists()


def pair(client, **kwargs):
    result = client.post('/pair', json={'code': CODE}, **kwargs)
    assert result.status_code == 200, result.text
    return {'Authorization': 'Bearer ' + result.json()['token']}


def payload(**values):
    return dict(request_id=str(uuid.uuid4()), provider='youtube', url='https://youtu.be/A5b469g7qDg',
                max_bytes=MAX_BYTES, max_duration=120, **values)


def wait_state(client, id, headers, wanted):
    for _ in range(100):
        result = client.get('/imports/' + id, headers=headers)
        if result.status_code == 200 and result.json()['state'] in wanted:
            return result.json()
        time.sleep(.01)
    raise AssertionError(result.text)


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Origin': ''},
    {'Host': 'evil.example'}, {'Host': 'localhost:17833'}, {'X-Replay-Local': ''},
])
def test_rejects_ambient_websites_rebinding_and_simple_requests(daemon, headers):
    client, manager = daemon
    response = client.post('/pair', json={'code': CODE}, headers=headers)
    assert response.status_code == 403
    assert not manager.sessions


def test_browser_preflight_and_limited_pair_attempts(daemon):
    client, _ = daemon
    response = client.options('/pair', headers={'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type,x-replay-local',
        'Access-Control-Request-Private-Network': 'true'})
    assert response.status_code == 204
    assert response.headers['access-control-allow-origin'] == ORIGIN
    assert response.headers['access-control-allow-private-network'] == 'true'
    for _ in range(5):
        assert client.post('/pair', json={'code': 'wrong'}).status_code == 401
    assert client.post('/pair', json={'code': CODE}).status_code == 429


def test_code_discovery_returns_current_code_without_creating_a_session(daemon):
    client, manager = daemon
    for code in (CODE, '123456ABCDEF'):
        manager.pairing_code = code
        response = client.get('/pairing-code')
        assert response.status_code == 200
        assert response.json() == {'code': code, 'version': 1}
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['access-control-allow-origin'] == ORIGIN
    assert not manager.sessions
    assert not manager.pair_attempts
    assert client.get('/session').status_code == 401


@pytest.mark.parametrize('headers', [
    {'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Origin': ''},
    {'Origin': ORIGIN + '.evil.example'}, {'Origin': ORIGIN + '/'},
    {'Host': 'evil.example'}, {'Host': 'localhost:17833'}, {'X-Replay-Local': ''},
])
def test_code_discovery_rejects_untrusted_or_simple_requests(daemon, headers):
    client, _ = daemon
    response = client.get('/pairing-code', headers=headers)
    assert response.status_code == 403
    assert CODE not in response.text


def test_code_discovery_preflight(daemon):
    client, _ = daemon
    response = client.options('/pairing-code', headers={
        'Access-Control-Request-Method': 'GET',
        'Access-Control-Request-Headers': 'x-replay-local',
        'Access-Control-Request-Private-Network': 'true'})
    assert response.status_code == 204
    assert response.headers['access-control-allow-origin'] == ORIGIN
    assert response.headers['access-control-allow-private-network'] == 'true'


def test_file_integrity_idempotency_and_session_isolation(daemon):
    client, manager = daemon
    owner = pair(client)
    other = pair(client)
    body = payload()
    created = client.post('/imports', json=body, headers=owner)
    assert created.status_code == 202
    id = created.json()['id']
    ready = wait_state(client, id, owner, {'ready'})
    assert ready['sha256'] == hashlib.sha256(VIDEO).hexdigest()
    assert client.post('/imports', json=body, headers=owner).json()['id'] == id
    assert client.post('/imports', json={**body, 'url': 'https://youtu.be/another'}, headers=owner).status_code == 409
    for suffix in ('', '/file'):
        assert client.get(f'/imports/{id}{suffix}', headers=other).status_code == 404
    assert client.delete('/imports/' + id, headers=other).status_code == 404
    assert client.get('/session', headers={**owner, 'Origin': SECOND_ORIGIN}).status_code == 401
    result = client.get('/imports/' + id + '/file', headers=owner)
    assert result.content == VIDEO
    assert result.headers['cache-control'] == 'no-store'
    assert client.delete('/imports/' + id, headers=owner).status_code == 204
    assert not list(manager.root.iterdir())


def test_unpaired_cloud_bearer_and_unsafe_urls_never_start(daemon):
    client, manager = daemon
    assert client.post('/imports', json=payload()).status_code == 401
    assert client.get('/session', headers={'Authorization': 'Bearer synthetic-cloud-token'}).status_code == 401
    owner = pair(client)
    for changes in ({'provider': 'direct', 'url': 'https://127.0.0.1/private'},
                    {'provider': 'youtube', 'url': 'https://evil.example/video'},
                    {'max_bytes': MAX_BYTES + 1}, {'max_bytes': True}, {'max_duration': 121}):
        assert client.post('/imports', json={**payload(), **changes}, headers=owner).status_code in {400, 422}
    assert not manager.jobs
    assert client.post('/pair', content=b'x' * 9000, headers={'Content-Type': 'application/json'}).status_code == 413


def test_cancel_and_disconnect_stop_active_download(tmp_path):
    started = threading.Event()

    def slow(source, output, **kwargs):
        started.set()
        while True:
            kwargs['check_active']()
            time.sleep(.005)

    manager = LocalImports(root=tmp_path, downloader=slow, pairing_code=CODE)
    with TestClient(create_local_import_app(manager=manager), base_url='http://127.0.0.1:17833',
                    headers={'Origin': ORIGIN, 'X-Replay-Local': '1'}) as client:
        owner = pair(client)
        id = client.post('/imports', json=payload(), headers=owner).json()['id']
        assert started.wait(1)
        assert client.post('/imports', json=payload(), headers=owner).status_code == 409
        assert client.delete('/session', headers=owner).status_code == 204
        assert client.get('/imports/' + id, headers=owner).status_code == 401
        for _ in range(100):
            with manager.lock:
                if not manager.jobs:
                    break
            time.sleep(.01)
        assert not manager.jobs
        assert not list(manager.root.iterdir())


def test_expiry_removes_abandoned_files(daemon):
    client, manager = daemon
    owner = pair(client)
    id = client.post('/imports', json=payload(), headers=owner).json()['id']
    wait_state(client, id, owner, {'ready'})
    future = time.time() + 1801
    manager.now = lambda: future
    manager.sweep()
    assert client.get('/imports/' + id, headers=owner).status_code == 404
    assert not list(manager.root.iterdir())
