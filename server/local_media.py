"""Bounded transfers and Windows media execution for the desktop helper."""
import os
from .media_runtime import MediaError

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


def _windows_job(process):
    """Kill-on-close and 1 GiB memory ceiling, without administrator privileges."""
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                    ('min_working', c.c_size_t), ('max_working', c.c_size_t), ('processes', w.DWORD),
                    ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in ['read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes']]
    class Extended(c.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                    ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]; kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle: raise c.WinError(c.get_last_error())
    limits = Extended(); limits.basic.flags = 0x2000 | 0x100 | 0x8  # kill on close, process memory, process count
    limits.basic.processes = 1; limits.process_memory = 1024 ** 3
    try:
        if not kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)): raise c.WinError(c.get_last_error())
        if not kernel.AssignProcessToJobObject(handle, w.HANDLE(int(process._handle))): raise c.WinError(c.get_last_error())
    except Exception:
        kernel.CloseHandle(handle); raise
    return lambda: kernel.CloseHandle(handle)


def windows_execute(command, *, timeout, stall_timeout=None, on_progress=None, on_start=None,
                    should_stop=None, redactions=(), on_stdout=None, output_path=None, max_output_bytes=None):
    # Windows selectors cannot read anonymous pipes. Bounded reader threads feed
    # the same progress/checksum callbacks without buffering an entire output.
    from collections import deque
    from pathlib import Path
    import queue
    import subprocess
    import threading
    import time
    from .media_runtime import redact_diagnostic
    if should_stop and should_stop(): return -1, 0.0, False, None, '', True
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    close_job = None
    halt = threading.Event(); events = queue.Queue(maxsize=16)
    def read(pipe, kind):
        try:
            while not halt.is_set():
                chunk = os.read(pipe.fileno(), 8192)
                while not halt.is_set():
                    try: events.put((kind, chunk), timeout=.1); break
                    except queue.Full: pass
                if not chunk: break
        except (OSError, ValueError): pass
    readers = [threading.Thread(target=read, args=(pipe, kind), daemon=True) for pipe, kind in [(process.stdout, 'out'), (process.stderr, 'err')]]
    started = last = time.monotonic(); progress = 0.; ended = stopped = False; error = None
    diagnostics = deque(maxlen=8); buffer = b''; eof = 0
    try:
        close_job = _windows_job(process)
        if on_start: on_start(process)
        for reader in readers: reader.start()
        while eof < 2 or process.poll() is None:
            now = time.monotonic()
            if max_output_bytes is not None and output_path is not None and Path(output_path).exists() and Path(output_path).stat().st_size >= max_output_bytes:
                error = 'OUTPUT_LIMIT_EXCEEDED'; break
            if should_stop and should_stop(): stopped = True; break
            if now - started > timeout: error = 'STREAM_TIMEOUT'; break
            if stall_timeout and now - last > stall_timeout: error = 'STREAM_STALLED'; break
            try: kind, chunk = events.get(timeout=.1)
            except queue.Empty: continue
            if not chunk: eof += 1; continue
            if kind == 'err': diagnostics.append(chunk); continue
            if on_stdout: on_stdout(chunk)
            buffer = (buffer + chunk)[-65536:]
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                if line.startswith(b'out_time_us='):
                    try: position = max(0., int(line.split(b'=', 1)[1]) / 1_000_000)
                    except ValueError: continue
                    if position > progress:
                        progress = position; last = now
                        if on_progress: on_progress(progress)
                elif line.strip() == b'progress=end': ended = True
    finally:
        halt.set()
        if close_job: close_job()
        if process.poll() is None: process.kill()
        process.wait()
        for reader in readers:
            if reader.ident: reader.join(timeout=2)
        process.stdout.close(); process.stderr.close()
    diagnostic = redact_diagnostic(b''.join(diagnostics).decode('utf8', errors='replace'), redactions)
    return process.returncode, progress, ended, error, diagnostic, stopped
