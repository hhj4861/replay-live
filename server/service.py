"""Single-host persistent scheduler and real FFmpeg streaming worker."""
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from cryptography.fernet import Fernet
from .media_runtime import validate_media, stream_command, run_stream

TERMINAL = {'completed', 'stopped', 'failed'}
ACTIVE = {'starting', 'streaming', 'stopping'}


class Service:
    def __init__(self, root: Path, retry_delay=5, capacity=None, *, sqlite_timeout=1,
                 poll_interval=.25, stall_timeout=30, validation_timeout=300):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = (root / 'worker.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            raise RuntimeError('이 데이터 폴더를 사용하는 서버가 이미 실행 중입니다.')
        for name in ('media', 'outputs'):
            (root / name).mkdir(exist_ok=True, mode=0o700)
        key_path = root / 'secret.key'
        if not key_path.exists():
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self._dedupe_key = key_path.read_bytes()
        self.db_path = root / 'replay.sqlite3'
        self.guard = threading.RLock()
        self.processes = {}
        self.threads = []
        self.closing = threading.Event()
        self.retry_delay = retry_delay
        self.capacity = capacity
        self.sqlite_timeout = sqlite_timeout
        self.poll_interval = poll_interval
        self.stall_timeout = stall_timeout
        self.validation_timeout = validation_timeout
        self.heartbeat_at = time.time()
        self.last_error = None
        self.pending = {}
        self.progress_pending = {}
        self.stop_requests = set()
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
                    bytes INTEGER NOT NULL, duration REAL NOT NULL, width INTEGER NOT NULL,
                    height INTEGER NOT NULL, fps REAL NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, media_id TEXT NOT NULL REFERENCES media(id), title TEXT NOT NULL,
                    target TEXT NOT NULL, secret TEXT, scheduled REAL NOT NULL, state TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3, next_run REAL NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    at REAL NOT NULL, message TEXT NOT NULL);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(jobs)')}
            for name, sql_type in [('idempotency_key', 'TEXT'), ('request_fingerprint', 'TEXT'), ('deadline', 'REAL'), ('error_code', 'TEXT')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {sql_type}')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS jobs_idempotency ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state,next_run,created)')
            # A process cannot be adopted safely after an unclean restart.
            interrupted = db.execute("SELECT id FROM jobs WHERE state IN ('starting','streaming','stopping')").fetchall()
            for row in interrupted:
                db.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?", ('서버가 재시작되어 송출이 중단되었습니다. 새 방송을 생성하세요.', time.time(), row['id']))
                self.event(db, row['id'], '서버 재시작: 이전 송출을 실패 상태로 정리했습니다.')
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=self.sqlite_timeout)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def event(db, job_id, message):
        db.execute('INSERT INTO events(job_id,at,message) VALUES (?,?,?)', (job_id, time.time(), message))

    def probe(self, path):
        return validate_media(path, validation_timeout=self.validation_timeout)

    def add_media(self, path, name):
        info = self.probe(path)
        item = dict(id=uuid.uuid4().hex, name=Path(name).name[:180], path=str(path), bytes=path.stat().st_size, created=time.time(), **info)
        with self.db() as db:
            db.execute('INSERT INTO media VALUES (:id,:name,:path,:bytes,:duration,:width,:height,:fps,:created)', item)
        return {k: v for k, v in item.items() if k != 'path'}

    def list_media(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,name,bytes,duration,width,height,fps,created FROM media ORDER BY created DESC')]

    def create_job(self, media_id, title, target, stream_key='', scheduled=None, idempotency_key=None, deadline=None):
        title = title.strip()
        if not 1 <= len(title) <= 120:
            raise ValueError('방송 이름은 1~120자로 입력하세요.')
        if target not in ('local', 'youtube'):
            raise ValueError('지원하지 않는 송출 대상입니다.')
        if target == 'youtube' and not re.fullmatch(r'[A-Za-z0-9_-]{10,160}', stream_key):
            raise ValueError('YouTube 스트림 키를 확인하세요.')
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', idempotency_key)):
            raise ValueError('[IDEMPOTENCY_INVALID] 중복 방지 키 형식이 올바르지 않습니다.')
        fingerprint = hmac.new(self._dedupe_key, json.dumps([media_id, title, target, stream_key, scheduled, deadline], separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
        now = time.time()
        scheduled = now if scheduled is None else scheduled
        with self.guard, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id,request_fingerprint FROM jobs WHERE idempotency_key=?', (idempotency_key,)).fetchone() if idempotency_key else None
            if existing:
                if not hmac.compare_digest(existing['request_fingerprint'], fingerprint):
                    raise ValueError('[IDEMPOTENCY_CONFLICT] 같은 중복 방지 키로 다른 방송을 등록할 수 없습니다.')
                return self.get_job(existing['id'])
            if not isinstance(scheduled, (int, float)) or not math.isfinite(scheduled) or scheduled < now - 5 or scheduled > now + 30 * 86400:
                raise ValueError('예약 시간은 현재부터 30일 이내로 선택하세요.')
            media = db.execute('SELECT duration FROM media WHERE id=?', (media_id,)).fetchone()
            if not media:
                raise ValueError('영상을 먼저 선택하세요.')
            if deadline is not None:
                if not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= now:
                    raise ValueError('[DEADLINE_EXCEEDED] 송출 가능한 시간이 종료되었습니다.')
                queued = db.execute("SELECT j.next_run,j.progress,j.attempt,j.max_attempts,j.state,m.duration FROM jobs j JOIN media m ON j.media_id=m.id WHERE j.state IN ('scheduled','retry_wait','starting','streaming','stopping') ORDER BY j.next_run,j.created").fetchall()
                finish = now
                for item in queued:
                    attempts = max(1, item['max_attempts'] - item['attempt'] + int(item['state'] in ACTIVE))
                    retry_budget = sum(self.retry_delay * 2 ** attempt for attempt in range(item['attempt'], item['max_attempts'] - 1))
                    finish = max(finish, item['next_run']) + (max(0, item['duration'] - item['progress']) + 45) * attempts + retry_budget + 5
                finish = max(finish, scheduled) + (media['duration'] + 45) * 3 + self.retry_delay * 3 + 5
                if finish > deadline:
                    raise ValueError('[DEADLINE_CAPACITY] 대기 중인 방송과 재시도 시간을 고려하면 종료 전에 완료할 수 없습니다.')
            job_id = uuid.uuid4().hex
            secret = self.cipher.encrypt(stream_key.encode()).decode() if target == 'youtube' else None
            db.execute('''INSERT INTO jobs(id,media_id,title,target,secret,scheduled,state,next_run,created,updated,idempotency_key,request_fingerprint,deadline)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (job_id, media_id, title, target, secret, scheduled, 'scheduled', scheduled, now, now, idempotency_key, fingerprint, deadline))
            self.event(db, job_id, '방송을 예약했습니다.' if scheduled > now + 1 else '송출 대기열에 등록했습니다.')
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self.db() as db:
            row = db.execute('''SELECT j.id,j.media_id,j.title,j.target,j.scheduled,j.state,j.progress,j.attempt,
                j.max_attempts,j.next_run,j.created,j.updated,j.error,j.error_code,j.deadline,m.name AS media_name,m.duration
                FROM jobs j JOIN media m ON m.id=j.media_id WHERE j.id=?''', (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return dict(row)

    def list_jobs(self):
        with self.db() as db:
            ids = [r['id'] for r in db.execute('SELECT id FROM jobs ORDER BY created DESC LIMIT 100')]
        return [self.get_job(i) for i in ids]

    def events(self, job_id):
        self.get_job(job_id)
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT at,message FROM events WHERE job_id=? ORDER BY id DESC LIMIT 100', (job_id,))]

    def start(self):
        if hasattr(self, 'scheduler') and self.scheduler.is_alive():
            return
        self.heartbeat_at = time.time()
        self.scheduler = threading.Thread(target=self.loop, daemon=True)
        self.scheduler.start()

    def readiness(self):
        alive = hasattr(self, 'scheduler') and self.scheduler.is_alive()
        database = True
        try:
            with self.db() as db:
                db.execute('SELECT 1 FROM jobs LIMIT 1').fetchone()
        except sqlite3.Error:
            database = False
        fresh = time.time() - self.heartbeat_at < max(5, self.sqlite_timeout * 2 + 1)
        healthy = alive and database and fresh and self.last_error is None and not self.closing.is_set()
        return dict(ready=healthy, status='ok' if healthy else 'degraded', scheduler_alive=alive,
                    heartbeat_at=self.heartbeat_at, last_error=self.last_error, database=database)

    def loop(self):
        while not self.closing.wait(self.poll_interval):
            try:
                self.tick()
                self.last_error = None
            except sqlite3.Error:
                self.last_error = 'DB_UNAVAILABLE'
            except Exception:
                self.last_error = 'SCHEDULER_ERROR'
            finally:
                self.heartbeat_at = time.time()

    def _flush_pending(self):
        if not self.pending and not self.progress_pending:
            return
        with self.db() as db:
            for job_id, position in self.progress_pending.items():
                row = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row and row['state'] in ('starting', 'streaming'):
                    if row['state'] == 'starting' and position > 0:
                        self.event(db, job_id, 'FFmpeg 송출이 진행 중입니다. YouTube 공개 상태는 Studio에서 확인하세요.')
                    db.execute("UPDATE jobs SET progress=MAX(progress,?),state='streaming',updated=? WHERE id=?", (position, time.time(), job_id))
            for job_id, result in self.pending.items():
                current = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
                state, error_code, error, position, next_run, message = result
                if current and (current['state'] in ('stopping', 'stopped') or job_id in self.stop_requests):
                    state, error_code, error, message = 'stopped', None, None, '송출을 중지했습니다.'
                db.execute('UPDATE jobs SET state=?,error_code=?,error=?,progress=MAX(progress,?),next_run=?,updated=? WHERE id=?',
                           (state, error_code, error, position, next_run, time.time(), job_id))
                self.event(db, job_id, message)
        for job_id in self.pending:
            self.stop_requests.discard(job_id)
        self.pending.clear()
        self.progress_pending.clear()

    def tick(self):
        with self.guard:
            if self.closing.is_set():
                return
            self._flush_pending()
            self.threads = [worker for worker in self.threads if worker.is_alive()]
            if self.processes:
                return
            acquired = handed_off = False
            claimed = None
            try:
                with self.db() as db:
                    # Claim the writer transaction before reserving shared capacity.
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute("SELECT 1 FROM jobs WHERE state IN ('starting','streaming','stopping')").fetchone():
                        return
                    row = db.execute("SELECT j.*,m.duration FROM jobs j JOIN media m ON j.media_id=m.id WHERE j.state IN ('scheduled','retry_wait') AND j.next_run<=? ORDER BY j.next_run,j.created LIMIT 1", (time.time(),)).fetchone()
                    if not row:
                        return
                    if row['id'] in self.stop_requests:
                        db.execute("UPDATE jobs SET state='stopped',updated=? WHERE id=?", (time.time(), row['id']))
                        self.event(db, row['id'], '예약을 취소했습니다.')
                        # Discard only after this writer transaction commits.
                        self.pending[row['id']] = ('stopped', None, None, row['progress'], row['next_run'], '송출을 중지했습니다.')
                        return
                    if row['deadline'] is not None and time.time() + max(0, row['duration'] - row['progress']) + 5 > row['deadline']:
                        db.execute("UPDATE jobs SET state='failed',error_code='DEADLINE_EXCEEDED',error=?,updated=? WHERE id=?", ('송출 가능 시간이 부족합니다. 방송을 다시 등록하세요.', time.time(), row['id']))
                        self.event(db, row['id'], '실행 기한이 지나 송출을 시작하지 않았습니다.')
                        return
                    if self.capacity is not None:
                        acquired = self.capacity.acquire(blocking=False)
                        if not acquired:
                            return
                    db.execute("UPDATE jobs SET state='starting',attempt=attempt+1,error_code=NULL,error=NULL,updated=? WHERE id=?", (time.time(), row['id']))
                    self.event(db, row['id'], f"송출 시도 {row['attempt'] + 1}/{row['max_attempts']}")
                    claimed = row['id']
                worker = threading.Thread(target=self.run, args=(claimed, acquired), daemon=True)
                worker.start()
                handed_off = True
                self.threads.append(worker)
            except Exception:
                if claimed is not None:
                    self.pending[claimed] = ('failed', 'PROCESS_START_FAILED', '송출 작업자를 시작하지 못했습니다.', 0, time.time(), '작업자 시작 실패')
                raise
            finally:
                if acquired and not handed_off:
                    self.capacity.release()

    def command(self, media_path, target, offset=0):
        return stream_command(media_path, target, offset)

    def run(self, job_id, owns_capacity=False):
        result = None
        row = None
        try:
            with self.guard, self.db() as db:
                row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
                media = dict(db.execute('SELECT * FROM media WHERE id=?', (row['media_id'],)).fetchone())
                if row['state'] in TERMINAL or row['state'] == 'stopping' or self.closing.is_set():
                    return
                target = str(self.root / 'outputs' / f"{job_id}-{row['attempt']}.flv")
                stream_key = ''
                if row['target'] == 'youtube':
                    stream_key = self.cipher.decrypt(row['secret'].encode()).decode()
                    target = 'rtmps://a.rtmps.youtube.com:443/live2/' + stream_key

            def started(process):
                with self.guard:
                    self.processes[job_id] = process

            def progress(position):
                with self.guard:
                    self.progress_pending[job_id] = position
                    try:
                        self._flush_pending()
                    except sqlite3.Error:
                        self.last_error = 'DB_UNAVAILABLE'

            result = run_stream(self.command(media['path'], target, row['progress']), expected_duration=media['duration'],
                                offset=row['progress'], local_output=target if row['target'] == 'local' else None,
                                on_start=started, on_progress=progress,
                                should_stop=lambda: self.closing.is_set() or job_id in self.stop_requests,
                                deadline=row['deadline'], stall_timeout=self.stall_timeout, redactions=(stream_key,))
            delay = self.retry_delay * 2 ** max(0, row['attempt'] - 1)
            next_run = time.time() + delay
            if result.stopped or job_id in self.stop_requests or self.closing.is_set():
                transition = ('stopped', None, None, result.progress, next_run, '송출을 중지했습니다.')
            elif result.complete:
                # Preserve measured progress. Never manufacture progress from metadata.
                transition = ('completed', None, None, result.progress, next_run, '영상 끝까지 송출을 완료했습니다.')
            else:
                can_retry = row['attempt'] < row['max_attempts'] and result.error_code not in ('STREAM_INCOMPLETE', 'DEADLINE_EXCEEDED')
                if row['deadline'] is not None and next_run + max(0, media['duration'] - result.progress) + 5 > row['deadline']:
                    can_retry = False
                state = 'retry_wait' if can_retry else 'failed'
                message = '송출 실패: 마지막 진행 위치부터 재시도를 예약했습니다.' if can_retry else '송출을 완료하지 못했습니다.'
                error = f"[{result.error_code}] {message}"
                if result.diagnostic:
                    error += '\n' + result.diagnostic
                transition = (state, result.error_code, error, result.progress, next_run, message)
            with self.guard:
                self.pending[job_id] = transition
        except Exception as error:
            code = 'DB_UNAVAILABLE' if isinstance(error, sqlite3.Error) else 'PROCESS_START_FAILED'
            with self.guard:
                self.pending[job_id] = ('failed', code, f'[{code}] 송출 작업을 완료하지 못했습니다.', row['progress'] if row else 0, time.time(), '송출 작업 오류가 발생했습니다.')
        finally:
            with self.guard:
                self.processes.pop(job_id, None)
                try:
                    self._flush_pending()
                except sqlite3.Error:
                    self.last_error = 'DB_UNAVAILABLE'
                finally:
                    if job_id not in self.pending:
                        self.stop_requests.discard(job_id)
                    if owns_capacity:
                        self.capacity.release()

    def stop(self, job_id):
        with self.guard:
            # Retain cancellation in memory even if a transient DB lock occurs.
            self.stop_requests.add(job_id)
            with self.db() as db:
                row = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
                if not row:
                    self.stop_requests.discard(job_id)
                    raise KeyError(job_id)
                if row['state'] in TERMINAL:
                    self.stop_requests.discard(job_id)
                    return self.get_job(job_id)
                process = self.processes.get(job_id)
                state = 'stopping' if process and process.poll() is None else 'stopped'
                db.execute('UPDATE jobs SET state=?,updated=? WHERE id=?', (state, time.time(), job_id))
                self.event(db, job_id, '중지 요청을 받았습니다.' if state == 'stopping' else '예약을 취소했습니다.')
                if state == 'stopped' and row['state'] in ('scheduled', 'retry_wait'):
                    self.stop_requests.discard(job_id)
        return self.get_job(job_id)

    def close(self):
        self.closing.set()
        if hasattr(self, 'scheduler'):
            self.scheduler.join(timeout=self.sqlite_timeout + 2)
        for worker in list(self.threads):
            worker.join(timeout=self.sqlite_timeout + 5)
        with self.guard:
            try:
                self._flush_pending()
            except sqlite3.Error:
                pass  # Active DB states are failed explicitly on the next startup.
        fcntl.flock(self.lock_file, fcntl.LOCK_UN)
        self.lock_file.close()
