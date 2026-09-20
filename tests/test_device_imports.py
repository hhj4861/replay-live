import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import jwt
import pytest
from fastapi.testclient import TestClient

from server.device_import_worker import cloud_origin, run_device_import
from server.device_imports import tasks
from server.local_import_daemon import LocalImports, create_local_import_app
from server.media_sources import SourceImportError
from server.repository import object_deletions
from sqlalchemy import select, update
from test_commercial_api import commercial, run_claimed
from test_google_api import google_api, components, signing_key, exchange
from test_poc import clip


def ticket(client, **changes):
    result = client.post('/api/device-imports', json={
        'provider': 'direct', 'url': 'https://media.example/owned.mp4', 'name': '내 PC 영상.mp4', **changes})
    assert result.status_code == 201, result.text
    return result.json()


def headers(grant):
    return {'Authorization': 'Bearer ' + grant['token']}


def metadata(clip):
    return {'bytes': clip.stat().st_size, 'sha256': hashlib.sha256(clip.read_bytes()).hexdigest()}


def test_pc_gets_cloud_task_and_uploads_directly_then_real_validation(commercial, clip, tmp_path):
    cloud, repo, _ = commercial
    grant = ticket(cloud)
    assert cloud.get('/api/media').json() == []
    assert repo.list_jobs('alpha') == []  # Never enqueue a cloud source downloader.
    seen = []
    def downloaded(source, output, **limits):
        seen.append(source)
        limits['check_active']()
        output.write_bytes(clip.read_bytes())
        return metadata(clip)
    manager = LocalImports(root=tmp_path, pairing_code='PC-A', downloader=downloaded,
        cloud_url='http://testserver', cloud_client=TestClient(cloud.app), development=True)
    with TestClient(create_local_import_app(manager=manager), base_url='http://127.0.0.1:17833',
            headers={'Origin': 'https://replay-live.pages.dev', 'X-Replay-Local': '1'}) as local:
        pair = local.post('/pair', json={'code': 'PC-A'}).json()
        assert 'cloud-direct-upload' in pair['features']
        local_headers = {'Authorization': 'Bearer ' + pair['token']}
        created = local.post('/cloud-imports', headers=local_headers,
                             json={'id': grant['id'], 'token': grant['token']})
        assert created.status_code == 202, created.text
        for _ in range(200):
            result = local.get('/imports/' + grant['id'], headers=local_headers).json()
            if result['state'] not in ('downloading', 'cancelling'):
                break
            time.sleep(.02)
        assert result['state'] == 'ready', result
        assert result['media_id'] == grant['id']
        assert 'bytes' not in result and 'sha256' not in result
        assert local.get('/imports/' + grant['id'] + '/file', headers=local_headers).status_code == 409
        assert not list(manager.root.rglob('*.mp4'))
        another = local.post('/pair', json={'code': 'PC-A'}).json()
        assert local.get('/imports/' + grant['id'], headers={'Authorization': 'Bearer ' + another['token']}).status_code == 404
        assert local.post('/cloud-imports', headers={'Authorization': 'Bearer ' + another['token']},
                          json={'id': grant['id'], 'token': grant['token']}).status_code == 409
    assert seen == [{'provider': 'direct', 'url': 'https://media.example/owned.mp4'}]
    assert cloud.get('/api/device-imports/' + grant['id']).json()['state'] == 'completed'
    assert [item['target'] for item in repo.list_jobs('alpha')] == ['validate']
    run_claimed(cloud)
    item = cloud.get('/api/media').json()[0]
    assert item['id'] == grant['id'] and item['status'] == 'ready'
    preview = cloud.get('/api/media/' + item['id'] + '/preview').json()
    assert cloud.get(preview['url']).content == clip.read_bytes()
    assert cloud.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []


def test_capability_cannot_access_other_task_or_general_apis(commercial):
    client, repo, _ = commercial
    grant = ticket(client)
    path = '/api/device-imports/' + grant['id']
    assert client.get(path + '/task', headers=headers(grant)).status_code == 200
    assert client.get(path + '/task').status_code == 401
    assert client.get('/api/media', headers=headers(grant)).status_code == 401
    assert client.get('/api/device-imports/' + 'f' * 32 + '/task', headers=headers(grant)).status_code == 401
    assert client.get(path, headers={'Authorization': 'Bearer beta'}).status_code == 404
    assert client.delete(path, headers={'Authorization': 'Bearer beta'}).status_code == 404
    assert client.post('/internal/claim', headers=headers(grant), json={'worker_id': 'pc', 'version': 'integration-test'}).status_code == 401
    with repo._read() as conn:
        row = dict(conn.execute(select(tasks)).mappings().one())
    assert row['token_hash'] != grant['token']
    assert 'https://media.example' not in row['source_ciphertext']


