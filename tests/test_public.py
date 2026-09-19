from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
import pytest
from server.app import create_app
from server.public_access import PUBLIC_UPLOAD, PUBLIC_DURATION
from test_poc import clip, until

ORIGIN = 'https://replay.example'
HEADERS = {'X-Replay-Client': '1', 'Origin': ORIGIN}


@pytest.fixture
def public_client(tmp_path):
    app = create_app(tmp_path / 'public', public_origins=[ORIGIN], invite_code='test-invite-code-only')
    with TestClient(app, headers=HEADERS) as client:
        yield client


def login(client):
    response = client.post('/api/session', json={'code': 'test-invite-code-only'})
    assert response.status_code == 200, response.text
    return {'Authorization': 'Bearer ' + response.json()['token']}


def test_public_isolation_and_real_stream(public_client, clip):
    client = public_client
    assert client.get('/api/health').status_code == 401
    assert client.post('/api/session', json={'code': 'wrong'}).status_code == 401
    owner_a, owner_b = login(client), login(client)
    result = client.post('/api/media', files={'file': ('clip.mp4', clip.read_bytes())}, headers=owner_a)
    assert result.status_code == 201, result.text
    media_id = result.json()['id']
    assert client.get('/api/media', headers=owner_b).json() == []
    assert client.get(f'/api/media/{media_id}/preview', headers=owner_b).status_code == 404
    assert client.get(f'/api/media/{media_id}/preview', headers=owner_a).content == clip.read_bytes()
    payload = {'media_id': media_id, 'title': '외부 격리 테스트'}
    assert client.post('/api/broadcasts', json=payload, headers=owner_b).status_code == 400
    response = client.post('/api/broadcasts', json=payload, headers=owner_a)
    assert response.status_code == 201, response.text
    job_id = response.json()['id']
    for suffix in ('', '/events', '/output'):
        assert client.get(f'/api/broadcasts/{job_id}{suffix}', headers=owner_b).status_code == 404
    assert client.post(f'/api/broadcasts/{job_id}/stop', headers=owner_b).status_code == 404
    assert client.get('/api/broadcasts', headers=owner_b).json() == []
    client.headers.update(owner_a)
    assert until(client, job_id, {'completed', 'failed'})['state'] == 'completed'
    assert client.get(f'/api/broadcasts/{job_id}/output').content.startswith(b'FLV')


