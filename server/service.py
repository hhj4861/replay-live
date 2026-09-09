"""Single-host persistent scheduler and real FFmpeg streaming worker."""
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from cryptography.fernet import Fernet

TERMINAL = {'completed', 'stopped', 'failed'}
ACTIVE = {'starting', 'streaming', 'stopping'}


class Service:
    def __init__(self, root: Path, retry_delay=5):
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
        self.db_path = root / 'replay.sqlite3'
        self.guard = threading.RLock()
        self.processes = {}
        self.threads = []
        self.closing = threading.Event()
        self.retry_delay = retry_delay
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
            # A process cannot be adopted safely after an unclean restart.
            interrupted = db.execute("SELECT id FROM jobs WHERE state IN ('starting','streaming','stopping')").fetchall()
            for row in interrupted:
                db.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?", ('서버가 재시작되어 송출이 중단되었습니다. 새 방송을 생성하세요.', time.time(), row['id']))
                self.event(db, row['id'], '서버 재시작: 이전 송출을 실패 상태로 정리했습니다.')
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
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
        if not shutil.which('ffprobe') or not shutil.which('ffmpeg'):
            raise ValueError('FFmpeg와 ffprobe를 먼저 설치하세요.')
        try:
            result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                     '-show_format', '-show_streams', '-of', 'json', str(path)],
                                    capture_output=True, timeout=30, check=True)
            info = json.loads(result.stdout)
        except (subprocess.SubprocessError, ValueError):
            raise ValueError('읽을 수 없는 MP4 파일입니다.') from None
        videos = [s for s in info['streams'] if s['codec_type'] == 'video']
        audios = [s for s in info['streams'] if s['codec_type'] == 'audio']
        if 'mp4' not in info['format'].get('format_name', '').split(',') or len(videos) != 1 or len(audios) != 1:
            raise ValueError('H.264 영상 1개와 AAC 오디오 1개가 있는 MP4만 지원합니다.')
        v, a = videos[0], audios[0]
        duration = float(info['format'].get('duration', 0))
        numerator, denominator = v.get('avg_frame_rate', '0/1').split('/')
        fps = float(numerator) / max(float(denominator), 1)
        if v['codec_name'] != 'h264' or a['codec_name'] != 'aac' or v.get('pix_fmt') != 'yuv420p':
            raise ValueError('H.264(yuv420p) + AAC 형식으로 변환한 MP4를 사용하세요.')
        if not math.isfinite(duration) or not 1 <= duration <= 4 * 3600 or not 0 < fps <= 60 or v['width'] > 1920 or v['height'] > 1080 or v['width'] < v['height']:
            raise ValueError('1초~4시간, 최대 1920×1080, 60fps의 가로 영상을 사용하세요.')
        return dict(duration=duration, width=v['width'], height=v['height'], fps=fps)

    def add_media(self, path, name):
        info = self.probe(path)
        item = dict(id=uuid.uuid4().hex, name=Path(name).name[:180], path=str(path), bytes=path.stat().st_size, created=time.time(), **info)
        with self.db() as db:
            db.execute('INSERT INTO media VALUES (:id,:name,:path,:bytes,:duration,:width,:height,:fps,:created)', item)
        return {k: v for k, v in item.items() if k != 'path'}

    def list_media(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,name,bytes,duration,width,height,fps,created FROM media ORDER BY created DESC')]

    def create_job(self, media_id, title, target, stream_key='', scheduled=None):
        title = title.strip()
        if not 1 <= len(title) <= 120:
            raise ValueError('방송 이름은 1~120자로 입력하세요.')
        if target not in ('local', 'youtube'):
            raise ValueError('지원하지 않는 송출 대상입니다.')
        if target == 'youtube' and not re.fullmatch(r'[A-Za-z0-9_-]{10,160}', stream_key):
            raise ValueError('YouTube 스트림 키를 확인하세요.')
        now = time.time()
        scheduled = now if scheduled is None else scheduled
        if not isinstance(scheduled, (int, float)) or not math.isfinite(scheduled) or scheduled < now - 5 or scheduled > now + 30 * 86400:
            raise ValueError('예약 시간은 현재부터 30일 이내로 선택하세요.')
        with self.db() as db:
            if not db.execute('SELECT 1 FROM media WHERE id=?', (media_id,)).fetchone():
                raise ValueError('영상을 먼저 선택하세요.')
            job_id = uuid.uuid4().hex
            secret = self.cipher.encrypt(stream_key.encode()).decode() if target == 'youtube' else None
            db.execute('''INSERT INTO jobs(id,media_id,title,target,secret,scheduled,state,next_run,created,updated)
                          VALUES (?,?,?,?,?,?,?,?,?,?)''',
                       (job_id, media_id, title, target, secret, scheduled, 'scheduled', scheduled, now, now))
            self.event(db, job_id, '방송을 예약했습니다.' if scheduled > now + 1 else '송출 대기열에 등록했습니다.')
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self.db() as db:
            row = db.execute('''SELECT j.id,j.media_id,j.title,j.target,j.scheduled,j.state,j.progress,j.attempt,
                j.max_attempts,j.next_run,j.created,j.updated,j.error,m.name AS media_name,m.duration
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
        self.scheduler = threading.Thread(target=self.loop, daemon=True)
        self.scheduler.start()

    def loop(self):
        while not self.closing.wait(.25):
            self.tick()

    def tick(self):
        with self.guard:
            if self.closing.is_set() or self.processes:
                return
            with self.db() as db:
                if db.execute("SELECT 1 FROM jobs WHERE state IN ('starting','streaming','stopping')").fetchone():
                    return
                row = db.execute("SELECT * FROM jobs WHERE state IN ('scheduled','retry_wait') AND next_run<=? ORDER BY next_run,created LIMIT 1", (time.time(),)).fetchone()
                if not row:
                    return
                db.execute("UPDATE jobs SET state='starting',attempt=attempt+1,updated=? WHERE id=?", (time.time(), row['id']))
                self.event(db, row['id'], f"송출 시도 {row['attempt'] + 1}/{row['max_attempts']}")
            worker = threading.Thread(target=self.run, args=(row['id'],), daemon=True)
            self.threads.append(worker)
            worker.start()

    def command(self, media_path, target, offset=0):
        # Re-encode with predictable keyframes and AAC settings for YouTube ingest.
        return ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-nostats', '-progress', 'pipe:1',
                '-re', '-ss', str(offset), '-protocol_whitelist', 'file,pipe', '-i', media_path,
                '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'libx264', '-preset', 'veryfast',
                '-pix_fmt', 'yuv420p', '-r', '30', '-g', '60', '-keyint_min', '60', '-sc_threshold', '0',
                '-b:v', '6000k', '-maxrate', '6000k', '-bufsize', '12000k',
                '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
                '-rw_timeout', '15000000', *(['-tls_verify', '1'] if target.startswith('rtmps://') else []), '-f', 'flv', '-y', target]

    def run(self, job_id):
        process = None
        try:
            with self.guard:
                with self.db() as db:
                    row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
                    media = dict(db.execute('SELECT * FROM media WHERE id=?', (row['media_id'],)).fetchone())
                if row['state'] in TERMINAL or row['state'] == 'stopping' or self.closing.is_set():
                    return
                offset = row['progress']
                target = str(self.root / 'outputs' / f"{job_id}-{row['attempt']}.flv")
                if row['target'] == 'youtube':
                    target = 'rtmps://a.rtmps.youtube.com:443/live2/' + self.cipher.decrypt(row['secret'].encode()).decode()
                # Never retain stderr or command arguments in application logs: FFmpeg may echo the URL/key.
                process = subprocess.Popen(self.command(media['path'], target, offset),
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                self.processes[job_id] = process
            for line in process.stdout:
                if line.startswith('out_time_us='):
                    try:
                        position = min(media['duration'], offset + max(0, int(line.strip().split('=')[1])) / 1_000_000)
                    except ValueError:
                        continue
                    with self.db() as db:
                        current = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()['state']
                        if current in ('starting', 'streaming'):
                            if current == 'starting' and position > offset:
                                self.event(db, job_id, 'FFmpeg가 영상을 송출하고 있습니다. 플랫폼 공개 상태는 YouTube Studio에서 확인하세요.' if row['target'] == 'youtube' else '로컬 파일로 실시간 송출을 검증하고 있습니다.')
                            db.execute("UPDATE jobs SET progress=?,state=?,updated=? WHERE id=?", (position, 'streaming' if position > offset else current, time.time(), job_id))
            code = process.wait()
            with self.guard, self.db() as db:
                current = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if current['state'] == 'stopping' or self.closing.is_set():
                    state, error, message = 'stopped', None, '송출을 중지했습니다.'
                elif code == 0:
                    state, error, message = 'completed', None, '영상 끝까지 송출을 완료했습니다.'
                elif current['attempt'] < current['max_attempts']:
                    state, error, message = 'retry_wait', f'FFmpeg 종료 코드 {code}', '송출 실패: 마지막 진행 위치부터 재시도를 예약했습니다.'
                else:
                    state, error, message = 'failed', f'FFmpeg 종료 코드 {code}. 영상, 네트워크, 스트림 키를 확인하세요.', '최대 시도 횟수에 도달했습니다.'
                delay = self.retry_delay * 2 ** max(0, current['attempt'] - 1)
                db.execute('UPDATE jobs SET state=?,error=?,next_run=?,updated=?,progress=? WHERE id=?',
                           (state, error, time.time() + delay, time.time(), media['duration'] if state == 'completed' else current['progress'], job_id))
                self.event(db, job_id, message)
        except Exception:
            with self.db() as db:
                db.execute("UPDATE jobs SET state='failed',error=?,updated=? WHERE id=?", ('송출 프로세스를 실행할 수 없습니다. FFmpeg 설치와 파일을 확인하세요.', time.time(), job_id))
                self.event(db, job_id, '송출 프로세스 오류가 발생했습니다.')
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
            with self.guard:
                self.processes.pop(job_id, None)

    def stop(self, job_id):
        with self.guard, self.db() as db:
            row = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            if row['state'] in TERMINAL:
                return self.get_job(job_id)
            process = self.processes.get(job_id)
            state = 'stopping' if process and process.poll() is None else 'stopped'
            db.execute('UPDATE jobs SET state=?,updated=? WHERE id=?', (state, time.time(), job_id))
            self.event(db, job_id, '중지 요청을 받았습니다.' if state == 'stopping' else '예약을 취소했습니다.')
            if process and process.poll() is None:
                process.terminate()
                def force_stop():
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                threading.Thread(target=force_stop, daemon=True).start()
        return self.get_job(job_id)

    def close(self):
        self.closing.set()
        if hasattr(self, 'scheduler'):
            self.scheduler.join(timeout=2)
        for job_id in list(self.processes):
            self.stop(job_id)
        for worker in self.threads:
            worker.join(timeout=5)
        fcntl.flock(self.lock_file, fcntl.LOCK_UN)
        self.lock_file.close()