def test_viewer_cannot_delegate_and_only_one_active_task_per_user(commercial):
    client, _, _ = commercial
    body = {'provider': 'direct', 'url': 'https://media.example/owned.mp4'}
    assert client.post('/api/device-imports', json=body, headers={'Authorization': 'Bearer viewer'}).status_code == 403
    first = ticket(client)
    assert client.post('/api/device-imports', json=body).status_code == 409
    assert client.post('/api/device-imports', json=body, headers={'Authorization': 'Bearer beta'}).status_code == 201
    assert client.delete('/api/device-imports/' + first['id']).status_code == 204
    assert client.post('/api/device-imports', json=body).status_code == 201


def test_logout_revokes_delegated_download_and_upload(commercial, clip):
    client, _, _ = commercial
    grant = ticket(client)
    assert client.post('/api/logout').status_code == 200
    for suffix in ('/task', '/upload', '/complete'):
        result = (client.get('/api/device-imports/' + grant['id'] + suffix, headers=headers(grant)) if suffix == '/task'
                  else client.post('/api/device-imports/' + grant['id'] + suffix, headers=headers(grant), json=metadata(clip)))
        assert result.status_code == 401


def test_cancel_and_expiry_prevent_upload(commercial, clip):
    client, _, _ = commercial
    grant = ticket(client)
    path = '/api/device-imports/' + grant['id']
    claims = jwt.decode(grant['token'], options={'verify_signature': False})
    claims['exp'] = time.time() - 1
    expired = {'Authorization': 'Bearer ' + jwt.encode(claims, 's' * 32, algorithm='HS256')}
    assert client.get(path + '/task', headers=expired).status_code == 401
    assert client.delete(path).status_code == 204
    assert client.post(path + '/upload', headers=headers(grant), json=metadata(clip)).status_code == 409


def test_signed_upload_is_idempotent_immutable_and_verified(commercial, clip):
    client, repo, _ = commercial
    grant = ticket(client)
    path = '/api/device-imports/' + grant['id']
    meta = metadata(clip)
    for _ in range(2):
        signed = client.post(path + '/upload', headers=headers(grant), json=meta)
        assert signed.status_code == 200, signed.text
    assert len(client.get('/api/media').json()) == 1
    assert client.post(path + '/upload', headers=headers(grant), json={**meta, 'sha256': '0' * 64}).status_code == 409
    assert client.post(path + '/upload', headers=headers(grant), json={**meta, 'bytes': 50 * 1024**2 + 1}).status_code == 422
    assert client.post(path + '/complete', headers=headers(grant)).status_code != 200
    upload = signed.json()
    assert client.put(upload['url'], headers=upload['headers'], content=clip.read_bytes()).status_code == 200
    for _ in range(2):
        result = client.post(path + '/complete', headers=headers(grant))
        assert result.status_code == 200, result.text
    assert len(repo.list_jobs('alpha')) == 1


def test_failed_pc_reports_failure_without_cloud_download(commercial, tmp_path):
    client, repo, _ = commercial
    grant = ticket(client)
    def denied(*args, **kwargs):
        raise SourceImportError('SOURCE_BOT_CHECK_REQUIRED')
    with pytest.raises(SourceImportError, match='SOURCE_BOT_CHECK_REQUIRED'):
        run_device_import(grant['id'], grant['token'], api='http://testserver', output=tmp_path / 'file.mp4',
            downloader=denied, check_active=lambda: None, on_phase=lambda _: None,
            client=TestClient(client.app), development=True)
    result = client.get('/api/device-imports/' + grant['id']).json()
    assert result['state'] == 'failed' and result['error_code'] == 'SOURCE_BOT_CHECK_REQUIRED'
    assert repo.list_jobs('alpha') == []


@pytest.mark.parametrize('revoke', ['logout', 'refresh', 'disable'])
def test_google_live_session_is_rechecked_for_delegated_tasks(google_api, signing_key, revoke):
    from server.google_auth import identities
    client, auth, _ = google_api
    session, _, _ = exchange(client, signing_key)
    client.headers['Authorization'] = 'Bearer ' + session.json()['token']
    grant = ticket(client)
    path = '/api/device-imports/' + grant['id'] + '/task'
    assert client.get(path, headers=headers(grant)).status_code == 200
    if revoke == 'disable':
        with auth.engine.begin() as conn:
            conn.execute(update(identities).values(enabled=False))
    else:
        endpoint = '/api/logout' if revoke == 'logout' else '/api/auth/google/refresh'
        assert client.post(endpoint, json={}).status_code == 200
    assert client.get(path, headers=headers(grant)).status_code in (401, 403)


def test_cancel_racing_signed_upload_cleans_media_reservation(commercial, clip, monkeypatch):
    client, repo, _ = commercial
    grant = ticket(client)
    path = '/api/device-imports/' + grant['id']
    storage = client.app.state.storage
    original = storage.presign_upload
    entered, resume = Event(), Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(storage, 'presign_upload', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, path + '/upload', headers=headers(grant), json=metadata(clip))
        try:
            assert entered.wait(5)
            assert client.delete(path).status_code == 204
        finally:
            resume.set()
        assert pending.result().status_code == 409
    assert client.get('/api/media').json() == []
    with repo._read() as conn:
        deletion = conn.execute(select(object_deletions)).mappings().one()
    assert deletion['not_before'] > time.time()  # Signed PUT must expire before deletion.


