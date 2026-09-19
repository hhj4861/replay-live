import hashlib
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server.auth import Principal
from server.production_app import create_production_app
from server.repository import Repository
from server.secrets import LocalKeyringProvider
from server.settings import Settings
from server.storage import LocalStorage
from server.worker import run_job
from server.output_policy import estimate_output_bytes
from test_poc import clip


class TestAuth:
    __test__ = False
    async def authenticate(self, authorization, requested_tenant=None):
        tokens = {'Bearer alpha': ('alpha', 'operator'), 'Bearer beta': ('beta', 'operator'), 'Bearer viewer': ('alpha', 'viewer')}
        if authorization not in tokens:
            raise HTTPException(401)
        tenant, role = tokens[authorization]
        if requested_tenant and tenant != requested_tenant:
            raise HTTPException(403)
        return Principal(authorization[7:], tenant, (role,), hashlib.sha256(authorization.encode()).hexdigest(), time.time() + 300)

    async def close(self):
        pass


@pytest.fixture
def commercial(tmp_path):
    cfg = Settings(mode='development', database_url='sqlite:///' + str(tmp_path / 'state.db'),
                   public_url='http://testserver', origins=('http://ui.test',),
                   control_token='c' * 32, callback_key='s' * 32, validation_timeout=30,
                   max_duration=60, local_root=str(tmp_path), version='integration-test')
    repo = Repository(cfg.database_url, create_schema=True, validation_duration=cfg.validation_timeout,
                      tenant_concurrency=cfg.tenant_concurrency, global_concurrency=cfg.global_concurrency)
    objects = LocalStorage(tmp_path / 'objects', signing_key=cfg.callback_key, base_url=cfg.public_url, allow_development=True)
    keys = LocalKeyringProvider({'test': Fernet.generate_key().decode()}, 'test', mode='development')
    app = create_production_app(cfg, repository=repo, storage=objects, keys=keys, authenticator=TestAuth())
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    with TestClient(app, headers={'Authorization': 'Bearer alpha'}) as client:
        yield client, repo, objects


def prepare(client, clip):
    response = client.post('/api/uploads', json={'name': 'clip.mp4', 'bytes': clip.stat().st_size,
                           'sha256': hashlib.sha256(clip.read_bytes()).hexdigest()})
    assert response.status_code == 201, response.text
    intent = response.json()
    signed = intent['upload']
    uploaded = client.put(signed['url'], content=clip.read_bytes(), headers=signed['headers'])
    assert uploaded.status_code == 200, uploaded.text
    done = client.post(f"/api/uploads/{intent['media']['id']}/complete")
    assert done.status_code == 202, done.text
    return intent


