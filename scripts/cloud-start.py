"""Start the existing invited-test API inside its persistent Vercel sandbox."""
import fcntl
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request


def ready():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8081/api/health', timeout=1) as response:
            return response.status == 401
    except urllib.error.HTTPError as error:
        return error.code == 401
    except (OSError, urllib.error.URLError):
        return False


def fail_previous_session_queue(data, deadline, backend_ready):
    """Never resume queued broadcasts from a different cloud VM session."""
    if backend_ready:
        return 0
    deadline_path = data / 'cloud-deadline'
    if not deadline_path.exists():
        return 0
    previous = float(deadline_path.read_text().strip())
    if not math.isfinite(previous):
        raise ValueError('Previous cloud session deadline is invalid')
    if abs(previous - deadline) <= 1:
        return 0
    message = '이전 클라우드 테스트 시간이 종료되어 대기 중인 방송을 취소했습니다. 방송을 다시 등록하세요.'
    count = 0
    for db_path in data.glob('*/replay.sqlite3'):
        if not re.fullmatch(r'[a-f0-9]{64}', db_path.parent.name) or db_path.is_symlink() or db_path.parent.is_symlink():
            continue
        db = sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=rw', uri=True, timeout=5)
        try:
            with db:
                jobs = db.execute("SELECT id FROM jobs WHERE state IN ('scheduled','retry_wait')").fetchall()
                now = time.time()
                for (job_id,) in jobs:
                    db.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?", (message, now, job_id))
                    db.execute('INSERT INTO events(job_id,at,message) VALUES (?,?,?)', (job_id, now, message))
                count += len(jobs)
        finally:
            db.close()
    return count


def main():
    root = Path(__file__).resolve().parent.parent
    host, expiry = sys.argv[1:]
    deadline = float(expiry)
    if not host or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in host):
        raise SystemExit('Invalid API hostname')
    if not math.isfinite(deadline) or deadline <= time.time():
        raise SystemExit('Sandbox session has expired')
    data = root / 'data-public'
    data.mkdir(mode=0o700, exist_ok=True)
    with (data / 'startup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        backend_ready = ready()
        # Keep the old deadline until every database is safe to restore. A failure
        # aborts startup, so retrying cannot accidentally skip unfinished cleanup.
        fail_previous_session_queue(data, deadline, backend_ready)
        deadline_path = data / 'cloud-deadline'
        temporary = deadline_path.with_suffix('.tmp')
        temporary.write_text(str(deadline))
        temporary.replace(deadline_path)
        if not backend_ready:
            env = dict(os.environ, REPLAY_PUBLIC_ORIGINS='https://replay-live-poc.vercel.app',
                       REPLAY_PUBLIC_HOSTS=f'localhost,127.0.0.1,{host}',
                       REPLAY_PUBLIC_DATA=str(data), REPLAY_CLOUD_DEADLINE_FILE=str(deadline_path))
            with (data / 'api.log').open('ab') as log:
                process = subprocess.Popen([str(root / '.venv/bin/python'), '-m', 'uvicorn', 'server.public_app:app',
                                            '--host', '0.0.0.0', '--port', '8081', '--no-access-log'],
                                           cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(60):
                if ready():
                    break
                if process.poll() is not None:
                    raise SystemExit('API startup failed')
                time.sleep(.25)
            else:
                raise SystemExit('API startup timed out')
    print('Replay API ready')


if __name__ == '__main__':
    main()
