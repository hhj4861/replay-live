"""Dedicated-VM platform launcher boundaries; no external streams or VM operations."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('platform_live_preview', ROOT / 'scripts/platform-live-preview.py')
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)


def test_allowlist_is_explicit_and_none_means_file_only():
    assert live.allowed_targets([]) == live.allowed_targets(['none']) == {'local'}
    assert live.allowed_targets(['twitch']) == {'local', 'twitch'}
    assert live.allowed_targets(['all']) == live.STREAM_TARGETS
    for values in [['unknown'], ['none', 'twitch'], ['all', 'none'], ['all', 'twitch']]:
        with pytest.raises(live.LivePreviewError):
            live.allowed_targets(values)


def boundary_client(allowed):
    app = FastAPI()
    auth = live.preview.preview_authenticator(None)
    live.install_boundary(app, auth, api_port=18092, web='http://127.0.0.1:13102', allowed=allowed)

    @app.post('/api/broadcasts')
    @app.post('/api/broadcast-batches')
    def accepted():
        return {'accepted': True}

    return TestClient(app, base_url='http://127.0.0.1:18092',
        headers={'Origin': 'http://127.0.0.1:13102', 'X-Replay-Client': '1'}), auth


def test_shared_preview_session_contract_and_exact_origin():
    client, auth = boundary_client(frozenset({'local'}))
    with client:
        first = client.post('/preview/session')
        assert first.status_code == 200
        second = client.post('/preview/session')
        assert first.json()['token'] != second.json()['token'] == auth.config.dev_token
        assert first.headers['Access-Control-Allow-Origin'] == 'http://127.0.0.1:13102'
        assert client.post('/preview/session', headers={'Origin': 'http://localhost:13102'}).status_code == 403
        assert client.post('/preview/session', headers={'Host': 'localhost:18092'}).status_code == 403
        assert client.post('/preview/session', headers={'X-Replay-Client': '0'}).status_code == 403
        status = client.get('/preview/status').json()
        assert status['sequential'] and not status['external_streaming']
        assert status['storage_limit_bytes'] == 1024**3 and status['max_duration_seconds'] == 120


@pytest.mark.parametrize('allowed', [frozenset({'local'}), frozenset({'local', 'twitch'})])
def test_api_guards_single_batch_unknown_and_unhashable_targets(allowed):
    client, _ = boundary_client(allowed)
    with client:
        assert client.post('/api/broadcasts', json={'target': 'local'}).status_code == 200
        assert client.post('/api/broadcasts', json={'target': 'twitch'}).status_code == (200 if 'twitch' in allowed else 400)
        for payload in [{'target': 'youtube', 'stream_key': 'secret-canary'}, {'target': []}, None]:
            response = client.post('/api/broadcasts', json=payload)
            assert response.status_code == 400 and 'secret-canary' not in response.text
        response = client.post('/api/broadcast-batches', json={'destinations': [{'target': 'local'}, {'target': 'youtube'}]})
        assert response.status_code == 400
        assert client.post('/api/broadcast-batches', json={'destinations': []}).status_code == 400
        response = client.post('/api/broadcasts', content=b' ' * 8200,
            headers={'Content-Type': 'application/json'})
        assert response.status_code == 413


def test_all_platforms_accepts_catalog_targets_but_keeps_request_boundary():
    client, _ = boundary_client(live.allowed_targets(['all']))
    with client:
        status = client.get('/preview/status').json()
        assert set(status['allowed_targets']) == live.STREAM_TARGETS
        assert status['external_streaming'] and status['sequential']
        for target in live.STREAM_TARGETS:
            assert client.post('/api/broadcasts', json={'target': target}).status_code == 200
        assert client.post('/api/broadcast-batches', json={'destinations': [
            {'target': target} for target in ('youtube', 'twitch', 'instagram', 'tiktok')]}).status_code == 200
        assert client.post('/api/broadcasts', json={'target': 'unknown'}).status_code == 400
        assert client.post('/api/broadcasts', json={'target': 'twitch'},
                           headers={'Origin': 'https://untrusted.example'}).status_code == 403


def test_all_platforms_real_api_accepts_single_jobs_and_rejects_multiple_destinations(tmp_path, monkeypatch):
    from server import stream_targets

    monkeypatch.setattr(stream_targets, '_resolve', lambda host, port: ['8.8.8.8'])
    def forbidden(*args, **kwargs):
        raise AssertionError('Admission tests must never start a worker or stream')
    monkeypatch.setattr(live, 'worker_loop', forbidden)
    monkeypatch.setattr(live, 'run_job', forbidden)
    runtime = live.build_application(tmp_path / 'all-platforms', allowed=live.allowed_targets(['all']))
    runtime.repo.add_media(live.TENANT, id='admission-fixture', name='fixture.mp4', object_key='fixture.mp4',
        bytes=100, sha256='a' * 64, status='ready', duration=17, width=640, height=360, fps=30)
    with TestClient(runtime.app, base_url=runtime.cfg.public_url,
            headers={'Origin': runtime.cfg.origins[0], 'X-Replay-Client': '1'}) as client:
        session = client.post('/preview/session')
        assert session.status_code == 200
        client.headers['Authorization'] = 'Bearer ' + session.json()['token']
        catalog = client.get('/api/stream-targets')
        assert catalog.status_code == 200
        assert catalog.json()['max_destinations'] == 1
        assert {item['id'] for item in catalog.json()['targets']} == live.STREAM_TARGETS
        destinations = [{'target': target, 'server_url': 'rtmps://ingest.example.com:443/live',
                         'stream_key': 'synthetic-key-' + target} for target in sorted(live.LIVE_TARGETS)]
        for destination in destinations:
            response = client.post('/api/broadcasts',
                json={'media_id': 'admission-fixture', 'title': 'Admission only', **destination},
                headers={'Idempotency-Key': 'all-platforms-' + destination['target']})
            assert response.status_code == 201, response.text
            assert response.json()['target'] == destination['target']
            assert response.json()['state'] == 'scheduled'
            stopped = client.post('/api/broadcasts/' + response.json()['id'] + '/stop')
            assert stopped.status_code == 200 and stopped.json()['state'] == 'stopped'
        response = client.post('/api/broadcast-batches',
            json={'media_id': 'admission-fixture', 'title': 'Rejected batch', 'destinations': destinations[:2]},
            headers={'Idempotency-Key': 'all-platforms-batch'})
        assert response.status_code == 409 and response.json()['code'] == 'QuotaExceeded'
        assert len(runtime.repo.list_jobs(live.TENANT)) == len(live.LIVE_TARGETS)
        assert runtime.repo.usage(live.TENANT)['pending_jobs'] == 0


def test_persistent_private_keys_media_and_cancelled_old_jobs(tmp_path):
    data = tmp_path / 'persist'
    first = live.build_application(data)
    values = (data / 'keys.json').read_bytes()
    first.repo.add_media(live.TENANT, id='preserved', name='fixture.mp4', object_key='fixture.mp4',
        bytes=100, sha256='a' * 64, status='ready', duration=15, width=640, height=360, fps=30)
    job = first.repo.create_job(live.TENANT, media_id='preserved', title='Do not resume',
        target='local', idempotency_key='old-scheduled-test')
    ciphertext = live.LocalKeyringProvider({'platform-test': live.persistent_keys(data)['keyring']},
        'platform-test', mode='development').encrypt('synthetic-secret', context={'tenant_id': live.TENANT})
    first.repo.close()
    second = live.build_application(data, allowed=frozenset({'local', 'twitch'}))
    try:
        assert (data / 'keys.json').read_bytes() == values
        assert second.repo.get_media(live.TENANT, 'preserved')['status'] == 'ready'
        assert second.repo.get_job(live.TENANT, job['id'])['state'] == 'stopped'
        keys = live.LocalKeyringProvider({'platform-test': live.persistent_keys(data)['keyring']}, 'platform-test', mode='development')
        assert keys.decrypt(ciphertext, context={'tenant_id': live.TENANT}) == 'synthetic-secret'
        assert data.stat().st_mode & 0o777 == 0o700
        assert (data / 'keys.json').stat().st_mode & 0o777 == 0o600
        assert (data / 'platform-test.sqlite3').stat().st_mode & 0o777 == 0o600
        assert second.cfg.max_storage_bytes == 1024**3 and second.cfg.max_duration == 120
        assert second.repo.tenant_concurrency == second.repo.global_concurrency == 1
    finally:
        second.repo.close()


def test_keys_missing_or_insecure_never_regenerated(tmp_path):
    data = live.private_directory(tmp_path / 'missing')
    live.create_private_file(data / 'platform-test.sqlite3')
    with pytest.raises(live.LivePreviewError, match='PLATFORM_TEST_KEYS_MISSING'):
        live.persistent_keys(data)
    assert not (data / 'keys.json').exists()
    other = live.private_directory(tmp_path / 'insecure')
    live.persistent_keys(other)
    (other / 'keys.json').chmod(0o644)
    with pytest.raises(live.LivePreviewError):
        live.persistent_keys(other)
    linked = live.private_directory(tmp_path / 'linked')
    (linked / 'keys.json').symlink_to(other / 'keys.json')
    with pytest.raises(live.LivePreviewError):
        live.persistent_keys(linked)


def test_hosts_restored_after_pin_and_previous_crash(tmp_path):
    hosts, journal = tmp_path / 'hosts', tmp_path / 'pin.json'
    original = '127.0.0.1 localhost\n1.1.1.1 ingest.example.com prior-alias # original\n'
    hosts.write_text(original)
    live.edit_host_pin('install', hostname='ingest.example.com', addresses=['8.8.8.8'], hosts_path=hosts, journal_path=journal)
    assert '8.8.8.8\tingest.example.com' in hosts.read_text()
    assert journal.stat().st_mode & 0o777 == 0o600
    with pytest.raises(live.LivePreviewError, match='RESTORE_REQUIRED'):
        live.edit_host_pin('install', hostname='next.example.com', addresses=['8.8.4.4'], hosts_path=hosts, journal_path=journal)
    live.edit_host_pin('restore', hosts_path=hosts, journal_path=journal)
    assert hosts.read_text() == original and not journal.exists()
    live.edit_host_pin('restore', hosts_path=hosts, journal_path=journal)


def test_hosts_restore_preserves_unrelated_external_change(tmp_path):
    hosts, journal = tmp_path / 'hosts', tmp_path / 'pin.json'
    hosts.write_text('127.0.0.1 localhost\n')
    live.edit_host_pin('install', hostname='ingest.example.com', addresses=['8.8.8.8'], hosts_path=hosts, journal_path=journal)
    changed = hosts.read_text() + '1.1.1.1 unrelated.example.com\n'
    hosts.write_text(changed)
    with pytest.raises(live.LivePreviewError, match='RESTORE_REQUIRED'):
        live.edit_host_pin('restore', hosts_path=hosts, journal_path=journal)
    assert hosts.read_text() == changed and journal.exists()


def test_worker_guards_before_pin_and_restores_after_failure(tmp_path, monkeypatch):
    calls = []
    runtime = SimpleNamespace(cfg=SimpleNamespace(public_url='http://127.0.0.1:18092'),
        allowed=frozenset({'local', 'twitch'}), data_dir=tmp_path)
    job = {'mode': 'development', 'version': live.VERSION, 'id': 'job', 'target': 'twitch',
        'callback_base': 'http://127.0.0.1:18092/internal/jobs/job', 'stream_destination': {'fixture': True}}
    monkeypatch.setattr(live, 'manage_host_pin', lambda action, destination=None: calls.append(action))
    def failure(*args, **kwargs):
        calls.append('worker-original-checks')
        raise RuntimeError('synthetic worker failure')
    monkeypatch.setattr(live, 'run_job', failure)
    with pytest.raises(RuntimeError):
        live.process_job(runtime, job, None)
    assert calls == ['install', 'worker-original-checks', 'restore']
    calls.clear()
    with pytest.raises(live.LivePreviewError):
        live.process_job(runtime, {**job, 'callback_base': 'https://foreign.invalid/jobs/job'}, None)
    assert not calls


def test_worker_refuses_disallowed_claim_and_scrubs_credentials(tmp_path, monkeypatch, capsys):
    import httpx
    calls = []
    runtime = SimpleNamespace(cfg=SimpleNamespace(public_url='http://127.0.0.1:18092'),
        allowed=frozenset({'local', 'twitch'}), data_dir=tmp_path)
    job = {'mode': 'development', 'version': live.VERSION, 'id': 'job', 'target': 'youtube',
        'callback_base': 'http://127.0.0.1:18092/internal/jobs/job', 'callback_token': 'secret-canary',
        'stream_key': 'secret-canary', 'stream_destination': {'stream_key': 'secret-canary'}}
    def forbidden(*args, **kwargs):
        raise AssertionError('Neither hosts nor media may be touched for a disallowed platform')
    monkeypatch.setattr(live, 'manage_host_pin', forbidden)
    monkeypatch.setattr(live, 'run_job', forbidden)
    def transport(request):
        calls.append(json.loads(request.content))
        assert str(request.url) == 'http://127.0.0.1:18092/internal/jobs/job/finish'
        return httpx.Response(200, json={'state': 'failed'})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        live.process_job(runtime, job, client)
    assert calls == [{'state': 'failed', 'error_code': 'PLATFORM_TEST_TARGET_FORBIDDEN', 'progress': 0}]
    assert job['callback_token'] == job['stream_key'] == '' and job['stream_destination'] == {}
    assert 'secret-canary' not in capsys.readouterr().out


def test_vm_gate_fails_before_opening_data_or_network(monkeypatch, capsys):
    monkeypatch.setattr(live.sys, 'argv', ['platform-live-preview.py', '--data-dir', '/unused', '--allow-target', 'twitch'])
    monkeypatch.setattr(live.sys, 'platform', 'darwin')
    def forbidden(*args, **kwargs):
        raise AssertionError('No data, hosts or network mutation before VM validation')
    monkeypatch.setattr(live, 'private_directory', forbidden)
    monkeypatch.setattr(live, 'manage_host_pin', forbidden)
    monkeypatch.setattr(live.preview, 'require_free_ports', forbidden)
    assert live.main() == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'PLATFORM_TEST_VM_REQUIRED'


def test_second_launcher_never_restores_an_active_runtimes_hosts(tmp_path, monkeypatch, capsys):
    data = live.private_directory(tmp_path / 'active')
    marker = tmp_path / 'vm-marker'
    marker.write_text(live.VM_MARKER_CONTENT)
    monkeypatch.setattr(live, 'VM_MARKER', marker)
    monkeypatch.setattr(live, 'require_test_vm', lambda: None)
    monkeypatch.setattr(live.preview, 'require_free_ports', lambda ports: None)
    monkeypatch.setattr(live.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(live.sys, 'argv', ['platform-live-preview.py', '--data-dir', str(data)])
    calls = []
    monkeypatch.setattr(live, 'manage_host_pin', lambda *args: calls.append(args))
    with live.exclusive_runtime(data, vm_lock_path=marker):
        assert live.main() == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'PLATFORM_TEST_ALREADY_RUNNING'
    assert not calls


def test_real_sample_seed_is_once_and_never_broadcasts(tmp_path, monkeypatch):
    sample = tmp_path / 'fixture.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '1', '-c:v', 'libx264', '-preset', 'ultrafast',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(sample)], check=True, capture_output=True, timeout=30)
    def forbidden(*args, **kwargs):
        raise AssertionError('Seeding does not claim or stream')
    monkeypatch.setattr(live, 'run_job', forbidden)
    runtime = live.build_application(tmp_path / 'sample-state')
    try:
        first = live.seed_sample(runtime, sample)
        assert live.seed_sample(runtime, sample) == first
        assert len(runtime.repo.list_media(live.TENANT)) == 1
        item = runtime.repo.get_media(live.TENANT, first)
        runtime.objects.verify_upload(live.TENANT, item['object_key'], size=item['bytes'], sha256=item['sha256'])
        assert runtime.repo.usage(live.TENANT)['pending_jobs'] == 0
    finally:
        runtime.repo.close()
