"""Local-only regressions for media correctness and scheduler failure recovery."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from server.media_runtime import MediaError, require_verified_rtmps, run_stream, stream_command, validate_media
from server.service import Service
from server.output_policy import AUDIO_BITRATE_BPS, VIDEO_BITRATE_BPS, VIDEO_BUFFER_BITS, estimate_output_bytes


@pytest.fixture(scope='module')
def media_clips(tmp_path_factory):
    root = tmp_path_factory.mktemp('reliability-media')
    paths = {}
    for seconds in (1, 6):
        path = root / f'{seconds}.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                        'testsrc2=size=320x180:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=440',
                        '-t', str(seconds), '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt',
                        'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(path)], check=True)
        paths[seconds] = path
    return paths


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError('Condition did not become true before timeout')


def test_truncated_faststart_mp4_is_rejected_after_metadata_probe(media_clips, tmp_path):
    original = media_clips[6].read_bytes()
    damaged = tmp_path / 'truncated.mp4'
    damaged.write_bytes(original[:len(original) // 2])
    assert validate_media(media_clips[6])['duration'] == 6
    # Header still claims six seconds, reproducing the previously accepted upload.
    duration = subprocess.check_output(['ffprobe', '-v', 'fatal', '-show_entries', 'format=duration',
                                        '-of', 'default=nw=1:nk=1', str(damaged)])
    assert float(duration) == 6
    with pytest.raises(MediaError) as error:
        validate_media(damaged)
    assert error.value.code == 'MEDIA_DECODE_FAILED'


def test_clean_exit_with_partial_progress_is_never_completed():
    result = run_stream([sys.executable, '-c', 'print("out_time_us=3085000\\nprogress=end")'],
                        expected_duration=6)
    assert not result.complete
    assert result.error_code == 'STREAM_INCOMPLETE'
    assert result.progress == 3.085


def test_real_output_probe_and_progress(media_clips, tmp_path):
    output = tmp_path / 'complete.flv'
    positions = []
    result = run_stream(stream_command(media_clips[1], output), expected_duration=1,
                        local_output=output, on_progress=positions.append, max_output_bytes=estimate_output_bytes(1))
    assert result.complete, result
    assert result.progress == 1 and .75 <= positions[-1] <= 1
    missing = run_stream([sys.executable, '-c', 'print("out_time_us=1000000\\nprogress=end")'],
                         expected_duration=1, local_output=tmp_path / 'missing.flv')
    assert missing.error_code == 'STREAM_INCOMPLETE'


def test_encoder_profile_uses_shared_admission_constants(media_clips, tmp_path):
    command = stream_command(media_clips[1], tmp_path / 'profile.flv')
    assert int(command[command.index('-b:v') + 1]) == VIDEO_BITRATE_BPS
    assert int(command[command.index('-maxrate') + 1]) == VIDEO_BITRATE_BPS
    assert int(command[command.index('-bufsize') + 1]) == VIDEO_BUFFER_BITS
    assert int(command[command.index('-b:a') + 1]) == AUDIO_BITRATE_BPS
    assert command[command.index('-g') + 1] == '60'
    assert '-minrate' not in command and '-nal-hrd' not in command


@pytest.mark.parametrize('field,maximum', [('video_bitrate_bps', VIDEO_BITRATE_BPS), ('video_buffer_bits', VIDEO_BUFFER_BITS)])
@pytest.mark.parametrize('invalid', [None, True, False, 0, -1, 3_000_000.0, '3000000', float('nan'), float('inf'), 'above-maximum'])
def test_encoder_profile_rejects_invalid_bitrate_or_buffer(field, maximum, invalid):
    value = maximum + 1 if invalid == 'above-maximum' else invalid
    with pytest.raises(MediaError) as error:
        stream_command('input.mp4', 'output.flv', **{field: value})
    assert error.value.code == 'STREAM_PROFILE_INVALID'


@pytest.mark.parametrize('interval,bitrate,buffer', [(1, VIDEO_BITRATE_BPS, VIDEO_BUFFER_BITS),
    (2, VIDEO_BITRATE_BPS, VIDEO_BUFFER_BITS), (2, 3_000_000, 6_000_000)])
def test_real_live_profile_keyframe_interval_and_cbr_stay_within_output_budget(media_clips, tmp_path, interval, bitrate, buffer):
    output = tmp_path / f'live-{interval}s.flv'
    command = stream_command(media_clips[6], output, keyframe_seconds=interval, constant_bitrate=True,
        video_bitrate_bps=bitrate, video_buffer_bits=buffer)
    command.remove('-re')
    budget = estimate_output_bytes(6)
    result = run_stream(command, expected_duration=6, local_output=output, max_output_bytes=budget)
    assert result.complete and output.stat().st_size < budget
    probed = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-skip_frame', 'nokey', '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(output)]))
    positions = [float(frame['best_effort_timestamp_time']) for frame in probed['frames']]
    assert len(positions) == 6 // interval
    assert [right - left for left, right in zip(positions, positions[1:])] == pytest.approx([interval] * (len(positions) - 1), abs=.001)
    headers = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'info', '-i', str(output),
        '-map', '0:v:0', '-c:v', 'copy', '-frames:v', '1', '-bsf:v', 'trace_headers', '-f', 'null', '-'],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=True)
    assert re.search(r'cbr_flag\[0\].*= 1', headers.stderr), 'H.264 bitstream must signal CBR HRD'
    bit_rate_scale = int(re.search(r'\bbit_rate_scale\b.*= (\d+)', headers.stderr).group(1))
    bit_rate_value = int(re.search(r'bit_rate_value_minus1\[0\].*= (\d+)', headers.stderr).group(1))
    assert (bit_rate_value + 1) * (1 << (6 + bit_rate_scale)) == bitrate
    cpb_size_scale = int(re.search(r'\bcpb_size_scale\b.*= (\d+)', headers.stderr).group(1))
    cpb_size_value = int(re.search(r'cpb_size_value_minus1\[0\].*= (\d+)', headers.stderr).group(1))
    assert (cpb_size_value + 1) * (1 << (4 + cpb_size_scale)) == buffer
    measured = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'packet=size,pts_time:stream=width,height,avg_frame_rate', '-of', 'json', str(output)]))
    assert measured['streams'][0] == {'width': 320, 'height': 180, 'avg_frame_rate': '30/1'}
    assert len(measured['packets']) == 6 * 30
    # Video presentation span excludes the muxer's audio priming/start offsets.
    timestamps = [float(packet['pts_time']) for packet in measured['packets']]
    assert max(timestamps) - min(timestamps) + 1 / 30 == pytest.approx(6, abs=.002)
    measured_bitrate = sum(int(packet['size']) for packet in measured['packets']) * 8 / 6
    assert .85 * bitrate <= measured_bitrate <= 1.1 * bitrate


def test_commercial_portrait_validation_and_encoding_preserve_poc_landscape_rule(tmp_path):
    source = tmp_path / 'portrait.mp4'
    output = tmp_path / 'portrait.flv'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                    'testsrc2=size=180x320:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=440',
                    '-t', '1', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt',
                    'yuv420p', '-c:a', 'aac', str(source)], check=True)
    with pytest.raises(MediaError):
        validate_media(source)
    metadata = validate_media(source, allow_portrait=True)
    assert (metadata['width'], metadata['height']) == (180, 320)
    result = run_stream(stream_command(source, output), expected_duration=1,
        local_output=output, max_output_bytes=estimate_output_bytes(1))
    assert result.complete and result.progress == 1


@pytest.mark.parametrize('configuration', ['--enable-openssl', '--enable-gnutls --enable-openssl',
                                         '--enable-gnutls --enable-librtmp', '', '--disable-gnutls'])
def test_commercial_rtmps_rejects_unverified_tls_backends(monkeypatch, configuration):
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: configuration)
    with pytest.raises(MediaError) as error:
        require_verified_rtmps()
    assert error.value.code == 'STREAM_TLS_UNVERIFIED'


@pytest.mark.parametrize('profile', [{}, {'constant_bitrate': True, 'video_bitrate_bps': 3_000_000, 'video_buffer_bits': 6_000_000}],
                         ids=['local-default', 'twitch-cbr'])
def test_ffmpeg_size_limit_clean_exit_cannot_report_success(media_clips, tmp_path, profile):
    raw_output = tmp_path / 'ffmpeg-fs.flv'
    command = stream_command(media_clips[6], raw_output, **profile)
    command.remove('-re')
    command[-1:-1] = ['-fs', '4096']
    # FFmpeg itself can signal success despite ending a six-second clip early.
    raw = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert raw.returncode == 0 and 'progress=end' in raw.stdout
    info = subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                    '-of', 'default=nw=1:nk=1', str(raw_output)])
    assert float(info) < 6 - .25
    output = tmp_path / 'enforced.flv'
    limited = stream_command(media_clips[6], output, **profile)
    limited.remove('-re')
    result = run_stream(limited, expected_duration=6, local_output=output, max_output_bytes=4096)
    assert not result.complete and result.error_code == 'OUTPUT_LIMIT_EXCEEDED'
    assert output.stat().st_size <= 4096


def test_os_file_ceiling_cannot_be_bypassed_by_zero_exit(tmp_path):
    output = tmp_path / 'capped.flv'
    script = ('from pathlib import Path\n'
              f'try: Path({str(output)!r}).write_bytes(b"x"*8192)\n'
              'except OSError: pass\n'
              'print("out_time_us=1000000\\nprogress=end")\n')
    result = run_stream([sys.executable, '-c', script], expected_duration=1,
                        local_output=output, max_output_bytes=4096)
    assert not result.complete and result.error_code == 'OUTPUT_LIMIT_EXCEEDED'
    assert output.stat().st_size <= 4096


def test_diagnostics_are_bounded_and_credentials_redacted():
    secret = 'fake-stream-key-1234567890'
    script = f'import sys; sys.stderr.write("z"*20000 + " rtmps://invalid.test/live2/{secret} stream_key={secret} {secret}"); sys.exit(1)'
    result = run_stream([sys.executable, '-c', script], expected_duration=1, redactions=(secret,))
    assert result.error_code == 'STREAM_EXIT'
    assert len(result.diagnostic) <= 2048
    assert secret not in result.diagnostic and 'invalid.test' not in result.diagnostic
    assert '[REDACTED' in result.diagnostic


def test_watchdog_kills_process_group_and_deadline_prevents_start(tmp_path):
    marker = tmp_path / 'orphan-ran'
    child = f'import time; from pathlib import Path; time.sleep(1.5); Path({str(marker)!r}).touch()'
    script = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)'
    processes = []
    result = run_stream([sys.executable, '-c', script], expected_duration=1, stall_timeout=.2,
                        on_start=processes.append)
    assert result.error_code == 'STREAM_STALLED'
    assert processes[0].poll() is not None
    time.sleep(1.6)
    assert not marker.exists()
    processes.clear()
    expired = run_stream([sys.executable, '-c', 'raise RuntimeError("must not start")'],
                         expected_duration=1, deadline=time.time() - 1, on_start=processes.append)
    assert expired.error_code == 'DEADLINE_EXCEEDED' and not processes


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux prctl parent-death protection')
def test_linux_encoder_dies_when_watchdog_parent_is_killed(tmp_path):
    pidfile = tmp_path / 'encoder.pid'
    command = 'from pathlib import Path; import os,time; Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid())); time.sleep(30)'
    script = 'from server.media_runtime import run_stream; import sys; run_stream([sys.executable,"-c",' + repr(command) + '],expected_duration=10)'
    parent = subprocess.Popen([sys.executable, '-c', script])
    encoder = None
    try:
        wait_for(pidfile.exists)
        encoder = int(pidfile.read_text())
        parent.kill()
        parent.wait(timeout=5)
        def encoder_exited():
            stat = Path(f'/proc/{encoder}/stat')
            return not stat.exists() or stat.read_text().split()[2] == 'Z'
        wait_for(encoder_exited, timeout=5)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if encoder:
            try:
                os.killpg(encoder, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_database_lock_recovers_scheduler_and_does_not_consume_shared_capacity(tmp_path, media_clips):
    capacity = threading.BoundedSemaphore(1)
    first = Service(tmp_path / 'one', capacity=capacity, sqlite_timeout=.04, poll_interval=.02)
    second = Service(tmp_path / 'two', capacity=capacity, sqlite_timeout=.04, poll_interval=.02)
    lock = None
    try:
        first_media = first.add_media(media_clips[1], 'one.mp4')
        second_media = second.add_media(media_clips[1], 'two.mp4')
        first_job = first.create_job(first_media['id'], '잠긴 DB', 'local')
        second_job = second.create_job(second_media['id'], '다른 사용자', 'local')
        lock = sqlite3.connect(first.db_path)
        lock.execute('BEGIN IMMEDIATE')
        first.start()
        wait_for(lambda: first.last_error == 'DB_UNAVAILABLE')
        assert first.scheduler.is_alive() and not first.readiness()['ready']
        assert capacity.acquire(blocking=False), 'A failed database claim leaked shared capacity'
        capacity.release()
        second.start()
        wait_for(lambda: second.get_job(second_job['id'])['state'] == 'completed')
        lock.rollback()
        lock.close()
        lock = None
        wait_for(lambda: first.get_job(first_job['id'])['state'] == 'completed')
        wait_for(lambda: first.readiness()['ready'])
        assert first.get_job(first_job['id'])['attempt'] == 1
    finally:
        if lock:
            lock.rollback()
            lock.close()
        first.close()
        second.close()


def test_worker_start_failure_releases_capacity(tmp_path, media_clips, monkeypatch):
    capacity = threading.BoundedSemaphore(1)
    service = Service(tmp_path / 'failed-start', capacity=capacity)
    try:
        media = service.add_media(media_clips[1], 'one.mp4')
        job = service.create_job(media['id'], '실패', 'local')
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, 'start', lambda self: (_ for _ in ()).throw(RuntimeError('injected')))
            with pytest.raises(RuntimeError, match='injected'):
                service.tick()
        assert capacity.acquire(blocking=False)
        capacity.release()
        service.tick()
        assert service.get_job(job['id'])['error_code'] == 'PROCESS_START_FAILED'
    finally:
        service.close()


def test_cancel_between_claim_and_process_start_never_launches_encoder(tmp_path, media_clips):
    service = Service(tmp_path / 'cancel-race', poll_interval=.02)
    preparing, release = threading.Event(), threading.Event()
    original = service.command
    def delayed_command(*args):
        preparing.set()
        assert release.wait(5)
        return original(*args)
    service.command = delayed_command
    try:
        media = service.add_media(media_clips[1], 'one.mp4')
        job = service.create_job(media['id'], '시작 직전 취소', 'local')
        service.start()
        assert preparing.wait(5)
        assert service.stop(job['id'])['state'] == 'stopped'
        release.set()
        wait_for(lambda: not any(worker.is_alive() for worker in service.threads))
        assert service.get_job(job['id'])['state'] == 'stopped'
        assert not list((service.root / 'outputs').glob('*.flv'))
    finally:
        release.set()
        service.close()


def test_final_state_survives_transient_database_lock(tmp_path, media_clips):
    capacity = threading.BoundedSemaphore(1)
    service = Service(tmp_path / 'finish-lock', capacity=capacity, sqlite_timeout=.03, poll_interval=.02)
    lock = None
    try:
        media = service.add_media(media_clips[1], 'one.mp4')
        job = service.create_job(media['id'], '완료 저장 재시도', 'local')
        service.start()
        wait_for(lambda: bool(service.processes))
        lock = sqlite3.connect(service.db_path)
        lock.execute('BEGIN IMMEDIATE')
        wait_for(lambda: bool(service.pending) and not service.processes)
        wait_for(lambda: capacity.acquire(blocking=False))
        capacity.release()
        assert service.pending[job['id']][0] == 'completed'
        lock.rollback()
        lock.close()
        lock = None
        wait_for(lambda: service.get_job(job['id'])['state'] == 'completed')
        assert service.get_job(job['id'])['attempt'] == 1
    finally:
        if lock:
            lock.rollback()
            lock.close()
        service.close()


def test_atomic_idempotency_persists_across_restart(tmp_path, media_clips):
    root = tmp_path / 'idempotency'
    service = Service(root)
    try:
        media = service.add_media(media_clips[1], 'one.mp4')
        def create():
            return service.create_job(media['id'], '중복 요청', 'local', idempotency_key='request-1')['id']
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda _: create(), range(12)))
        assert len(set(ids)) == 1 and len(service.list_jobs()) == 1
        with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
            service.create_job(media['id'], '다른 요청', 'local', idempotency_key='request-1')
    finally:
        service.close()
    resumed = Service(root)
    try:
        assert resumed.create_job(media['id'], '중복 요청', 'local', idempotency_key='request-1')['id'] == ids[0]
    finally:
        resumed.close()


def test_deadline_admission_counts_backlog_and_dispatch_rechecks(tmp_path, media_clips):
    service = Service(tmp_path / 'deadlines')
    try:
        media = service.add_media(media_clips[1], 'one.mp4')
        deadline = time.time() + 240
        job = service.create_job(media['id'], '첫 작업', 'local', deadline=deadline)
        with pytest.raises(ValueError, match='DEADLINE_CAPACITY'):
            service.create_job(media['id'], '초과 작업', 'local', deadline=deadline)
        with service.db() as db:
            db.execute('UPDATE jobs SET deadline=? WHERE id=?', (time.time() + .1, job['id']))
        service.tick()
        finished = service.get_job(job['id'])
        assert finished['error_code'] == 'DEADLINE_EXCEEDED' and finished['attempt'] == 0
        assert not service.processes
    finally:
        service.close()
