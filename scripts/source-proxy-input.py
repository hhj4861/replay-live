"""One-use loopback proxy input; forward only in memory to a local fixed command.

Usage: python scripts/source-proxy-input.py -- node /path/to/diagnostic.mjs
The command receives REPLAY_DIAGNOSTIC_PROXY_URL. Form input is never executed.
"""
import argparse
import html
import http.server
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.parse


def serve(command, lifetime=1800):
    route = '/' + secrets.token_urlsafe(24)
    csrf = secrets.token_urlsafe(32)
    deadline = time.monotonic() + lifetime
    lock = threading.Lock()
    state = {'busy': False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, message, status=200, form=False):
            body = ('<!doctype html><meta name="viewport" content="width=device-width">'
                    '<title>Replay 다운로드 진단</title><style>body{font:17px system-ui;'
                    'max-width:620px;margin:60px auto;padding:24px;color:#16243a}'
                    'input{width:95%;padding:14px;margin:16px 0}button{padding:14px 24px;'
                    'background:#3155df;color:white;border:0;border-radius:10px}</style>'
                    '<h1>다운로드 오류 확인</h1><p>' + html.escape(message) + '</p>')
            if form:
                body += ('<p>입력값은 파일에 저장하지 않고 이번 진단에만 사용합니다.</p>'
                         '<form method="post" action="' + route + '">'
                         '<input type="hidden" name="csrf" value="' + csrf + '">'
                         '<label>프록시 URL<input name="proxy" type="password" '
                         'autocomplete="off" required></label>'
                         '<button>안전하게 입력하고 테스트</button></form>')
            payload = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            # no-referrer makes a native browser form POST send Origin: null.
            # Preserve same-origin submissions without leaking this route externally.
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def valid(self):
            return (self.path == route and time.monotonic() < deadline
                    and self.headers.get('Host') == authority)

        def do_GET(self):
            if not self.valid():
                return self.reply('만료되었거나 잘못된 주소입니다. 새 링크를 요청해 주세요.', 410)
            if state['busy']:
                return self.reply('입력 완료. 서버에서 진단 중입니다. 이 창은 닫아도 됩니다.')
            self.reply('DataImpulse에서 복사한 프록시 URL을 입력해 주세요.', form=True)

        def do_POST(self):
            if not self.valid():
                return self.reply('입력 링크가 만료되었습니다. 새 링크를 요청해 주세요.', 410)
            if self.headers.get('Origin') != origin:
                return self.reply('입력 화면을 새로 열고 다시 제출해 주세요.', 403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length < 8192:
                    raise ValueError()
                fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
                if not secrets.compare_digest(fields.get('csrf', [''])[0], csrf):
                    return self.reply('입력 화면을 새로 열고 다시 제출해 주세요.', 403)
                value = fields.get('proxy', [''])[0].strip()
                parsed = urllib.parse.urlsplit(value)
                if (parsed.scheme != 'http' or parsed.hostname != 'gw.dataimpulse.com'
                        or parsed.port != 823 or not parsed.username or not parsed.password
                        or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                        or any(ord(c) < 32 for c in urllib.parse.unquote(value))):
                    raise ValueError()
            except (ValueError, UnicodeError):
                return self.reply('프록시 URL 형식을 확인해 주세요. 입력값은 저장하지 않았습니다.', 400, form=True)
            with lock:
                if state['busy']:
                    return self.reply('이미 진단 중입니다.', 409)
                state['busy'] = True
            self.reply('입력 완료. 서버에서 진단을 시작했습니다. 이 창은 닫아도 됩니다.')
            threading.Thread(target=run, args=(value,), daemon=False).start()

    def run(value):
        try:
            result = subprocess.run(command, env={**os.environ, 'REPLAY_DIAGNOSTIC_PROXY_URL': value},
                                    capture_output=True, text=True, timeout=300)
            # Only diagnostic JSON reaches the console; never echo arbitrary errors.
            for line in result.stdout.splitlines():
                try:
                    record = json.loads(line)
                    if value not in line and isinstance(record, dict):
                        print(json.dumps(record), flush=True)
                except ValueError:
                    pass
            print(json.dumps({'diagnostic_exit': result.returncode}), flush=True)
        except (OSError, subprocess.SubprocessError):
            print(json.dumps({'diagnostic_error': 'RUN_FAILED'}), flush=True)
        finally:
            server.shutdown()

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    authority = f'127.0.0.1:{server.server_port}'
    origin = 'http://' + authority
    timer = threading.Timer(lifetime, server.shutdown)
    timer.daemon = True
    timer.start()
    print(json.dumps({'input_url': origin + route}), flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        timer.cancel()
        server.server_close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a local diagnostic command is required')
    serve(command)