def test_expiry_cleans_abandoned_tasks_but_preserves_admitted_video(commercial, clip):
    client, repo, _ = commercial
    abandoned = ticket(client)
    with repo.engine.begin() as conn:
        conn.execute(update(tasks).values(expires_at=1))
    response = client.post('/internal/maintenance', headers={'Authorization': 'Bearer ' + 'c' * 32})
    assert response.status_code == 200, response.text
    assert response.json()['expired_device_imports'] == 1
    with repo._read() as conn:
        assert not conn.execute(select(tasks.c.source_ciphertext)).scalar_one_or_none()
    grant = ticket(client)
    path = '/api/device-imports/' + grant['id']
    signed = client.post(path + '/upload', headers=headers(grant), json=metadata(clip)).json()
    assert client.put(signed['url'], headers=signed['headers'], content=clip.read_bytes()).status_code == 200
    assert client.post(path + '/complete', headers=headers(grant)).status_code == 200
    # Simulate a process dying after validation admission but before its reply.
    with repo.engine.begin() as conn:
        conn.execute(update(tasks).where(tasks.c.id == grant['id']).values(state='completing', expires_at=1))
    ticket(client)
    assert repo.get_media('alpha', grant['id'])['status'] == 'validating'
    run_claimed(client)
    assert repo.get_media('alpha', grant['id'])['status'] == 'ready'


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:pass@example.com',
    'https://example.com/api', 'https://example.com?token=secret', 'file:///tmp/foo'])
def test_daemon_fixed_cloud_origin_rejects_unsafe_configuration(url):
    with pytest.raises(ValueError):
        cloud_origin(url)


@pytest.mark.parametrize(('failure', 'code'), [
    ('/task', 'DEVICE_IMPORT_TASK_FAILED'),
    ('/upload', 'DEVICE_IMPORT_UPLOAD_FAILED'),
    ('storage', 'DEVICE_IMPORT_UPLOAD_FAILED'),
    ('/complete', 'DEVICE_IMPORT_COMPLETE_FAILED'),
    ('revoked', 'DEVICE_IMPORT_REVOKED'),
    ('bad-destination', 'DEVICE_IMPORT_INVALID'),
    ('vercel-lookalike', 'DEVICE_IMPORT_INVALID'),
    (None, None),
])
def test_device_worker_preserves_stage_errors_and_accepts_official_blob_put(tmp_path, failure, code):
    import httpx
    data = b'bounded synthetic recording'
    digest = hashlib.sha256(data).hexdigest()
    task_id = 'a' * 32
    calls, reported = [], []
    def transport(request):
        calls.append(request)
        path = request.url.path
        if path.endswith('/failure'):
            import json
            reported.append(json.loads(request.content)['code'])
            return httpx.Response(200, json={})
        if failure == 'revoked' and path.endswith('/task'):
            return httpx.Response(401, json={'detail': 'secret must not escape'})
        if failure in ('/task', '/upload', '/complete') and path.endswith(failure):
            return httpx.Response(400 if failure == '/upload' else 502, json={'detail': 'secret must not escape'})
        if path.endswith('/task'):
            return httpx.Response(200, json={'id': task_id, 'state': 'queued',
                'source': {'provider': 'direct', 'url': 'https://media.example/recording.mp4'},
                'max_bytes': 1024, 'max_duration': 120, 'expires_at': time.time() + 300})
        if path.endswith('/upload'):
            url = 'https://vercel.com/api/blob/?pathname=one-file&vercel-blob-signature=synthetic'
            if failure == 'bad-destination': url = 'https://vercel.com/unrelated-endpoint'
            if failure == 'vercel-lookalike': url = 'https://vercel.com.evil.example/api/blob/'
            return httpx.Response(200, json={'url': url, 'method': 'PUT',
                'headers': {'Content-Type': 'video/mp4', 'Content-Length': str(len(data))}})
        if request.url.host == 'vercel.com':
            assert request.method == 'PUT' and request.content == data
            assert 'authorization' not in request.headers
            return httpx.Response(503 if failure == 'storage' else 200)
        if path.endswith('/complete'):
            return httpx.Response(200, json={'media': {'id': task_id}})
        raise AssertionError('Unexpected destination')
    def download(source, output, **kwargs):
        output.write_bytes(data)
        return {'bytes': len(data), 'sha256': digest}
    phases = []
    output = tmp_path / 'recording.mp4'
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        args = dict(api='https://api.example', output=output, downloader=download,
                    check_active=lambda: None, on_phase=phases.append, client=client)
        if code:
            with pytest.raises(SourceImportError) as error:
                run_device_import(task_id, 'single-task-capability', **args)
            assert error.value.code == code and str(error.value) == code
            assert reported == [code]
        else:
            assert run_device_import(task_id, 'single-task-capability', **args) == {'media_id': task_id}
            assert phases == ['requesting', 'downloading', 'uploading', 'validating'] and not reported
    assert not output.exists()
    if failure == '/complete':
        assert sum(r.url.path.endswith('/complete') for r in calls) == 3
