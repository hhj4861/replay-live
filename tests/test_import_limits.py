"""Per-video limits can be disabled without disabling storage or lease budgets."""
import hashlib
import json
import subprocess

import httpx
import pytest

from server.media_runtime import MediaError, validate_media
from server.media_sources import SourceImportError, _recording
from server.repository import Repository, QuotaExceeded
from server.worker import run_job
from test_vercel_free_runtime import settings
from test_worker import WorkerHTTP, import_config, install_import_fixture
from test_commercial_api import commercial, prepare, run_claimed


def test_zero_limits_keep_a_finite_account_quota_and_runtime_budget():
    cfg = settings(storage_provider='cloudflare-r2', max_duration=0, max_upload_bytes=0,
                   max_storage_bytes=256 * 1024**2, worker_max_seconds=3600, validation_timeout=1800)
    assert cfg.max_duration == cfg.max_upload_bytes == 0
    assert cfg.max_storage_bytes == 256 * 1024**2
    for changes in [{'max_duration': -1}, {'max_upload_bytes': -1}, {'max_duration': True}]:
        with pytest.raises(ValueError): settings(**changes)
    _recording({'duration': 2199}, 0)
    _recording({'duration': 18000}, 0)
    with pytest.raises(SourceImportError): _recording({'duration': 2199}, 120)
    for invalid in [float('nan'), float('inf'), -1, 0]:
        with pytest.raises(SourceImportError): _recording({'duration': invalid}, 0)


def test_remaining_storage_is_still_reserved_atomically(tmp_path):
    repo = Repository(f'sqlite:///{tmp_path}/quota.db', max_storage_bytes=256 * 1024**2, create_schema=True)
    try:
        repo.add_media('owner', name='existing.mp4', object_key='existing', bytes=10 * 1024**2,
                       duration=2199, width=320, height=180, fps=30)
        result = repo.create_import('owner', name='long.mp4', object_key='new', secret_ciphertext='synthetic',
            idempotency_key='large-import', request_fingerprint='a' * 64, max_bytes=5 * 1024**3)
        assert result['job']['output_budget_bytes'] == 246 * 1024**2
        with pytest.raises(QuotaExceeded):
            repo.create_import('owner', name='another.mp4', object_key='other', secret_ciphertext='synthetic',
                idempotency_key='second-import', request_fingerprint='b' * 64, max_bytes=5 * 1024**3)
    finally: repo.close()


def test_actual_36_minute_mp4_passes_full_validation_when_length_limit_disabled(tmp_path, commercial):
    clip = tmp_path / 'long.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=1',
        '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '2199', '-c:v', 'libx264',
        '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(clip)], check=True)
    assert validate_media(clip, validation_timeout=60, max_duration=0)['duration'] == 2199
    with pytest.raises(MediaError): validate_media(clip, validation_timeout=60, max_duration=120)
    client, repo, _ = commercial
    object.__setattr__(client.app.state.settings, 'max_duration', 0)
    object.__setattr__(client.app.state.settings, 'max_upload_bytes', 0)
    assert client.get('/api/limits').json()['max_duration_seconds'] == 0
    prepare(client, clip); run_claimed(client)
    assert client.get('/api/media').json()[0]['duration'] == 2199
    # Large upload admission is quota bounded even with the per-file cap off.
    response = client.post('/api/uploads', json={'name': 'large.mp4', 'bytes': 65 * 1024**2, 'sha256': 'a' * 64})
    assert response.status_code == 201, response.text


@pytest.mark.parametrize('fail_part', [False, True])
def test_worker_large_upload_splits_bytes_and_aborts_failed_transfer(tmp_path, monkeypatch, fail_part):
    data = b'v' * (65 * 1024**2 + 17)
    install_import_fixture(monkeypatch, data)
    # Media decoding is covered by the real 36-minute fixture above; here the
    # real worker HTTP flow must preserve every byte and its upload budget.
    monkeypatch.setattr('server.worker.validate_media', lambda *a, **kw: dict(duration=2199, width=320, height=180, fps=30))
    job = import_config(data); job.update(max_duration=0, validation_timeout=60)
    remote = WorkerHTTP(data); chunks = []; completed = []; aborted = []
    def handler(request):
        if request.url.path.endswith('/output'):
            return httpx.Response(200, json={'url': 'https://objects.example/result', 'method': 'PUT',
                'headers': {'Content-Type': 'video/mp4', 'Content-Length': str(len(data))},
                'multipart': {'part_size': 32 * 1024**2}})
        if request.url.path == '/result':
            if request.method == 'DELETE': aborted.append(True); return httpx.Response(200)
            if request.method == 'POST': completed.append(json.loads(request.content)); return httpx.Response(200)
            number = int(request.headers['x-replay-part'])
            if fail_part and number == 2: return httpx.Response(503)
            chunk = request.read(); assert int(request.headers['content-length']) == len(chunk)
            assert len(chunk) <= 32 * 1024**2
            chunks.append(chunk)
            return httpx.Response(200, json={'partNumber': number, 'etag': f'part-{number}'})
        return remote(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    if fail_part:
        assert result['state'] == 'failed' and aborted and not completed
    else:
        assert result['state'] == 'completed' and not aborted
        assert hashlib.sha256(b''.join(chunks)).hexdigest() == result['output_sha256']
        assert [p['partNumber'] for p in completed[0]['parts']] == [1, 2, 3]
    assert not list(tmp_path.iterdir())
