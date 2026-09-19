"""Short-lived invited test sessions, isolated data, and a shared FFmpeg slot."""
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import secrets
import re
import sqlite3
import threading
import time
from fastapi import HTTPException
from .service import Service
from .request_boundary import ScopedRateLimiter

PUBLIC_UPLOAD = 50 * 1024 * 1024
PUBLIC_DURATION = 120
MAX_ACTIVE_SESSIONS = 32
SHARED_SAMPLE_ID = 'shared-sample'


class TestService(Service):
    def register_shared_sample(self, path):
        """Reference the operator-provided sample without copying private uploads."""
        with self.guard, self.db() as db:
            if db.execute('SELECT 1 FROM media WHERE id=?', (SHARED_SAMPLE_ID,)).fetchone():
                return
            info = self.probe(path)
            item = dict(id=SHARED_SAMPLE_ID, name='sample.mp4', path=str(path),
                        bytes=path.stat().st_size, created=0, **info)
            db.execute('INSERT INTO media VALUES (:id,:name,:path,:bytes,:duration,:width,:height,:fps,:created)', item)

    def probe(self, path):
        info = super().probe(path)
        if info['duration'] > PUBLIC_DURATION:
            raise ValueError('외부 테스트 영상은 최대 2분까지 지원합니다.')
        return info

    def add_media(self, path, name):
        with self.guard:
            if sum(item['id'] != SHARED_SAMPLE_ID for item in self.list_media()) >= 3:
                raise ValueError('테스트 세션당 영상은 최대 3개까지 업로드할 수 있습니다.')
            return super().add_media(path, name)

    def create_job(self, *args, **kwargs):
        with self.guard:
            if len(self.list_jobs()) >= 5:
                raise ValueError('테스트 세션당 방송은 최대 5개까지 등록할 수 있습니다.')
            return super().create_job(*args, **kwargs)


class PublicAccess:
    def __init__(self, root: Path, invite_code=None, cleanup_interval=30.0):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        code_path = self.root / 'invite-code.txt'
        if invite_code is None:
            if not code_path.exists():
                fd = os.open(code_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as out:
                    out.write(secrets.token_urlsafe(24) + '\n')
            invite_code = code_path.read_text().strip()
        self.invite_code = invite_code
        self.guard = threading.RLock()
        self.capacity = threading.BoundedSemaphore(1)
        self.uploads = threading.BoundedSemaphore(2)
        self.services = {}
        self.failed_logins = ScopedRateLimiter(limit=20, window=60, max_clients=4096)
        self.closing = threading.Event()
        self.cleanup_interval = max(0.1, float(cleanup_interval))
        self.session_path = self.root / 'sessions.json'
        self.sessions = json.loads(self.session_path.read_text()) if self.session_path.exists() else {}
        if not isinstance(self.sessions, dict) or any(not re.fullmatch(r'[a-f0-9]{64}', digest)
                or isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry)
                for digest, expiry in self.sessions.items()):
            raise ValueError('테스트 세션 저장소 형식이 올바르지 않습니다.')
        # Preserve pending work, but do not restore an idle scheduler for every
        # expired historical session. User files and records are retained.
        try:
            for digest, expiry in self.sessions.items():
                if expiry > time.time() or self._has_pending_work(digest):
                    self._service(digest)
        except Exception:
            for service in self.services.values():
                service.close()
            raise
        self.reaper = threading.Thread(target=self._cleanup_loop, name='replay-session-cleanup', daemon=True)
        self.reaper.start()

    def _has_pending_work(self, digest):
        path = self.root / digest / 'replay.sqlite3'
        if not path.is_file():
            return False
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            return db.execute("SELECT 1 FROM jobs WHERE state NOT IN ('completed','stopped','failed') LIMIT 1").fetchone() is not None
        finally:
            db.close()

    def cleanup_expired(self):
        stopped = 0
        with self.guard:
            for digest, service in list(self.services.items()):
                if self.sessions.get(digest, 0) > time.time():
                    continue
                with service.guard:
                    if service.processes or any(job['state'] not in ('completed', 'stopped', 'failed') for job in service.list_jobs()):
                        continue
                    service.closing.set()
                service.close()
                del self.services[digest]
                stopped += 1
        return stopped

    def _cleanup_loop(self):
        while not self.closing.wait(self.cleanup_interval):
            try:
                self.cleanup_expired()
            except Exception:
                logging.getLogger('replay.sessions').error('Expired session cleanup failed; retained data for retry')

    def _service(self, digest):
        if digest not in self.services:
            service = TestService(self.root / digest, capacity=self.capacity)
            sample = self.root / 'shared' / 'sample.mp4'
            if sample.is_file():
                try:
                    service.register_shared_sample(sample)
                except Exception:
                    service.lock_file.close()
                    raise
            service.start()
            self.services[digest] = service
        return self.services[digest]

    def login(self, code, client_key='default', resume_token=None):
        with self.guard:
            if self.closing.is_set():
                raise HTTPException(503, '테스트 서버가 종료 중입니다.')
            now = time.time()
            if not self.failed_logins.check(client_key):
                raise HTTPException(429, '접속 시도가 많습니다. 1분 뒤 다시 시도하세요.')
            if not isinstance(code, str) or not hmac.compare_digest(code.encode(), self.invite_code.encode()):
                self.failed_logins.record(client_key)
                raise HTTPException(401, '테스트 접속 코드를 확인하세요.')
            self.failed_logins.clear(client_key)
            self.cleanup_expired()
            if resume_token is not None:
                if not isinstance(resume_token, str) or not 1 <= len(resume_token) <= 256:
                    raise HTTPException(401, '기존 테스트 세션을 확인할 수 없습니다.')
                digest = hashlib.sha256(resume_token.encode()).hexdigest()
                if self.sessions.get(digest, 0) <= now:
                    raise HTTPException(401, '기존 테스트 세션이 만료되었습니다.')
                self._service(digest)
                return {'token': resume_token, 'expires_at': self.sessions[digest]}
            active_sessions = sum(expires_at > now for expires_at in self.sessions.values())
            if active_sessions >= MAX_ACTIVE_SESSIONS:
                raise HTTPException(429, f'유효한 테스트 세션 {MAX_ACTIVE_SESSIONS}개가 모두 사용 중입니다. 세션은 접속 후 24시간에 만료됩니다. 운영자에게 문의하세요.')
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            self.sessions[digest] = now + 24 * 3600
            temporary = self.session_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.sessions))
            os.chmod(temporary, 0o600)
            temporary.replace(self.session_path)
            self._service(digest)
            return {'token': token, 'expires_at': self.sessions[digest]}

    def authenticate(self, authorization):
        if not authorization.startswith('Bearer '):
            raise HTTPException(401, '테스트 접속 코드로 먼저 접속하세요.')
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with self.guard:
            if self.sessions.get(digest, 0) <= time.time():
                raise HTTPException(401, '테스트 세션이 만료되었습니다. 다시 접속하세요.')
            return self._service(digest)

    def close(self):
        self.closing.set()
        if hasattr(self, 'reaper'):
            self.reaper.join(timeout=5)
        with self.guard:
            for service in self.services.values():
                service.close()
            self.services.clear()
