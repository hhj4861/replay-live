"""Real local FFmpeg with an in-memory HTTP control/object service; no network."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import httpx
import pytest

from server.worker import run_job
from server.media_runtime import MediaError, StreamResult
from server.output_policy import VIDEO_BITRATE_BPS, VIDEO_BUFFER_BITS, estimate_output_bytes
from server.stream_targets import LIVE_TARGETS, pin_destination, validate_destination, verify_host_pin
import server.worker as worker_module
import server.media_runtime as runtime_module


@pytest.fixture(scope='module')
def worker_clip(tmp_path_factory):
    path = tmp_path_factory.mktemp('worker-media') / 'valid.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x180:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=440',
                    '-t', '1', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                    '-c:a', 'aac', '-movflags', '+faststart', str(path)], check=True)
    return path.read_bytes()


def worker_config(data, target='local'):
    return dict(id='synthetic-job', tenant_id='synthetic-tenant', lease_version=1, version='test-release',
                mode='production', target=target, duration=1, progress=0, bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(), input={'url': 'https://objects.example/input'},
                callback_base='https://api.example/internal/jobs/synthetic-job', callback_token='synthetic-token',
                stream_key='synthetic-stream-key', deadline=time.time() + 120, lease_seconds=90,
                validation_timeout=10, max_duration=120,
                output_budget_bytes=estimate_output_bytes(1) if target == 'local' else 0)


class WorkerHTTP:
    def __init__(self, data, *, cancel=False, deny=False):
        self.data, self.cancel, self.deny = data, cancel, deny
        self.finished, self.outputs, self.requests = [], [], []
        self.finish_timeouts = []
        self.output_headers = []
        self.intent = None

    def __call__(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith('/heartbeat'):
            return httpx.Response(401 if self.deny else 200, json={'cancel_requested': self.cancel})
        if path == '/input':
            return httpx.Response(200, content=self.data)
        if path.endswith('/output'):
            self.intent = json.loads(request.content)
            return httpx.Response(200, json={'url': 'https://objects.example/result', 'headers': {'Content-Type': 'video/x-flv'}})
        if path == '/result':
            self.output_headers.append(request.headers)
            self.outputs.append(request.read())
            return httpx.Response(200)
        if path.endswith('/finish'):
            self.finished.append(json.loads(request.content))
            self.finish_timeouts.append(request.extensions['timeout'])
            return httpx.Response(401 if self.deny else 200, json={})
        raise AssertionError(f'Unexpected mocked route: {request.method} {path}')


def test_worker_validation_reports_actual_metadata_and_scrubs_credentials(worker_clip, tmp_path):
    remote = WorkerHTTP(worker_clip)
    job = worker_config(worker_clip, 'validate')
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'completed'
    assert result['metadata'] == dict(duration=1, width=320, height=180, fps=30)
    assert remote.finished == [result]
    assert set(remote.finish_timeouts[0].values()) == {10}
    assert not remote.outputs
    assert job['stream_key'] == job['callback_token'] == ''
    assert not list(tmp_path.iterdir()), 'Worker scratch directory leaked'


def import_config(data):
    job = worker_config(data, 'import')
    job.update(input=None, bytes=0, sha256='', duration=0, output_budget_bytes=len(data) * 2,
               source={'provider': 'direct', 'url': 'https://media.example/own.mp4?signature=synthetic'})
    return job


def install_import_fixture(monkeypatch, data, *, error=None):
    calls = []
    def download(source, output, **limits):
        limits['check_active']()
        calls.append((source.copy(), limits['max_bytes']))
        if error:
            raise worker_module.SourceImportError(error)
        output.write_bytes(data)
        return {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'name': '내 채널 녹화본.mp4'}
    monkeypatch.setattr(worker_module, 'download_source', download)
    return calls


def test_import_worker_validates_and_uploads_local_mp4_without_existing_input(worker_clip, tmp_path, monkeypatch):
    calls = install_import_fixture(monkeypatch, worker_clip)
    remote = WorkerHTTP(worker_clip)
    job = import_config(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert len(calls) == 1 and result['state'] == 'completed'
    assert result['metadata']['duration'] == 1 and result['metadata']['width'] == 320
    assert remote.outputs == [worker_clip]
    assert remote.intent == {'bytes': len(worker_clip), 'sha256': hashlib.sha256(worker_clip).hexdigest()}
    assert ('GET', '/input') not in remote.requests
    assert remote.finished == [result]
    assert job['source'] == {} and job['callback_token'] == ''
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('cancel', [False, True])
def test_import_requires_active_lease_before_source_access(worker_clip, tmp_path, monkeypatch, cancel):
    calls = install_import_fixture(monkeypatch, worker_clip)
    remote = WorkerHTTP(worker_clip, cancel=cancel, deny=not cancel)
    job = import_config(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        run_job(job, client=client, workdir=tmp_path)
    assert not calls and not remote.outputs and remote.intent is None
    assert job['source'] == {}


@pytest.mark.parametrize('invalid', ['duration', 'integrity', 'source_error'])
def test_import_failure_never_issues_upload(worker_clip, tmp_path, monkeypatch, invalid):
    data = b'not a video' if invalid == 'integrity' else worker_clip
    install_import_fixture(monkeypatch, data, error='SOURCE_UNAVAILABLE' if invalid == 'source_error' else None)
    job = import_config(worker_clip)
    if invalid == 'duration':
        job['max_duration'] = 0.5
    remote = WorkerHTTP(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'failed' and not remote.outputs and remote.intent is None
    assert set(remote.finish_timeouts[0].values()) == {10}
    if invalid == 'source_error':
        assert result['error_code'] == 'SOURCE_UNAVAILABLE'
    assert job['source'] == {} and not list(tmp_path.iterdir())


@pytest.mark.parametrize('phase', ['probe', 'decode'])
def test_import_cancellation_interrupts_validation_before_upload(worker_clip, tmp_path, monkeypatch, phase):
    install_import_fixture(monkeypatch, worker_clip)
    job = import_config(worker_clip)
    remote = WorkerHTTP(worker_clip)
    execute = runtime_module._execute
    interrupted = []
    def cancel_execute(command, **options):
        if command[0] == ('ffprobe' if phase == 'probe' else 'ffmpeg'):
            job['deadline'] = time.time() - 1
            assert options['should_stop']()
            interrupted.append(True)
        return execute(command, **options)
    monkeypatch.setattr(runtime_module, '_execute', cancel_execute)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert interrupted and result['state'] == 'stopped'
    assert remote.intent is None and not remote.outputs


def test_worker_local_stream_uploads_verified_h264_aac_flv(worker_clip, tmp_path):
    remote = WorkerHTTP(worker_clip)
    job = worker_config(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'completed' and result['progress'] == 1
    assert remote.finished == [result] and len(remote.outputs) == 1
    body = remote.outputs[0]
    assert body[:3] == b'FLV'
    assert hashlib.sha256(body).hexdigest() == result['output_sha256'] == remote.intent['sha256']
    assert len(body) == result['output_bytes'] == remote.intent['bytes']
    assert remote.output_headers[0]['Content-Length'] == str(len(body))
    assert 'Transfer-Encoding' not in remote.output_headers[0]
    output = tmp_path / 'verified.flv'
    output.write_bytes(body)
    info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(output)]))
    assert {stream['codec_name'] for stream in info['streams']} == {'h264', 'aac'}
    assert float(info['format']['duration']) >= 1


def test_worker_rejects_changed_input_before_streaming(worker_clip, tmp_path):
    remote = WorkerHTTP(worker_clip[:-1])
    job = worker_config(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'failed' and result['error_code'] == 'MEDIA_INTEGRITY_FAILED'
    assert remote.finished == [result] and not remote.outputs


def test_worker_requires_lease_before_download(worker_clip, tmp_path):
    remote = WorkerHTTP(worker_clip, deny=True)
    job = worker_config(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'failed'
    assert not any(method == 'GET' for method, _ in remote.requests)
    assert not remote.outputs and job['callback_token'] == job['stream_key'] == ''


def test_worker_cancel_returns_the_state_submitted_to_finish(worker_clip, tmp_path):
    remote = WorkerHTTP(worker_clip, cancel=True)
    job = worker_config(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert remote.finished[-1]['state'] == 'stopped'
    assert result['state'] == remote.finished[-1]['state']
    assert not remote.outputs


def test_worker_removes_invalid_secret_config_before_exit(tmp_path):
    config = tmp_path / 'job.json'
    config.write_text('{"stream_key":"synthetic-secret",broken')
    result = subprocess.run([sys.executable, '-m', 'server.worker', str(config)], capture_output=True, text=True)
    assert result.returncode != 0
    assert not config.exists()
    assert 'synthetic-secret' not in result.stdout + result.stderr


@pytest.mark.parametrize('budget', [None, 0, -1, True, '1024'])
def test_local_worker_requires_positive_integer_output_budget(worker_clip, tmp_path, budget):
    remote = WorkerHTTP(worker_clip)
    job = worker_config(worker_clip)
    if budget is None:
        job.pop('output_budget_bytes')
    else:
        job['output_budget_bytes'] = budget
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'failed' and result['error_code'] == 'OUTPUT_BUDGET_MISSING'
    assert not any(method == 'GET' for method, _ in remote.requests)
    assert remote.intent is None and not remote.outputs


def test_real_encoder_over_budget_fails_without_upload_or_retry(worker_clip, tmp_path):
    remote = WorkerHTTP(worker_clip)
    job = worker_config(worker_clip)
    job['output_budget_bytes'] = 4096
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'failed' and result['error_code'] == 'OUTPUT_LIMIT_EXCEEDED'
    assert remote.finished == [result]
    assert remote.intent is None and not remote.outputs
    assert job['stream_key'] == job['callback_token'] == ''


def test_worker_checks_actual_output_even_if_encoder_reports_success(worker_clip, tmp_path, monkeypatch):
    remote = WorkerHTTP(worker_clip)
    job = worker_config(worker_clip)
    job['output_budget_bytes'] = 1024
    def oversized_result(command, **kwargs):
        kwargs['local_output'].write_bytes(b'x' * (kwargs['max_output_bytes'] + 1))
        return StreamResult(0, 1, True, None, '')
    monkeypatch.setattr(worker_module, 'run_stream', oversized_result)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'failed' and result['error_code'] == 'OUTPUT_LIMIT_EXCEEDED'
    assert remote.intent is None and not remote.outputs


def test_worker_rechecks_file_budget_after_signed_upload_grant(worker_clip, tmp_path, monkeypatch):
    job = worker_config(worker_clip)
    job['output_budget_bytes'] = 1024
    output_files = []
    def successful_result(command, **kwargs):
        output_files.append(kwargs['local_output'])
        kwargs['local_output'].write_bytes(b'synthetic-output')
        return StreamResult(0, 1, True, None, '')
    monkeypatch.setattr(worker_module, 'run_stream', successful_result)
    class ChangedOutputHTTP(WorkerHTTP):
        def __call__(self, request):
            response = super().__call__(request)
            if request.url.path.endswith('/output'):
                output_files[0].write_bytes(b'x' * 1025)
            return response
    remote = ChangedOutputHTTP(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'failed' and result['error_code'] == 'OUTPUT_LIMIT_EXCEEDED'
    assert remote.intent is not None
    assert not any(method == 'PUT' for method, _ in remote.requests)


def test_upload_reader_never_yields_more_than_reserved_file_bytes(tmp_path):
    output = tmp_path / 'growing.flv'
    size = 1024 * 1024 + 64
    output.write_bytes(b'x' * size)
    with output.open('rb') as handle:
        chunks = worker_module.output_chunks(handle, size, size + 16)
        sent = len(next(chunks))
        with output.open('ab') as writer:
            writer.write(b'x' * 32)
        with pytest.raises(MediaError) as error:
            while True:
                sent += len(next(chunks))
        assert error.value.code == 'OUTPUT_LIMIT_EXCEEDED'
        assert sent == size


def test_output_reader_does_not_yield_a_chunk_read_after_lease_loss(tmp_path):
    output = tmp_path / 'output.flv'
    output.write_bytes(b'synthetic-output')
    active = [True]

    def check_active():
        if not active[0]:
            raise MediaError('LEASE_LOST', 'synthetic lease expired')

    with output.open('rb') as source:
        class LosingLeaseReader:
            def fileno(self):
                return source.fileno()

            def read(self, size):
                chunk = source.read(size)
                active[0] = False
                return chunk

        chunks = worker_module.output_chunks(LosingLeaseReader(), output.stat().st_size,
                                            1024, check_active=check_active)
        with pytest.raises(MediaError, match='synthetic lease expired'):
            next(chunks)


@pytest.mark.parametrize('phase', ['checksum', 'grant', 'put_chunk', 'put_response', 'finish_response'])
def test_real_worker_stops_output_work_at_deadline(worker_clip, tmp_path, monkeypatch, phase):
    """Encode a real FLV; expire at controlled HTTP/read boundaries without network."""
    job = worker_config(worker_clip)
    job['deadline'] = time.time() + 20
    remote = WorkerHTTP(worker_clip)
    sent = bytearray()
    timeouts = []
    original_chunks = worker_module.output_chunks

    def expire_during_checksum(*args, **kwargs):
        for chunk in original_chunks(*args, **kwargs):
            yield chunk
            if phase == 'checksum':
                job['deadline'] = time.time() - 1

    monkeypatch.setattr(worker_module, 'output_chunks', expire_during_checksum)

    class DeadlineTransport(httpx.BaseTransport):
        def handle_request(self, request):
            path = request.url.path
            if path.endswith('/output') or path == '/result':
                remaining = job['deadline'] - time.time()
                values = request.extensions['timeout'].values()
                assert all(0 < value <= remaining + .1 for value in values)
                timeouts.append(path)
            if path == '/result':
                remote.requests.append((request.method, path))
                remote.output_headers.append(request.headers)
                for chunk in request.stream:
                    sent.extend(chunk)
                    if phase == 'put_chunk':
                        job['deadline'] = time.time() - 1
                assert request.headers['Content-Length'] == str(len(sent))
                assert 'Transfer-Encoding' not in request.headers
                remote.outputs.append(bytes(sent))
                if phase == 'put_response':
                    job['deadline'] = time.time() - 1
                return httpx.Response(200)
            request.read()
            response = remote(request)
            if path.endswith('/output') and phase == 'grant':
                job['deadline'] = time.time() - 1
            if path.endswith('/finish') and phase == 'finish_response':
                job['deadline'] = time.time() - 1
                return httpx.Response(200, json={'state': 'completed'})
            return response

    with httpx.Client(transport=DeadlineTransport()) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] != 'completed'
    assert job['stream_key'] == job['callback_token'] == ''
    assert not list(tmp_path.iterdir())
    if phase == 'finish_response':
        assert result['error_code'] == 'COMPLETION_UNCONFIRMED'
        assert remote.finished[0]['state'] == 'completed', 'Deadline crossed only after the finish request'
    else:
        assert result['state'] == 'stopped'
        assert remote.finished == [result]
        assert 'output_bytes' not in result
    if phase == 'checksum':
        assert remote.intent is None and not timeouts
    elif phase == 'grant':
        assert remote.intent is not None and timeouts == ['/internal/jobs/synthetic-job/output']
    if phase in ('checksum', 'grant'):
        assert not sent and not remote.outputs
    elif phase == 'put_chunk':
        assert sent[:3] == b'FLV' and not remote.outputs
    else:
        assert len(remote.outputs) == 1 and sent[:3] == b'FLV'


@pytest.mark.parametrize('target', sorted(LIVE_TARGETS))
def test_each_live_platform_uses_its_pinned_destination_and_scrubs_secrets(worker_clip, tmp_path, monkeypatch, target, caplog):
    job = worker_config(worker_clip, target)
    job['progress'] = .25
    job['stream_destination'] = pin_destination(validate_destination(target,
        'rtmps://ingest.example.com:443/live', 'synthetic-platform-key?token=value&expires=99'),
        resolver=lambda host, port: ['8.8.8.8'])
    hosts = tmp_path / 'synthetic-hosts'
    hosts.write_text('8.8.8.8 ingest.example.com\n')
    monkeypatch.setattr(worker_module, 'verify_host_pin', lambda value:
        verify_host_pin(value, hosts_path=hosts, resolver=lambda host, port: ['8.8.8.8']))
    calls = []
    def stream(command, **kwargs):
        calls.append((command, kwargs))
        assert command[-1] == 'rtmps://ingest.example.com:443/live/synthetic-platform-key?token=value&expires=99'
        assert command[command.index('-tls_verify') + 1] == '1'
        assert command[command.index('-rtmp_playpath') + 1] == 'synthetic-platform-key?token=value&expires=99'
        assert command[command.index('-rtmp_app') + 1] == 'live'
        assert command[command.index('-rtmp_tcurl') + 1] == 'rtmps://ingest.example.com:443/live'
        assert command[command.index('-g') + 1] == ('30' if target == 'chzzk' else '60')
        assert command[command.index('-keyint_min') + 1] == command[command.index('-g') + 1]
        assert command[command.index('-r') + 1] == '30'
        assert command[command.index('-nal-hrd') + 1] == 'cbr'
        bitrate = 3_000_000 if target == 'twitch' else VIDEO_BITRATE_BPS
        buffer = 6_000_000 if target == 'twitch' else VIDEO_BUFFER_BITS
        for option in ('-b:v', '-maxrate', '-minrate'):
            assert int(command[command.index(option) + 1]) == bitrate
        assert int(command[command.index('-bufsize') + 1]) == buffer
        assert kwargs['offset'] == .25 and kwargs['local_output'] is None
        assert kwargs['expected_duration'] == 1
        assert kwargs['max_output_bytes'] is None
        assert 'synthetic-platform-key?token=value&expires=99' in kwargs['redactions']
        kwargs['on_progress'](1)
        return StreamResult(0, 1, True, None, '')
    monkeypatch.setattr(worker_module, 'run_stream', stream)
    remote = WorkerHTTP(worker_clip)
    with caplog.at_level('INFO'), httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'completed' and len(calls) == 1
    assert set(remote.finish_timeouts[0].values()) == {10}
    assert remote.intent is None and not remote.outputs
    assert job['stream_destination'] == {} and job['stream_key'] == job['callback_token'] == ''
    assert 'synthetic-platform-key' not in caplog.text and 'ingest.example.com' not in caplog.text


@pytest.mark.parametrize('target', ['local', 'import'])
@pytest.mark.parametrize('cancel_during_verification', [False, True])
def test_file_verification_finish_keeps_heartbeats_active_and_rejects_lost_lease(
        worker_clip, tmp_path, monkeypatch, target, cancel_during_verification):
    """Hold only the synthetic finish response until a real heartbeat completes."""
    job = import_config(worker_clip) if target == 'import' else worker_config(worker_clip)
    job.update(lease_seconds=180, deadline=time.time() + 300)
    if target == 'import':
        install_import_fixture(monkeypatch, worker_clip)
    finish_started = threading.Event()
    refreshed_during_finish = threading.Event()
    original_beat = worker_module.LeaseClient.beat
    renewed_at = []

    def tracked_beat(lease):
        original_beat(lease)
        if finish_started.is_set():
            renewed_at.append(lease.last_success)
            refreshed_during_finish.set()

    monkeypatch.setattr(worker_module.LeaseClient, 'beat', tracked_beat)

    class VerifyingHTTP(WorkerHTTP):
        def __call__(self, request):
            if request.url.path.endswith('/finish'):
                response = super().__call__(request)
                assert set(request.extensions['timeout'].values()) == {90}
                assert self.outputs and self.finished[-1]['state'] == 'completed'
                self.cancel = cancel_during_verification
                finish_started.set()
                assert refreshed_during_finish.wait(8), 'Heartbeat stopped while storage verification was pending'
                assert response.status_code == 200
                return httpx.Response(200, json={'state': 'completed'})
            return super().__call__(request)

    remote = VerifyingHTTP(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert renewed_at and remote.finished[0]['state'] == 'completed'
    assert sum(path.endswith('/heartbeat') for _, path in remote.requests) >= 2
    if cancel_during_verification:
        assert result['state'] == 'failed' and result['error_code'] == 'COMPLETION_UNCONFIRMED'
    else:
        assert result['state'] == 'completed' and result['output_sha256'] == remote.intent['sha256']
    assert job['stream_key'] == job['callback_token'] == '' and not list(tmp_path.iterdir())


@pytest.mark.parametrize('target', ['local', 'import'])
@pytest.mark.parametrize('boundary', ['deadline', 'lease'])
def test_file_verification_timeout_is_capped_by_the_remaining_execution_boundary(
        worker_clip, tmp_path, monkeypatch, target, boundary):
    job = import_config(worker_clip) if target == 'import' else worker_config(worker_clip)
    job.update(lease_seconds=180, deadline=time.time() + 300)
    if target == 'import':
        install_import_fixture(monkeypatch, worker_clip)

    class BoundedHTTP(WorkerHTTP):
        def __call__(self, request):
            response = super().__call__(request)
            if request.url.path == '/result':
                if boundary == 'deadline':
                    job['deadline'] = time.time() + 8
                else:
                    job['lease_seconds'] = 25  # Five seconds minus time since the latest heartbeat.
            if request.url.path.endswith('/finish'):
                upper_bound = job['deadline'] - time.time() + .1 if boundary == 'deadline' else 5
                assert all(0 < value <= upper_bound for value in request.extensions['timeout'].values())
                return httpx.Response(200, json={'state': 'completed'})
            return response

    remote = BoundedHTTP(worker_clip)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'completed' and len(remote.finished) == 1
    assert remote.outputs and job['callback_token'] == ''


def test_live_worker_refuses_missing_pins_before_download(worker_clip, tmp_path):
    remote = WorkerHTTP(worker_clip)
    job = worker_config(worker_clip, 'youtube')
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = run_job(job, client=client, workdir=tmp_path)
    assert result['state'] == 'failed'
    assert not any(method == 'GET' for method, _ in remote.requests)
    assert not remote.outputs
