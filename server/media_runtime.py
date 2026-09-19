"""Bounded, transport-independent FFmpeg execution for local and isolated workers."""
from collections import deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time

if __package__:
    from .output_policy import AUDIO_BITRATE_BPS, MAX_OUTPUT_BYTES, VIDEO_BITRATE_BPS, VIDEO_BUFFER_BITS
else:  # The resource-limit wrapper executes this file directly before execvp.
    from output_policy import AUDIO_BITRATE_BPS, MAX_OUTPUT_BYTES, VIDEO_BITRATE_BPS, VIDEO_BUFFER_BITS


class MediaError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'[{code}] {message}')


@dataclass(frozen=True)
class StreamResult:
    exit_code: int
    progress: float
    complete: bool
    error_code: str | None
    diagnostic: str
    stopped: bool = False


def redact_diagnostic(value, redactions=()):
    # Replace the whole URL (including its path/key), not just query parameters.
    value = re.sub(r'(?i)\b(?:rtmps?|https?)://[^\s\]\[<>\"\']+', '[REDACTED_URL]', value)
    for secret in redactions:
        if secret:
            value = value.replace(str(secret), '[REDACTED]')
    value = re.sub(r'(?i)(?:stream[_ -]?key|authorization|token)\s*[:=]\s*\S+', '[REDACTED_CREDENTIAL]', value)
    value = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', value)
    return value[-2048:]


