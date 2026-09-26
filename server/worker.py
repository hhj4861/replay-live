"""One job, one isolated process/VM. Receives only presigned objects and a lease token.

No database, KMS, cloud account, or other tenant credentials are accepted here.
"""
import argparse
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from urllib.parse import urlsplit

import httpx

from .media_runtime import MediaError, require_verified_rtmps, run_stream, stream_command, validate_media
from .media_sources import SourceImportError, download_source
from .output_policy import MAX_OUTPUT_BYTES
from .stream_targets import (LIVE_TARGETS, STREAM_TARGETS, StreamTargetError,
                             destination_url, verify_host_pin)


class LeaseClient:
    def __init__(self, job, client=None):
        self.job = job
        self.http = client or httpx.Client(timeout=10, follow_redirects=False)
        self.owns_client = client is None
        self.stop = threading.Event()
        self.done = threading.Event()
        self.progress = float(job.get('progress', 0))
        self.last_success = time.monotonic()
        self.thread = threading.Thread(target=self._loop, name='lease-heartbeat', daemon=True)

    def call(self, path, body, *, timeout=None):
        result = self.http.post(self.job['callback_base'] + path, json=body,
                headers={'Authorization': 'Bearer ' + self.job['callback_token']},
                **({'timeout': timeout} if timeout is not None else {}))
        result.raise_for_status()
        return result.json()

    def beat(self):
        response = self.call('/heartbeat', {'progress': self.progress})
        self.last_success = time.monotonic()
        if response.get('cancel_requested'):
            self.stop.set()

    def _loop(self):
        while not self.done.wait(3):
            try:
                self.beat()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 403, 404, 409):
                    self.stop.set()
            except Exception:
                pass
            if time.monotonic() - self.last_success >= max(1, self.job['lease_seconds'] - 20):
                self.stop.set()

    def should_stop(self):
        return (self.stop.is_set() or time.time() >= self.job['deadline']
                or time.monotonic() - self.last_success >= max(1, self.job['lease_seconds'] - 20))

    def require_active(self):
        if self.should_stop():
            code = 'DEADLINE_EXCEEDED' if time.time() >= self.job['deadline'] else 'LEASE_LOST'
            raise MediaError(code, '작업 실행 권한 또는 이용 시간이 종료되었습니다.')

    def active_timeout(self, maximum):
        self.require_active()
        remaining = min(self.job['deadline'] - time.time(),
                        max(1, self.job['lease_seconds'] - 20) - (time.monotonic() - self.last_success))
        if remaining <= 0:
            self.require_active()
            raise MediaError('LEASE_LOST', '작업 실행 권한이 종료되었습니다.')
        # HTTPX bounds individual network operations, not total wall-clock time.
        # Chunk guards and response checks also enforce the execution boundary.
        return min(maximum, remaining)

    def close(self):
        self.done.set()
        if self.thread.is_alive():
            self.thread.join(timeout=12)
        if self.owns_client:
            self.http.close()


def checked_url(value, development):
    parsed = urlsplit(value)
    if parsed.scheme not in (('https', 'http') if development else ('https',)) or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Invalid worker URL')
    if not development and parsed.hostname in ('localhost', '127.0.0.1', '169.254.169.254'):
        raise ValueError('Local worker destination is forbidden')
    return value


def output_chunks(handle, expected_size, budget, *, check_active=None):
    """Bound both checksum reads and uploaded bytes to the approved file size."""
    if check_active:
        check_active()
    size = os.fstat(handle.fileno()).st_size
    if size > budget:
        raise MediaError('OUTPUT_LIMIT_EXCEEDED', '출력 파일이 예약된 용량을 초과했습니다.')
    if size != expected_size:
        raise MediaError('MEDIA_INTEGRITY_FAILED', '출력 파일의 크기가 변경되었습니다.')
    remaining = expected_size
    while remaining:
        if check_active:
            check_active()
        chunk = handle.read(min(1024 * 1024, remaining))
        if check_active:
            check_active()
        if not chunk:
            raise MediaError('MEDIA_INTEGRITY_FAILED', '출력 파일을 끝까지 읽을 수 없습니다.')
        remaining -= len(chunk)
        yield chunk
        if check_active:
            check_active()
    size = os.fstat(handle.fileno()).st_size
    if size > budget:
        raise MediaError('OUTPUT_LIMIT_EXCEEDED', '출력 파일이 예약된 용량을 초과했습니다.')
    if size != expected_size:
        raise MediaError('MEDIA_INTEGRITY_FAILED', '출력 파일의 크기가 변경되었습니다.')
    if check_active:
        check_active()


