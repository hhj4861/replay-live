"""Import admission, encrypted handoff, immutable publication and cleanup."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import uuid

import pytest
from sqlalchemy import select, update

from server.repository import (Conflict, LeaseLost, NotFound, QuotaExceeded,
                               Repository, RepositoryError, jobs, media,
                               upload_reservations)


INFO = {'duration': 15, 'width': 1280, 'height': 720, 'fps': 30}


@pytest.fixture
def import_repo(tmp_path):
    clock = [1_000_000.0]
    repo = Repository(f'sqlite:///{tmp_path}/imports.db', clock=lambda: clock[0],
        max_storage_bytes=20_000, runtime_allowance=0, validation_duration=60,
        tenant_concurrency=4, create_schema=True)
    yield repo, clock
    repo.close()


def admit(repo, tenant='alpha', *, key='source-import-1', fingerprint='a' * 64, max_bytes=500_000):
    identifier = uuid.uuid4().hex
    return repo.create_import(tenant, id=identifier, name='녹화영상.mp4',
        object_key=f'replay/{tenant}/media/{identifier}.mp4', secret_ciphertext='encrypted-source',
        idempotency_key=key, request_fingerprint=fingerprint, max_bytes=max_bytes)


def reserve(repo, claim, *, size=2000):
    return repo.reserve_output(claim['id'], claim['lease_token'], bytes=size, sha256='a' * 64,
        object_key=f'replay/{claim["tenant_id"]}/media/{claim["media_id"]}-{claim["lease_version"]}.mp4')


def test_import_reserves_remaining_capacity_and_idempotent_replay_is_free(import_repo):
    repo, _ = import_repo
    repo.add_media('alpha', name='existing.mp4', object_key='existing', bytes=3000, **INFO)
    first = admit(repo)
    assert first['media']['status'] == 'importing' and first['media']['bytes'] == 0
    assert first['job']['output_budget_bytes'] == 17_000
    assert repo.usage('alpha')['storage_bytes'] == 20_000
    replay = admit(repo)
    assert replay['job']['replayed'] is True
    assert replay['media']['id'] == first['media']['id']
    assert len(repo.list_media('alpha')) == 2
    with pytest.raises(Conflict):
        admit(repo, fingerprint='b' * 64)
    with pytest.raises(QuotaExceeded):
        admit(repo, key='source-import-2')
    assert len(repo.list_jobs('alpha')) == 1
    assert admit(repo, 'beta')['job']['output_budget_bytes'] == 20_000
    with pytest.raises(NotFound):
        repo.get_media('beta', first['media']['id'])


def test_import_admission_rolls_back_media_when_runtime_capacity_rejects(import_repo):
    repo, _ = import_repo
    repo.max_daily_runtime_seconds = 30
    with pytest.raises(QuotaExceeded):
        admit(repo)
    assert repo.list_media('alpha') == []
    assert repo.list_jobs('alpha') == []
    assert repo.usage('alpha')['storage_bytes'] == 0


def test_concurrent_imports_cannot_overbook_tenant_storage(import_repo):
    repo, clock = import_repo
    rival = Repository(str(repo.engine.url), clock=lambda: clock[0],
        max_storage_bytes=20_000, validation_duration=60, tenant_concurrency=4)
    barrier = threading.Barrier(2)

    def create(instance, key):
        barrier.wait()
        try:
            admit(instance, key=key)
            return True
        except QuotaExceeded:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create, repo, 'import-first'), pool.submit(create, rival, 'import-other')]
            assert sum(result.result() for result in futures) == 1
        assert len(repo.list_media('alpha')) == 1
        assert repo.usage('alpha')['storage_bytes'] == 20_000
    finally:
        rival.close()


def test_import_completion_publishes_original_once_and_clears_source(import_repo):
    repo, clock = import_repo
    created = admit(repo)
    claim = repo.claim('worker', lease_seconds=30)
    intent = reserve(repo, claim)
    assert repo.usage('alpha')['storage_bytes'] == 20_000
    with pytest.raises(RepositoryError):
        repo.finish(claim['id'], claim['lease_token'], state='completed', output_key=intent['object_key'],
            output_bytes=2000, validation_metadata={**INFO, 'width': 1})
    assert repo.get_media('alpha', created['media']['id'])['status'] == 'importing'
    with repo.engine.connect() as conn:
        assert conn.execute(select(upload_reservations.c.status)).scalar_one() == 'pending'
    result = repo.finish(claim['id'], claim['lease_token'], state='completed', output_key=intent['object_key'],
        output_bytes=2000, validation_metadata=INFO)
    item = repo.get_media('alpha', created['media']['id'])
    assert item['status'] == 'ready' and item['object_key'] == intent['object_key']
    assert item['bytes'] == 2000 and item['sha256'] == 'a' * 64
    assert item['duration'] == 15
    assert result['output_key'] is None and result['output_bytes'] == 0
    assert repo.usage('alpha')['storage_bytes'] == 2000
    assert repo.usage('alpha')['storage_reserved_bytes'] == 0
    with repo.engine.connect() as conn:
        assert conn.execute(select(jobs.c.secret_ciphertext)).scalar_one() is None
    with pytest.raises(LeaseLost):
        repo.finish(claim['id'], claim['lease_token'], state='completed', output_key=intent['object_key'],
                    output_bytes=2000, validation_metadata=INFO)
    # Expiring job history must not delete a source still retained in the library.
    with repo.engine.begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == claim['id']).values(updated=clock[0] - 10))
    repo.cleanup(clock[0] - 5)
    assert repo.list_jobs('alpha') == []
    assert repo.get_media('alpha', item['id'])['status'] == 'ready'
    assert repo.pending_deletions() == []


@pytest.mark.parametrize('ending', ['failed', 'cancelled', 'expired'])
def test_failed_import_keeps_issued_put_charged_until_expiry_and_deletion(import_repo, ending):
    repo, clock = import_repo
    created = admit(repo)
    claim = repo.claim('worker', lease_seconds=10)
    intent = reserve(repo, claim)
    if ending == 'failed':
        repo.finish(claim['id'], claim['lease_token'], state='failed', error_code='SOURCE_UNAVAILABLE')
    elif ending == 'cancelled':
        repo.cancel('alpha', claim['id'])
        result = repo.finish(claim['id'], claim['lease_token'], state='completed', output_key=intent['object_key'],
            output_bytes=2000, validation_metadata=INFO)
        assert result['state'] == 'stopped'
    else:
        clock[0] += 11
        repo.recover_expired()
        with pytest.raises(LeaseLost):
            reserve(repo, claim)
    assert repo.get_media('alpha', created['media']['id'])['status'] == 'failed'
    assert repo.usage('alpha')['storage_bytes'] == 2000
    assert repo.pending_deletions() == []
    clock[0] = intent['expires_at'] + 1
    deletion = repo.pending_deletions()[0]
    assert deletion['object_key'] == intent['object_key']
    repo.confirm_deletion(deletion['id'])
    assert repo.usage('alpha')['storage_bytes'] == 0
    with repo.engine.connect() as conn:
        assert conn.execute(select(jobs.c.secret_ciphertext)).scalar_one() is None


def test_import_requires_verified_reserved_upload(import_repo):
    repo, _ = import_repo
    admit(repo)
    claim = repo.claim('worker')
    with pytest.raises(Conflict):
        repo.finish(claim['id'], claim['lease_token'], state='completed', validation_metadata=INFO)
    with pytest.raises(Conflict):
        repo.finish(claim['id'], claim['lease_token'], state='completed', output_key='unreserved',
                    output_bytes=2000, validation_metadata=INFO)
    assert repo.get_media('alpha', claim['media_id'])['status'] == 'importing'


@pytest.fixture
def import_api(tmp_path):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from server.production_app import create_production_app
    from server.secrets import LocalKeyringProvider
    from server.settings import Settings
    from server.storage import LocalStorage
    from test_commercial_api import TestAuth
    cfg = Settings(mode='development', database_url=f'sqlite:///{tmp_path}/api.db',
        public_url='http://testserver', origins=('http://ui.test',), control_token='c' * 32,
        callback_key='s' * 32, validation_timeout=30, max_duration=60,
        max_storage_bytes=20_000, local_root=str(tmp_path), version='import-test')
    repo = Repository(cfg.database_url, create_schema=True, max_storage_bytes=cfg.max_storage_bytes,
                      validation_duration=cfg.validation_timeout)
    objects = LocalStorage(tmp_path / 'objects', signing_key=cfg.callback_key,
                           base_url=cfg.public_url, allow_development=True)
    keys = LocalKeyringProvider({'test': Fernet.generate_key().decode()}, 'test', mode='development')
    app = create_production_app(cfg, repository=repo, storage=objects, keys=keys, authenticator=TestAuth())
    app.state.access_policy.migrate()
    app.state.operations.migrate()
    with TestClient(app, headers={'Authorization': 'Bearer alpha'}) as client:
        yield client, repo, keys


def test_api_import_uses_encrypted_handoff_and_checks_uploaded_original(import_api):
    client, repo, keys = import_api
    source = {'provider': 'youtube', 'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw&token=synthetic-private-query'}
    response = client.post('/api/media/imports', json=source, headers={'Idempotency-Key': 'api-import-test'})
    assert response.status_code == 202, response.text
    created = response.json()
    assert created['media']['status'] == 'importing'
    assert created['job']['output_budget_bytes'] == 20_000
    assert 'synthetic-private-query' not in response.text and 'ciphertext' not in response.text
    assert client.get('/api/broadcasts').json() == []
    assert client.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []
    repeat = client.post('/api/media/imports', json=source, headers={'Idempotency-Key': 'api-import-test'})
    assert repeat.json()['media']['id'] == created['media']['id']
    assert repeat.json()['job']['replayed'] is True
    with repo.engine.connect() as conn:
        ciphertext = conn.execute(select(jobs.c.secret_ciphertext)).scalar_one()
        assert source['url'] not in ciphertext
        assert json.loads(keys.decrypt(ciphertext, context={'tenant_id': 'alpha'})) == source
    response = client.post('/internal/claim', json={'worker_id': 'worker', 'version': 'import-test'},
        headers={'Authorization': 'Bearer ' + 'c' * 32})
    assert response.status_code == 200, response.text
    claim = response.json()['job']
    assert claim['source'] == source and claim['input'] is None
    assert claim['stream_destination'] is None and claim['stream_key'] == ''
    worker = {'Authorization': 'Bearer ' + claim['callback_token']}
    # These callbacks test object publication independently of downloader/decoder tests.
    original = b'validated-synthetic-original'
    digest = hashlib.sha256(original).hexdigest()
    base = '/internal/jobs/' + claim['id']
    output = client.post(base + '/output', json={'bytes': len(original), 'sha256': digest}, headers=worker)
    assert output.status_code == 200, output.text
    signed = output.json()
    assert signed['headers']['Content-Type'] == 'video/mp4'
    assert client.put(signed['url'], content=original, headers=signed['headers']).status_code == 200
    completed = {'state': 'completed', 'metadata': INFO, 'output_bytes': len(original), 'output_sha256': digest}
    bad = client.post(base + '/finish', json={**completed, 'output_sha256': 'f' * 64}, headers=worker)
    assert bad.status_code == 400
    assert client.get('/api/media').json()[0]['status'] == 'importing'
    done = client.post(base + '/finish', json=completed, headers=worker)
    assert done.status_code == 200, done.text
    assert done.json()['output_bytes'] == 0
    assert client.get('/api/media').json()[0]['status'] == 'ready'
    assert client.get('/api/usage').json()['storage_bytes'] == len(original)
    assert client.get('/api/usage').json()['storage_reserved_bytes'] == 0
    preview = client.get('/api/media/' + created['media']['id'] + '/preview').json()
    assert client.get(preview['url']).content == original


def test_api_import_authentication_catalog_and_boundaries(import_api):
    client, repo, _ = import_api
    source = {'provider': 'youtube', 'url': 'https://www.youtube.com/watch?v=jNQXAC9IVRw'}
    assert client.get('/api/media-sources', headers={'Authorization': ''}).status_code == 401
    assert any(item['id'] == 'youtube' for item in client.get('/api/media-sources').json()['sources'])
    assert client.post('/api/media/imports', json=source,
        headers={'Authorization': 'Bearer viewer', 'Idempotency-Key': 'api-viewer-import'}).status_code == 403
    assert client.post('/api/media/imports', json=source).status_code == 400
    unsafe = client.post('/api/media/imports', json={'provider': 'direct', 'url': 'https://127.0.0.1/private'},
        headers={'Idempotency-Key': 'api-unsafe-import'})
    assert unsafe.status_code == 400
    assert '127.0.0.1' not in unsafe.text
    assert client.post('/api/media/imports', content=b' ' * 9000,
        headers={'Content-Type': 'application/json'}).status_code == 413
    assert repo.list_media('alpha') == []
