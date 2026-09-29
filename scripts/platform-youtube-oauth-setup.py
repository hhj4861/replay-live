#!/usr/bin/env python3
"""One-use loopback form: pass OAuth credentials to the isolated live-test VM in memory.

Requires the prepared replay-auto-live VM and runtime wrapper. No credentials are
written to disk, argv or logs. The existing runtime must be idle before replacement.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time

REMOTE = r'''
import json,os,runpy,signal,sqlite3,time
from pathlib import Path
root=Path('/home/lima.linux/replay-auto-live')
assert Path('/etc/replay-platform-test-vm').is_file()
values=json.loads(input())
c=sqlite3.connect('/home/lima.linux/replay-auto-live-data/platform-test.sqlite3')
assert c.execute("select count(*) from replay_jobs where state not in ('completed','failed','stopped')").fetchone()[0] == 0
assert c.execute('select count(*) from replay_automations where enabled=1 or active_run is not null').fetchone()[0] == 0
c.close()
for entry in Path('/proc').iterdir():
 if not entry.name.isdigit() or int(entry.name)==os.getpid(): continue
 try: args=(entry/'cmdline').read_bytes().split(b'\0')
 except OSError: continue
 if str(root/'replay-auto-live-runtime.py').encode() in args:
  os.kill(int(entry.name),signal.SIGTERM)
  for _ in range(100):
   if not entry.exists(): break
   time.sleep(.1)
  else: raise RuntimeError('Previous runtime did not stop')
os.environ['REPLAY_YOUTUBE_CLIENT_ID']=values.pop('client_id')
os.environ['REPLAY_YOUTUBE_CLIENT_SECRET']=values.pop('client_secret')
os.chdir(root)
import sys
sys.path.insert(0,str(root))
runpy.run_path(str(root/'replay-auto-live-runtime.py'),run_name='__main__')
'''


def main():
    nonce = secrets.token_urlsafe(32)
    deadline = time.monotonic() + 1800
    child = None
    completed = False
    ready = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body, kind='text/html; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body.encode())

        def valid(self):
            return (not completed and time.monotonic() < deadline and self.path == '/' + nonce
                    and self.headers.get('Host') == origin.removeprefix('http://'))

        def do_GET(self):
            if not self.valid():
                return self.respond(404, '입력 화면이 만료되었습니다.')
            self.respond(200, '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>YouTube 방송 연결 준비</title>
<style>body{font:16px system-ui;background:#f4f6f8;color:#172034;margin:60px auto;max-width:620px;padding:24px}form{background:white;padding:28px;border-radius:20px}label{display:block;margin:20px 0}input{box-sizing:border-box;display:block;width:100%;padding:12px;margin-top:8px;border:1px solid #bbc4d0;border-radius:8px}button{border:0;border-radius:10px;background:#2456dc;color:white;font:inherit;padding:14px 24px}p{line-height:1.6}code{overflow-wrap:anywhere}</style>
<h1>YouTube 방송 연결 준비</h1><p>Google Cloud의 <b>웹 애플리케이션</b> 클라이언트를 입력하세요. 이 Mac의 테스트 서버 메모리에만 전달합니다.</p>
<p>Google Cloud → 클라이언트 → 승인된 리디렉션 URI에 다음 주소를 등록해 주세요.<br><code>http://127.0.0.1:18092/api/youtube-channel/callback</code></p>
<p>YouTube Data API v3를 활성화하고, 테스트 사용자에 방송할 계정을 추가해 주세요.</p>
<form method="post" autocomplete="off"><label>클라이언트 ID<input name="client_id" required autocomplete="off"></label><label>클라이언트 시크릿<input name="client_secret" type="password" required autocomplete="new-password"></label><button type="submit">안전하게 연결 준비</button><p>다음 단계에서 Replay 화면의 ‘YouTube 방송 권한 연결’을 눌러 동의합니다.</p></form></html>''')

        def do_POST(self):
            nonlocal child, completed
            if not self.valid() or self.headers.get('Origin') != origin:
                return self.respond(403, '입력 요청을 확인할 수 없습니다.')
            from urllib.parse import parse_qs
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 8192: raise ValueError()
                data = parse_qs(self.rfile.read(length).decode(), strict_parsing=True)
                client_id, client_secret = data['client_id'][0].strip(), data['client_secret'][0].strip()
                if not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com', client_id): raise ValueError()
                if not 8 <= len(client_secret) <= 500 or any(c.isspace() for c in client_secret): raise ValueError()
            except (ValueError, KeyError, UnicodeError):
                return self.respond(400, '입력 형식을 확인하고 뒤로 가서 다시 입력해 주세요.')
            env = {**os.environ, 'LIMA_HOME': '/private/tmp/replay-auto-live-vm'}
            child = subprocess.Popen(['limactl', 'shell', 'replay-auto-live', '--',
                '/home/lima.linux/replay-auto-live/.venv/bin/python', '-u', '-c', REMOTE,
                '/home/lima.linux/replay-auto-live/replay-auto-live-runtime.py'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True)
            child.stdin.write(json.dumps({'client_id': client_id, 'client_secret': client_secret}) + '\n')
            child.stdin.flush()
            child.stdin.close()
            del data, client_id, client_secret
            def monitor():
                for line in child.stdout:
                    try: value = json.loads(line)
                    except ValueError: continue
                    if 'ready' in value: ready.put(value['ready'])
                ready.put(False)
            threading.Thread(target=monitor, daemon=True).start()
            try: success = ready.get(timeout=40)
            except queue.Empty: success = False
            if not success:
                if child.poll() is None: child.terminate()
                child.wait(timeout=15)
                child = None
                return self.respond(500, '테스트 서버를 시작하지 못했습니다. 설정과 실행 상태를 확인해 주세요. 비밀 값은 저장하지 않았습니다.')
            completed = True
            self.respond(200, '<!doctype html><html lang="ko"><meta charset="utf-8"><title>연결 준비 완료</title><h1>연결 준비 완료</h1><p>이 창을 닫고 Replay에서 방송 관리 권한을 연결해 주세요.</p><a href="http://127.0.0.1:13102/#automations">Replay 자동 송출 열기</a></html>')
            threading.Thread(target=server.shutdown, daemon=True).start()

    server = HTTPServer(('127.0.0.1', 0), Handler)
    server.timeout = 1
    origin = 'http://127.0.0.1:' + str(server.server_address[1])
    print(json.dumps({'input_url': origin + '/' + nonce, 'expires_in_minutes': 30}), flush=True)
    timer = threading.Timer(1800, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        timer.cancel()
    if child:
        print(json.dumps({'configured': True, 'credentials_on_disk': False}), flush=True)
        child.wait()


if __name__ == '__main__':
    main()