def run_claimed(client):
    response = client.post('/internal/claim', json={'worker_id': 'integration', 'version': 'integration-test'},
                           headers={'Authorization': 'Bearer ' + 'c' * 32})
    assert response.status_code == 200, response.text
    job = response.json()['job']
    assert job
    def transport(request):
        url = urlsplit(str(request.url))
        path = url.path + ('?' + url.query if url.query else '')
        response = client.request(request.method, path, content=request.read(), headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        result = run_job(job, client=http)
    assert result['state'] == 'completed', result
    return job


def test_real_end_to_end_private_upload_validation_stream_and_reconnect(commercial, clip):
    client, repo, objects = commercial
    assert client.get('/api/health').json()['ready'] is False
    intent = prepare(client, clip)
    media_id = intent['media']['id']
    # Validate finalize replay does not create a second validation job.
    assert client.post(f'/api/uploads/{media_id}/complete').status_code == 202
    assert len(repo.list_jobs('alpha')) == 1
    run_claimed(client)
    assert client.get('/api/health').json()['ready'] is True
    item = client.get('/api/media').json()[0]
    assert item['status'] == 'ready' and item['duration'] >= 3
    assert 'object_key' not in item
    assert client.get('/api/broadcasts').json() == []
    payload = {'media_id': media_id, 'title': 'API 통합', 'target': 'local'}
    first = client.post('/api/broadcasts', json=payload, headers={'Idempotency-Key': 'integration-create-1'})
    assert first.status_code == 201, first.text
    budget = estimate_output_bytes(item['duration'])
    reserved_usage = client.get('/api/usage').json()
    assert reserved_usage['storage_reserved_bytes'] == budget
    assert reserved_usage['storage_bytes'] == clip.stat().st_size + budget
    replay = client.post('/api/broadcasts', json=payload, headers={'Idempotency-Key': 'integration-create-1'})
    assert replay.json()['id'] == first.json()['id'] and replay.json()['replayed'] is True
    assert client.get('/api/usage').json() == reserved_usage
    claimed = run_claimed(client)
    job = client.get('/api/broadcasts/' + claimed['id']).json()
    assert job['state'] == 'completed', job
    signed = client.get('/api/broadcasts/' + claimed['id'] + '/output').json()
    output = client.get(signed['url'])
    assert output.status_code == 200 and output.content.startswith(b'FLV')
    settled_usage = client.get('/api/usage').json()
    assert settled_usage['storage_reserved_bytes'] == 0
    assert settled_usage['storage_bytes'] == clip.stat().st_size + len(output.content)
    # A new HTTP client/token session for the same principal sees the same persistent records.
    client.cookies.clear()
    assert client.get('/api/broadcasts').json()[0]['id'] == job['id']
    assert client.get('/api/broadcasts/' + job['id'] + '/events').json()


def test_tenant_roles_callback_fencing_and_revocation(commercial, clip):
    client, repo, objects = commercial
    intent = prepare(client, clip)
    run_claimed(client)
    media_id = intent['media']['id']
    assert client.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []
    assert client.get(f'/api/media/{media_id}/preview', headers={'Authorization': 'Bearer beta'}).status_code == 404
    assert client.delete(f'/api/media/{media_id}', headers={'Authorization': 'Bearer viewer'}).status_code == 403
    assert client.get('/api/media', headers={'X-Replay-Tenant': 'beta'}).status_code == 403
    assert client.post('/internal/claim', json={'worker_id': 'attacker', 'version': 'test'}).status_code == 401
    assert client.post('/internal/jobs/x/heartbeat', json={}).status_code == 401
    assert client.post('/api/logout').status_code == 200
    assert client.get('/api/media').status_code == 401
    assert client.get('/api/media', headers={'Authorization': 'Bearer beta'}).status_code == 200


def test_upload_integrity_body_boundary_and_immutable_object(commercial, clip):
    client, repo, objects = commercial
    intent = prepare(client, clip)
    signed = intent['upload']
    assert client.put(signed['url'], content=b'changed', headers=signed['headers']).status_code == 400
    assert client.post('/api/uploads', content=b' ' * 5000, headers={'Content-Type': 'application/json'}).status_code == 413
    assert client.post('/api/uploads', json={'name': 'a.mp4', 'bytes': 1, 'sha256': 'f' * 64}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.get('/api/live').headers['X-Request-ID']
    assert client.get('/api/media', headers={'Authorization': ''}).status_code == 401


def test_production_settings_fail_closed(tmp_path):
    with pytest.raises(ValueError):
        Settings()
    with pytest.raises(ValueError):
        Settings(mode='production', database_url='sqlite:///:memory:', public_url='https://api.test',
                 origins=('https://ui.test',), control_token='c' * 32, callback_key='s' * 32, version='test')


def test_monitoring_requires_control_auth_and_delivers_safe_durable_alerts(commercial):
    from sqlalchemy import update
    from server.repository import jobs

    client, repo, _ = commercial
    media = repo.add_media('alpha', name='private-customer-name.mp4', object_key='private-object-key',
                           bytes=500, duration=15, width=1280, height=720, fps=30)
    row = repo.create_job('alpha', media_id=media['id'], title='private-customer-title', target='local',
                         idempotency_key='monitoring-test-1', request_fingerprint='a' * 64)
    with repo.engine.begin() as connection:
        connection.execute(update(jobs).where(jobs.c.id == row['id']).values(next_run=time.time() - 200))
    assert client.post('/internal/monitor').status_code == 401
    assert client.post('/internal/alerts').status_code == 401
    assert client.post('/internal/alerts/' + 'a' * 32 + '/ack').status_code == 401
    headers = {'Authorization': 'Bearer ' + 'c' * 32}
    first = client.post('/internal/monitor', headers=headers)
    assert first.status_code == 200
    assert first.json()['metrics']['due_jobs'] == 1
    assert first.json()['alerts_created'] == 2
    assert client.post('/internal/monitor', headers=headers).json()['alerts_created'] == 0
    alerts = client.post('/internal/alerts', headers=headers)
    assert 'private-' not in alerts.text and 'alpha' not in alerts.text
    assert {alert['code'] for alert in alerts.json()['alerts']} == {'QUEUE_DELAY', 'DISPATCHER_STALE'}
    for alert in alerts.json()['alerts']:
        for _ in range(2):
            assert client.post('/internal/alerts/' + alert['id'] + '/ack', headers=headers).json()['acknowledged']
    assert client.post('/internal/alerts', headers=headers).json() == {'alerts': []}


def test_upload_signing_outage_does_not_reserve_customer_storage(commercial, monkeypatch):
    from server.aws_identity import AWSIdentityError

    client, repo, objects = commercial
    before = repo.usage('alpha')
    def unavailable(*args, **kwargs):
        raise AWSIdentityError('CLOUD_IDENTITY_UNAVAILABLE')
    monkeypatch.setattr(objects, 'presign_upload', unavailable)
    for _ in range(2):
        response = client.post('/api/uploads', json={'name': 'retry.mp4', 'bytes': 1024, 'sha256': 'a' * 64})
        assert response.status_code == 503
    assert repo.list_media('alpha') == []
    assert repo.usage('alpha') == before


def test_long_local_output_rejected_before_work_while_youtube_remains_available(commercial):
    client, repo, _ = commercial
    # Development local objects permit >5 GiB; use that configuration to
    # exercise the independent tenant-storage limit without generating a file.
    repo.max_output_bytes = 12 * 1024**3
    media = repo.add_media('alpha', name='four-hours.mp4', object_key='alpha/long.mp4', bytes=1024,
                          duration=14400, width=1920, height=1080, fps=30)
    quote = client.get(f"/api/media/{media['id']}/output-estimate")
    assert quote.status_code == 200
    estimate = quote.json()
    assert estimate['estimated_output_bytes'] > 10 * 1024**3
    assert estimate['can_create'] is False and estimate['reason'] == 'OUTPUT_STORAGE_QUOTA_EXCEEDED'
    payload = {'media_id': media['id'], 'title': '장시간 사전 거절', 'target': 'local', 'max_attempts': 1}
    response = client.post('/api/broadcasts', json=payload, headers={'Idempotency-Key': 'long-output-capacity'})
    assert response.status_code == 409
    assert response.json()['code'] == 'OUTPUT_STORAGE_QUOTA_EXCEEDED'
    assert '저장 공간' in response.json()['detail'] or '저장할 공간' in response.json()['detail']
    assert repo.list_jobs('alpha') == [] and repo.usage('alpha')['storage_reserved_bytes'] == 0
    # This is a metadata/admission test only; no four-hour file or YouTube worker runs.
    youtube = client.post('/api/broadcasts', json={**payload, 'target': 'youtube', 'stream_key': 'synthetic-youtube-key'},
                          headers={'Idempotency-Key': 'long-youtube-admission'})
    assert youtube.status_code == 201
    assert youtube.json()['output_budget_bytes'] == 0
    assert repo.usage('alpha')['storage_bytes'] == 1024


def test_output_quote_is_tenant_scoped_and_file_cap_is_independent_of_source_upload(commercial):
    client, repo, _ = commercial
    media = repo.add_media('alpha', name='clip.mp4', object_key='alpha/estimate.mp4', bytes=100,
                          duration=15, width=640, height=360, fps=30)
    path = f"/api/media/{media['id']}/output-estimate"
    assert client.get(path, headers={'Authorization': 'Bearer beta'}).status_code == 404
    assert client.get(path, headers={'Authorization': 'Bearer viewer'}).status_code == 200
    repo.max_output_bytes = estimate_output_bytes(15) - 1
    assert client.get(path).json()['reason'] == 'OUTPUT_LIMIT_EXCEEDED'
    result = client.post('/api/broadcasts', json={'media_id': media['id'], 'title': '크기 제한', 'target': 'local'},
                         headers={'Idempotency-Key': 'output-per-file-limit'})
    assert result.status_code == 409 and result.json()['code'] == 'OUTPUT_LIMIT_EXCEEDED'
    assert repo.list_jobs('alpha') == []


def test_reservation_prevents_competing_upload_and_cancel_releases_capacity(commercial):
    client, repo, _ = commercial
    media = repo.add_media('alpha', name='clip.mp4', object_key='alpha/reservation.mp4', bytes=100,
                          duration=15, width=640, height=360, fps=30)
    budget = estimate_output_bytes(15)
    repo.max_storage_bytes = budget + 200
    result = client.post('/api/broadcasts', json={'media_id': media['id'], 'title': '용량 예약', 'target': 'local'},
                         headers={'Idempotency-Key': 'output-capacity-reserved'})
    assert result.status_code == 201
    blocked = client.post('/api/uploads', json={'name': 'competing.mp4', 'bytes': 101, 'sha256': 'a' * 64})
    assert blocked.status_code == 409
    assert client.get('/api/usage').json()['storage_available_bytes'] == 100
    for _ in range(2):
        assert client.post('/api/broadcasts/' + result.json()['id'] + '/stop').status_code == 200
    assert client.get('/api/usage').json()['storage_reserved_bytes'] == 0
    accepted = client.post('/api/uploads', json={'name': 'competing.mp4', 'bytes': 101, 'sha256': 'a' * 64})
    assert accepted.status_code == 201


def test_output_configuration_rejects_values_outside_worker_ceiling(commercial):
    from dataclasses import replace

    client, _, _ = commercial
    cfg = client.app.state.settings
    assert replace(cfg, max_output_bytes=12 * 1024**3).max_output_bytes > cfg.max_upload_bytes * 4
    for invalid in (0, -1, True, 1.5, 32 * 1024**3 + 1):
        with pytest.raises(ValueError):
            replace(cfg, max_output_bytes=invalid)