def run_job(job, *, client=None, workdir=None):
    """Runs synchronously; HTTP transport injection permits real-media integration tests."""
    development = job.get('mode') == 'development'
    checked_url(job['callback_base'], development)
    if job['target'] != 'import':
        checked_url(job['input']['url'], development)
    if job['target'] not in STREAM_TARGETS | {'validate', 'import'}:
        raise ValueError('Invalid target')
    lease = LeaseClient(job, client)
    result = {'state': 'failed', 'error_code': 'WORKER_FAILED'}
    with tempfile.TemporaryDirectory(prefix='replay-job-', dir=workdir) as scratch:
        source = Path(scratch) / 'source.mp4'
        output = Path(scratch) / 'output.flv'
        try:
            destination = None
            if job['target'] in LIVE_TARGETS:
                # The API/dispatcher upgrades legacy YouTube keys to this shape.
                # Missing VM pins must fail closed before media or RTMP access.
                destination = verify_host_pin(job.get('stream_destination'))
                if destination['target'] != job['target']:
                    raise StreamTargetError()
                if destination['protocol'] == 'rtmps':
                    require_verified_rtmps()
            output_budget = job.get('output_budget_bytes') if job['target'] in ('local', 'import') else None
            if job['target'] in ('local', 'import'):
                if isinstance(output_budget, bool) or not isinstance(output_budget, int) or output_budget <= 0:
                    raise MediaError('OUTPUT_BUDGET_MISSING', '예약된 출력 용량이 없어 작업을 시작할 수 없습니다.')
                if output_budget > MAX_OUTPUT_BYTES:
                    raise MediaError('OUTPUT_BUDGET_INVALID', '출력 용량 제한이 허용 범위를 초과했습니다.')
            # A successful heartbeat is required before downloading or touching the RTMP endpoint.
            lease.beat()
            lease.thread.start()
            imported = None
            lease.require_active()
            if job['target'] == 'import':
                if (job.get('mode') == 'production' and job.get('source', {}).get('provider') == 'youtube'
                        and not os.environ.get('REPLAY_SOURCE_PROXY_URL')):
                    raise SourceImportError('SOURCE_PROXY_UNAVAILABLE')
                imported = download_source(job.get('source'), source, max_bytes=output_budget,
                    max_duration=job['max_duration'], timeout=min(job['validation_timeout'], max(1, job['deadline'] - time.time())),
                    check_active=lease.require_active, proxy_url=os.environ.get('REPLAY_SOURCE_PROXY_URL'))
                lease.require_active()
            else:
                sha, size = hashlib.sha256(), 0
                with lease.http.stream('GET', job['input']['url'], headers=job['input'].get('headers', {}), timeout=30) as response:
                    response.raise_for_status()
                    with source.open('xb') as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            lease.require_active()
                            size += len(chunk)
                            if size > job['bytes']:
                                raise MediaError('MEDIA_INTEGRITY_FAILED', '영상 크기가 일치하지 않습니다.')
                            sha.update(chunk)
                            handle.write(chunk)
                if size != job['bytes'] or sha.hexdigest() != job['sha256']:
                    raise MediaError('MEDIA_INTEGRITY_FAILED', '영상 무결성 검증에 실패했습니다.')
            if lease.should_stop():
                result = {'state': 'stopped', 'progress': lease.progress}
            elif job['target'] in ('validate', 'import'):
                metadata = validate_media(source, validation_timeout=min(job['validation_timeout'], max(1, job['deadline'] - time.time())),
                    allow_portrait=True, should_stop=lease.should_stop)
                if metadata['duration'] > job['max_duration']:
                    raise MediaError('MEDIA_TOO_LONG', '허용된 영상 길이를 초과했습니다.')
                lease.require_active()
                if imported:
                    metadata['name'] = imported['name']
                result = {'state': 'completed', 'metadata': metadata, 'progress': 0}
            else:
                target = destination_url(destination) if destination else str(output)
                def progress(position):
                    lease.progress = max(lease.progress, position)
                # The deadline and watchdog kill the full FFmpeg process group even on parent failure.
                offset = 0 if job['target'] == 'local' else lease.progress
                rtmp_options = ({'rtmp_app': urlsplit(destination['server_url']).path.lstrip('/'),
                                 'rtmp_playpath': destination['stream_key'], 'rtmp_tcurl': destination['server_url']}
                                if destination else {})
                video_options = ({'video_bitrate_bps': 3_000_000, 'video_buffer_bits': 6_000_000}
                                 if job['target'] == 'twitch' else {})
                # Explicit playpath avoids FFmpeg's fixed-size URL-path parser
                # truncating long or query-bearing platform credentials.
                streamed = run_stream(stream_command(source, target, offset, **rtmp_options, **video_options,
                    keyframe_seconds=1 if job['target'] == 'chzzk' else 2, constant_bitrate=destination is not None),
                    expected_duration=job['duration'],
                    offset=offset, local_output=output if job['target'] == 'local' else None,
                    on_progress=progress, should_stop=lease.should_stop, deadline=job['deadline'],
                    max_runtime=max(1, job['deadline'] - time.time()),
                    redactions=(job.get('stream_key', ''), (destination or {}).get('stream_key', ''),
                                (destination or {}).get('server_url', '')),
                    max_output_bytes=output_budget)
                permanent_failure = streamed.error_code in ('OUTPUT_LIMIT_EXCEEDED', 'OUTPUT_BUDGET_INVALID', 'OUTPUT_BUDGET_MISSING')
                result = {'state': 'completed' if streamed.complete else 'stopped' if streamed.stopped else 'failed' if permanent_failure else 'retry_wait',
                          'progress': max(lease.progress, streamed.progress), 'error_code': streamed.error_code}
            if result['state'] == 'completed' and job['target'] in ('local', 'import'):
                if job['target'] == 'import':
                    output = source
                lease.require_active()
                output_size = output.stat().st_size
                if output_size > output_budget:
                    raise MediaError('OUTPUT_LIMIT_EXCEEDED', '출력 파일이 예약된 용량을 초과했습니다.')
                sha = hashlib.sha256()
                with output.open('rb') as handle:
                    for chunk in output_chunks(handle, output_size, output_budget, check_active=lease.require_active):
                        sha.update(chunk)
                checksum = sha.hexdigest()
                if imported and (imported['bytes'] != output_size or imported['sha256'] != checksum):
                    raise MediaError('MEDIA_INTEGRITY_FAILED', '가져온 영상의 무결성 검증에 실패했습니다.')
                signed = lease.call('/output', {'bytes': output_size, 'sha256': checksum},
                                    timeout=lease.active_timeout(10))
                lease.require_active()
                checked_url(signed['url'], development)
                upload_headers = httpx.Headers(signed.get('headers', {}))
                if 'Transfer-Encoding' in upload_headers or upload_headers.get('Content-Length', str(output_size)) != str(output_size):
                    raise MediaError('MEDIA_INTEGRITY_FAILED', '출력 업로드의 크기 정보가 일치하지 않습니다.')
                # Iterators have no length for HTTPX to infer. Send the
                # verified size explicitly so PUT has unambiguous framing.
                upload_headers['Content-Length'] = str(output_size)
                with output.open('rb') as handle:
                    # Check again before issuing PUT; a changed file must not
                    # start an upload, and the iterator never yields extra bytes.
                    if os.fstat(handle.fileno()).st_size > output_budget:
                        raise MediaError('OUTPUT_LIMIT_EXCEEDED', '출력 파일이 예약된 용량을 초과했습니다.')
                    if os.fstat(handle.fileno()).st_size != output_size:
                        raise MediaError('MEDIA_INTEGRITY_FAILED', '출력 파일의 크기가 변경되었습니다.')
                    response = lease.http.put(signed['url'], headers=upload_headers,
                        content=output_chunks(handle, output_size, output_budget, check_active=lease.require_active),
                        timeout=lease.active_timeout(60))
                    lease.require_active()
                    response.raise_for_status()
                result.update(output_bytes=output_size, output_sha256=checksum)
        except (MediaError, StreamTargetError, SourceImportError) as exc:
            result = {'state': 'failed', 'error_code': exc.code, 'progress': lease.progress}
        except Exception:
            # Never emit HTTP exceptions: they contain signed URLs or authorization details.
            result = {'state': 'failed', 'error_code': 'WORKER_IO_FAILED', 'progress': lease.progress}
        finally:
            try:
                if not lease.should_stop():
                    # Private Blob verifies the uploaded bytes before accepting
                    # file completion. Heartbeats continue during that bounded
                    # request, and the current lease/deadline still cap it.
                    verifying_output = result['state'] == 'completed' and job['target'] in ('local', 'import')
                    confirmed = lease.call('/finish', result, timeout=lease.active_timeout(90 if verifying_output else 10))
                    if confirmed.get('state'):
                        result['state'] = confirmed['state']
                        result['error_code'] = confirmed.get('error_code')
                    if result['state'] == 'completed':
                        # A late response is not a timely completion confirmation.
                        lease.require_active()
                else:
                    result = {'state': 'stopped', 'progress': lease.progress}
                    lease.call('/finish', result)
            except Exception:
                # Durable lease recovery will mark the job uncertain/failed; never invent success locally.
                result = {'state': 'failed', 'error_code': 'COMPLETION_UNCONFIRMED', 'progress': lease.progress}
            lease.close()
            job['stream_key'] = ''
            if isinstance(job.get('stream_destination'), dict):
                job['stream_destination'].clear()
            if isinstance(job.get('source'), dict):
                job['source'].clear()
            job['callback_token'] = ''
    logging.getLogger('replay.worker').info(json.dumps({'event': 'worker_exit', 'job_id': job['id'],
        'tenant_id': job['tenant_id'], 'version': job['version'], **{k: result.get(k) for k in ('state', 'error_code', 'progress')}}))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    args = parser.parse_args()
    config = Path(args.config)
    # Retried command starts in one sandbox cannot open two RTMP connections.
    with config.with_suffix('.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            job = json.loads(config.read_text())
        finally:
            config.unlink(missing_ok=True)
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        # Lower output verbosity of transports even if a parent enabled debug logging.
        logging.getLogger('httpx').setLevel(logging.WARNING)
        logging.getLogger('httpcore').setLevel(logging.WARNING)
        run_job(job)


if __name__ == '__main__':
    main()