def test_origins_limits_expiry(public_client, clip):
    client = public_client
    auth = login(client)
    preflight = client.options('/api/media', headers={'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,x-replay-client'})
    assert preflight.status_code == 200 and preflight.headers['access-control-allow-origin'] == ORIGIN
    assert client.get('/api/media', headers={**auth, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/media', content=b'', headers={**auth, 'Content-Length': str(PUBLIC_UPLOAD + 2 * 1024 * 1024)}).status_code == 413
    health = client.get('/api/health', headers=auth).json()
    assert health['max_upload_mb'] == 50 and health['max_duration_seconds'] == PUBLIC_DURATION
    for _ in range(3):
        assert client.post('/api/media', files={'file': ('clip.mp4', clip.read_bytes())}, headers=auth).status_code == 201
    assert client.post('/api/media', files={'file': ('clip.mp4', clip.read_bytes())}, headers=auth).status_code == 400
    service = client.app.state.access.authenticate(auth['Authorization'])
    media_id = service.list_media()[0]['id']
    payload = {'media_id': media_id, 'title': 'quota', 'scheduled_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    for _ in range(5):
        assert client.post('/api/broadcasts', json=payload, headers=auth).status_code == 201
    assert client.post('/api/broadcasts', json=payload, headers=auth).status_code == 400
    for digest in client.app.state.access.sessions:
        client.app.state.access.sessions[digest] = 0
    assert client.get('/api/media', headers=auth).status_code == 401


def test_sessions_share_single_streaming_slot(public_client, clip):
    client = public_client
    sessions = []
    for _ in range(2):
        auth = login(client)
        media = client.post('/api/media', files={'file': ('clip.mp4', clip.read_bytes())}, headers=auth).json()
        sessions.append((auth, media['id']))
    first_auth, first_media = sessions[0]
    second_auth, second_media = sessions[1]
    first = client.post('/api/broadcasts', json={'media_id': first_media, 'title': 'first'}, headers=first_auth).json()
    client.headers.update(first_auth)
    until(client, first['id'], {'starting', 'streaming'})
    second = client.post('/api/broadcasts', json={'media_id': second_media, 'title': 'second'}, headers=second_auth).json()
    import time
    time.sleep(.5)
    assert client.get(f"/api/broadcasts/{second['id']}", headers=second_auth).json()['state'] == 'scheduled'
    assert until(client, first['id'], {'completed', 'failed'})['state'] == 'completed'
    client.headers.update(second_auth)
    assert until(client, second['id'], {'completed', 'failed'})['state'] == 'completed'


def test_bodyless_stop_through_proxy(public_client, clip):
    client = public_client
    auth = login(client)
    media = client.post('/api/media', files={'file': ('clip.mp4', clip.read_bytes())}, headers=auth).json()
    job = client.post('/api/broadcasts', json={'media_id': media['id'], 'title': 'cancel', 'scheduled_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}, headers=auth).json()
    request = client.build_request('POST', f"/api/broadcasts/{job['id']}/stop", headers=auth)
    del request.headers['Content-Length']
    response = client.send(request)
    assert response.status_code == 200 and response.json()['state'] == 'stopped'


def test_shared_sample_existing_new_sessions_and_restart(tmp_path, clip):
    root = tmp_path / 'public'
    def app():
        return create_app(root, public_origins=[ORIGIN], invite_code='test-invite-code-only')

    # A session created before sample installation must receive it after restart.
    with TestClient(app(), headers=HEADERS) as client:
        owner_a = login(client)
        private = client.post('/api/media', files={'file': ('sample.mp4', clip.read_bytes())}, headers=owner_a).json()
    (root / 'shared').mkdir()
    (root / 'shared' / 'sample.mp4').write_bytes(clip.read_bytes())
    with TestClient(app(), headers=HEADERS) as client:
        owner_b = login(client)
        sample = client.get('/api/media', headers=owner_b).json()[0]
        assert sample['name'] == 'sample.mp4'
        assert 'path' not in sample
        for auth in (owner_a, owner_b):
            assert sample in client.get('/api/media', headers=auth).json()
            assert client.get(f"/api/media/{sample['id']}/preview", headers=auth).content == clip.read_bytes()
        assert client.get(f"/api/media/{sample['id']}/preview").status_code == 401
        assert client.get(f"/api/media/{private['id']}/preview", headers=owner_b).status_code == 404
        for _ in range(2):
            assert client.post('/api/media', files={'file': ('private.mp4', clip.read_bytes())}, headers=owner_a).status_code == 201
        assert client.post('/api/media', files={'file': ('extra.mp4', clip.read_bytes())}, headers=owner_a).status_code == 400
        response = client.post('/api/broadcasts', json={'media_id': sample['id'], 'title': 'shared sample'}, headers=owner_b)
        assert response.status_code == 201
        job = response.json()
        assert client.get(f"/api/broadcasts/{job['id']}", headers=owner_a).status_code == 404
        client.headers.update(owner_b)
        assert until(client, job['id'], {'completed', 'failed'})['state'] == 'completed'
        assert client.get(f"/api/broadcasts/{job['id']}/output").content.startswith(b'FLV')
    with TestClient(app(), headers=HEADERS) as client:
        assert client.get('/api/media', headers=owner_b).json() == [sample]
        assert len(client.get('/api/media', headers=owner_a).json()) == 4


def test_session_limit_counts_only_unexpired_sessions(tmp_path, monkeypatch):
    import hashlib
    from server import public_access
    monkeypatch.setattr(public_access, 'MAX_ACTIVE_SESSIONS', 2)
    root = tmp_path / 'public'
    def app():
        return create_app(root, public_origins=[ORIGIN], invite_code='test-invite-code-only')

    with TestClient(app(), headers=HEADERS) as client:
        expired_owner, active_owner = login(client), login(client)
        assert client.post('/api/session', json={'code': 'test-invite-code-only'}).status_code == 429
        access = client.app.state.access
        digest = hashlib.sha256(expired_owner['Authorization'].removeprefix('Bearer ').encode()).hexdigest()
        access.sessions[digest] = 0
        replacement = login(client)
        assert len(access.sessions) == 3  # Historical records do not consume active slots.
        assert client.get('/api/media', headers=expired_owner).status_code == 401
        assert client.get('/api/media', headers=active_owner).status_code == 200
        assert client.get('/api/media', headers=replacement).status_code == 200
        assert (root / digest / 'replay.sqlite3').exists()
        assert client.post('/api/session', json={'code': 'test-invite-code-only'}).status_code == 429
    with TestClient(app(), headers=HEADERS) as client:
        assert client.get('/api/media', headers=expired_owner).status_code == 401
        assert client.get('/api/media', headers=active_owner).status_code == 200
        assert client.get('/api/media', headers=replacement).status_code == 200
        assert client.post('/api/session', json={'code': 'test-invite-code-only'}).status_code == 429