def terminate_process_group(process):
    """Stop all descendants as well as the encoder; safe when already exited."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _bounded_command(command, timeout, max_output_bytes=None):
    # A separate exec wrapper applies limits without preexec_fn in a threaded API.
    return [sys.executable, str(Path(__file__).resolve()), '--bounded-exec',
            str(max(1, math.ceil(timeout))), str(os.getpid()),
            str(MAX_OUTPUT_BYTES if max_output_bytes is None else max_output_bytes), *map(str, command)]


def _execute(command, *, timeout, stall_timeout=None, on_progress=None, on_start=None,
             should_stop=None, redactions=(), on_stdout=None, output_path=None, max_output_bytes=None):
    if should_stop and should_stop():
        return -1, 0.0, False, None, '', True
    started = last_progress = time.monotonic()
    progress = 0.0
    ended = stopped = False
    error_code = None
    diagnostics = deque(maxlen=8)
    stdout_buffer = b''
    process = subprocess.Popen(_bounded_command(command, timeout, max_output_bytes), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True, stdin=subprocess.DEVNULL)
    try:
        if on_start:
            on_start(process)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if (max_output_bytes is not None and output_path is not None
                        and Path(output_path).exists() and Path(output_path).stat().st_size >= max_output_bytes):
                    error_code = 'OUTPUT_LIMIT_EXCEEDED'
                    terminate_process_group(process)
                elif should_stop and should_stop():
                    stopped = True
                    terminate_process_group(process)
                elif now - started > timeout:
                    error_code = 'STREAM_TIMEOUT'
                    terminate_process_group(process)
                elif stall_timeout and now - last_progress > stall_timeout:
                    error_code = 'STREAM_STALLED'
                    terminate_process_group(process)
                if stopped or error_code:
                    break
                for key, _ in selector.select(.2):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if key.data == 'stderr':
                        diagnostics.append(chunk)
                        continue
                    if on_stdout:
                        on_stdout(chunk)
                    stdout_buffer = (stdout_buffer + chunk)[-65536:]
                    while b'\n' in stdout_buffer:
                        line, stdout_buffer = stdout_buffer.split(b'\n', 1)
                        if line.startswith(b'out_time_us='):
                            try:
                                position = max(0.0, int(line.split(b'=', 1)[1]) / 1_000_000)
                            except ValueError:
                                continue
                            if position > progress:
                                last_progress = time.monotonic()
                                progress = position
                                if on_progress:
                                    on_progress(progress)
                        elif line.strip() == b'progress=end':
                            ended = True
            code = process.wait(timeout=3)
    finally:
        # The command can exit while leaving descendants behind.
        terminate_process_group(process)
        process.wait()
        process.stdout.close()
        process.stderr.close()
    diagnostic = redact_diagnostic(b''.join(diagnostics).decode('utf8', errors='replace'), redactions)
    return code, progress, ended, error_code, diagnostic, stopped


def _metadata(path, *, timeout=30, should_stop=None):
    chunks = []
    size = 0
    def collect(chunk):
        nonlocal size
        size += len(chunk)
        if size > 256 * 1024:
            raise MediaError('MEDIA_INVALID', '영상 정보가 허용 크기를 초과했습니다.')
        chunks.append(chunk)
    try:
        code, _, _, error, _, stopped = _execute(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                 '-show_entries', 'format=format_name,duration:stream=codec_type,codec_name,pix_fmt,width,height,avg_frame_rate',
                                 '-of', 'json', str(path)], timeout=timeout, on_stdout=collect, should_stop=should_stop)
        if stopped:
            raise MediaError('MEDIA_VALIDATION_CANCELLED', '영상 검사가 취소되었습니다.')
        if code != 0 or error:
            raise ValueError()
        return json.loads(b''.join(chunks))
    except MediaError:
        raise
    except (subprocess.SubprocessError, ValueError, OSError):
        raise MediaError('MEDIA_INVALID', '읽을 수 없는 영상 파일입니다.') from None


def validate_media(path, *, validation_timeout=300, allow_portrait=False, should_stop=None):
    if not shutil.which('ffprobe') or not shutil.which('ffmpeg'):
        raise MediaError('MEDIA_INVALID', 'FFmpeg와 ffprobe를 먼저 설치하세요.')
    validation_deadline = time.monotonic() + validation_timeout
    info = _metadata(path, timeout=min(30, validation_timeout), should_stop=should_stop)
    try:
        videos = [s for s in info['streams'] if s['codec_type'] == 'video']
        audios = [s for s in info['streams'] if s['codec_type'] == 'audio']
        if 'mp4' not in info['format'].get('format_name', '').split(',') or len(videos) != 1 or len(audios) != 1:
            raise ValueError()
        video, audio = videos[0], audios[0]
        duration = float(info['format']['duration'])
        numerator, denominator = video.get('avg_frame_rate', '0/1').split('/')
        fps = float(numerator) / float(denominator)
        width, height = int(video['width']), int(video['height'])
        if video['codec_name'] != 'h264' or audio['codec_name'] != 'aac' or video.get('pix_fmt') != 'yuv420p':
            raise ValueError()
        dimensions_valid = (0 < min(width, height) <= 1080 and max(width, height) <= 1920
                            and width % 2 == 0 and height % 2 == 0)
        if not math.isfinite(duration) or not 1 <= duration <= 14400 or not 0 < fps <= 60 or not dimensions_valid or (not allow_portrait and width < height):
            raise ValueError()
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        orientation = '가로·세로' if allow_portrait else '가로'
        raise MediaError('MEDIA_INVALID', f'1초~4시간, 최대 {orientation} 1080p/60fps, H.264(yuv420p)+AAC MP4를 사용하세요.') from None
    command = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-xerror', '-err_detect', 'explode',
               '-threads', '2', '-protocol_whitelist', 'file,pipe', '-i', str(path), '-map', '0:v:0', '-map', '0:a:0',
               '-progress', 'pipe:1', '-nostats', '-f', 'null', '-']
    remaining = validation_deadline - time.monotonic()
    if remaining <= 0:
        raise MediaError('MEDIA_VALIDATION_TIMEOUT', '영상 전체 검사 시간이 초과되었습니다. 파일을 다시 인코딩하세요.')
    code, progress, ended, error, _, stopped = _execute(command, timeout=remaining, should_stop=should_stop)
    if stopped:
        raise MediaError('MEDIA_VALIDATION_CANCELLED', '영상 검사가 취소되었습니다.')
    if error:
        raise MediaError('MEDIA_VALIDATION_TIMEOUT', '영상 전체 검사 시간이 초과되었습니다. 파일을 다시 인코딩하세요.')
    if code != 0 or not ended or progress < duration - .25:
        raise MediaError('MEDIA_DECODE_FAILED', '영상 일부가 손상되었거나 끝까지 읽을 수 없습니다. 완전한 파일을 다시 업로드하세요.')
    return dict(duration=duration, width=width, height=height, fps=fps)


def stream_command(media_path, target, offset=0, *, rtmp_app=None, rtmp_playpath=None, rtmp_tcurl=None,
                   keyframe_seconds=2, constant_bitrate=False,
                   video_bitrate_bps=VIDEO_BITRATE_BPS, video_buffer_bits=VIDEO_BUFFER_BITS):
    if isinstance(keyframe_seconds, bool) or keyframe_seconds not in (1, 2):
        raise MediaError('STREAM_PROFILE_INVALID', '키프레임 간격은 1초 또는 2초여야 합니다.')
    if any(isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum
           for value, maximum in ((video_bitrate_bps, VIDEO_BITRATE_BPS), (video_buffer_bits, VIDEO_BUFFER_BITS))):
        raise MediaError('STREAM_PROFILE_INVALID', '영상 비트레이트와 버퍼 크기가 허용 범위를 벗어났습니다.')
    gop = str(int(30 * keyframe_seconds))
    return ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-xerror', '-nostats', '-progress', 'pipe:1',
            '-re', '-ss', str(offset), '-threads', '2', '-protocol_whitelist', 'file,pipe', '-i', str(media_path),
            '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast',
            '-pix_fmt', 'yuv420p', '-r', '30', '-g', gop, '-keyint_min', gop, '-sc_threshold', '0',
            '-b:v', str(video_bitrate_bps), '-maxrate', str(video_bitrate_bps), '-bufsize', str(video_buffer_bits),
            *(['-minrate', str(video_bitrate_bps), '-nal-hrd', 'cbr'] if constant_bitrate else []),
            '-c:a', 'aac', '-b:a', str(AUDIO_BITRATE_BPS), '-ar', '44100', '-ac', '2',
            '-rw_timeout', '15000000', *(['-tls_verify', '1'] if str(target).startswith('rtmps://') else []),
            *(['-rtmp_app', rtmp_app] if rtmp_app is not None else []),
            *(['-rtmp_playpath', rtmp_playpath] if rtmp_playpath is not None else []),
            *(['-rtmp_tcurl', rtmp_tcurl] if rtmp_tcurl is not None else []),
            '-f', 'flv', '-y', str(target)]


def require_verified_rtmps():
    """Commercial RTMPS only uses the audited GnuTLS certificate/hostname path.

    FFmpeg's TLS documentation notes hostname-validation differences between
    backends. Never silently accept a chain-only backend or disable validation.
    """
    try:
        configuration = subprocess.check_output(['ffmpeg', '-hide_banner', '-buildconf'],
            stderr=subprocess.STDOUT, timeout=5, text=True)
        flags = set(configuration.split())
        if (len(configuration) > 65536 or '--enable-gnutls' not in flags
                or flags & {'--enable-openssl', '--enable-librtmp'}):
            raise ValueError()
    except (OSError, subprocess.SubprocessError, ValueError):
        raise MediaError('STREAM_TLS_UNVERIFIED', '현재 송출 엔진의 RTMPS 인증서 검증을 확인할 수 없습니다.') from None
    return 'gnutls'


def run_stream(command, *, expected_duration, offset=0, local_output=None, on_progress=None,
               on_start=None, should_stop=None, deadline=None, stall_timeout=30,
               max_runtime=None, redactions=(), max_output_bytes=None):
    if (not isinstance(expected_duration, (int, float)) or not math.isfinite(expected_duration)
            or not 0 < expected_duration <= 14400 or not isinstance(offset, (int, float))
            or not math.isfinite(offset) or not 0 <= offset < expected_duration):
        return StreamResult(-1, 0, False, 'MEDIA_INVALID', '')
    if max_output_bytes is not None:
        if (isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int)
                or not 0 < max_output_bytes <= MAX_OUTPUT_BYTES or local_output is None):
            return StreamResult(-1, offset, False, 'OUTPUT_BUDGET_INVALID', '')
        # -fs may terminate FFmpeg successfully at a truncated packet boundary.
        # RLIMIT_FSIZE is the hard ceiling; observed size independently marks any
        # encoder reaching that ceiling as failed, even when FFmpeg exits zero.
        command = list(command)
        if command and Path(str(command[0])).name == 'ffmpeg':
            command[-1:-1] = ['-fs', str(max_output_bytes)]
    remaining = max(0.0, expected_duration - offset)
    timeout = min(max_runtime, remaining + 45) if max_runtime is not None else remaining + 45
    if not math.isfinite(timeout) or timeout <= 0:
        return StreamResult(-1, offset, False, 'STREAM_TIMEOUT', '')
    deadline_limited = False
    if deadline is not None:
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= time.time():
            return StreamResult(-1, offset, False, 'DEADLINE_EXCEEDED', '')
        if deadline - time.time() < timeout:
            timeout = deadline - time.time()
            deadline_limited = True
    try:
        code, position, ended, error, diagnostic, stopped = _execute(command, timeout=timeout,
            stall_timeout=stall_timeout, on_progress=(lambda p: on_progress(min(expected_duration, offset + p))) if on_progress else None,
            on_start=on_start, should_stop=should_stop, redactions=redactions,
            output_path=local_output, max_output_bytes=max_output_bytes)
    except OSError:
        return StreamResult(-1, offset, False, 'PROCESS_START_FAILED', '')
    progress = min(expected_duration, offset + position)
    if max_output_bytes is not None and local_output is not None:
        try:
            if Path(local_output).stat().st_size >= max_output_bytes or code == -signal.SIGXFSZ:
                error = 'OUTPUT_LIMIT_EXCEEDED'
        except FileNotFoundError:
            pass
    if stopped:
        return StreamResult(code, progress, False, None, diagnostic, True)
    if error == 'STREAM_TIMEOUT' and deadline_limited:
        error = 'DEADLINE_EXCEEDED'
    if not error and code != 0:
        error = 'STREAM_EXIT'
    if not error and (not ended or position < remaining - .25):
        error = 'STREAM_INCOMPLETE'
    if not error and local_output is not None:
        try:
            output = _metadata(local_output)
            output_duration = float(output['format']['duration'])
            if not math.isfinite(output_duration) or output_duration < remaining - .25 or {s['codec_name'] for s in output['streams']} != {'h264', 'aac'}:
                error = 'STREAM_INCOMPLETE'
            else:
                # FLV timestamps include the final packet duration while FFmpeg's
                # progress clock can stop at the final packet's start timestamp.
                progress = min(expected_duration, offset + max(position, output_duration))
        except (MediaError, KeyError, TypeError, ValueError):
            error = 'STREAM_INCOMPLETE'
    return StreamResult(code, progress, error is None, error, diagnostic)


def _exec_with_limits():
    import resource
    seconds = int(sys.argv[2])
    if sys.platform.startswith('linux'):
        import ctypes
        # FFmpeg must die if its lease/watchdog process is SIGKILLed. Arm before
        # exec and check the original parent to close the fork-to-prctl race.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != int(sys.argv[3]):
            os._exit(1)
    resource.setrlimit(resource.RLIMIT_CPU, (seconds * 2 + 5, seconds * 2 + 5))
    file_limit = int(sys.argv[4])
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    os.execvp(sys.argv[5], sys.argv[5:])


if __name__ == '__main__' and len(sys.argv) > 5 and sys.argv[1] == '--bounded-exec':
    _exec_with_limits()
