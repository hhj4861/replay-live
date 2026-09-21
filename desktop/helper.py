"""User-installed desktop launcher. No cloud session or download URL is persisted."""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

NAME = 'Replay Live Helper'
LABEL = 'app.replaylive.helper'
SCHEME = 'replay-live-helper://start'
ORIGINS = ('https://replay-live-poc.vercel.app', 'https://replay-live.pages.dev')
APIS = ('https://replay-live-api.vercel.app', 'https://replay-live-api.guswhd1085.workers.dev')


def bundle_root():
    exe = Path(sys.executable).resolve()
    return exe.parents[2] if sys.platform == 'darwin' else exe.parent


def executable(root):
    return root / 'Contents/MacOS/ReplayLiveHelper' if sys.platform == 'darwin' else root / 'ReplayLiveHelper.exe'


def install_root():
    if sys.platform == 'darwin':
        return Path.home() / 'Applications' / f'{NAME}.app'
    if sys.platform == 'win32':
        return Path(os.environ['LOCALAPPDATA']) / 'ReplayLiveHelper'
    raise RuntimeError('macOS와 Windows에서 설치할 수 있습니다.')


def state_root():
    root = (Path.home() / 'Library/Application Support/ReplayLiveHelper' if sys.platform == 'darwin'
            else Path(os.environ['LOCALAPPDATA']) / 'ReplayLiveHelperState')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def configuration():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    value = json.loads((base / 'helper-config.json').read_text())
    if value['cloud_url'] not in APIS or value['web_origin'] not in ORIGINS:
        raise ValueError('Invalid packaged service destination')
    return value


def confirm(message):
    if sys.platform == 'darwin':
        script = ('display dialog ' + json.dumps(message, ensure_ascii=False) + ' with title "Replay Live Helper" '
                  'buttons {"취소", "확인"} default button "확인" cancel button "취소"')
        return subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True).returncode == 0
    import ctypes
    return ctypes.windll.user32.MessageBoxW(None, message, NAME, 0x21) == 1


def notice(message):
    if sys.platform == 'darwin':
        subprocess.run(['/usr/bin/osascript', '-e', 'display alert "Replay Live Helper" message ' + json.dumps(message, ensure_ascii=False)], capture_output=True)
    else:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, NAME, 0x40)


def set_startup(root, enabled):
    """Only current-user login state; never installs a privileged service."""
    command = [str(executable(root)), '--serve']
    if sys.platform == 'darwin':
        path = Path.home() / 'Library/LaunchAgents' / f'{LABEL}.plist'
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {'Label': LABEL, 'ProgramArguments': command, 'RunAtLoad': True,
                       'ProcessType': 'Background', 'LimitLoadToSessionType': 'Aqua'}
            path.write_bytes(plistlib.dumps(payload)); path.chmod(0o600)
            # RunAtLoad applies on the next login. Start immediately via launcher.
        else:
            path.unlink(missing_ok=True)
            subprocess.run(['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)
    else:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run') as key:
            if enabled: winreg.SetValueEx(key, 'ReplayLiveHelper', 0, winreg.REG_SZ, subprocess.list2cmdline(command))
            else:
                try: winreg.DeleteValue(key, 'ReplayLiveHelper')
                except FileNotFoundError: pass


def register_protocol(root):
    if sys.platform == 'darwin':
        subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister', '-f', str(root)], check=True, capture_output=True)
        return
    import winreg
    exe = str(executable(root))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\replay-live-helper') as key:
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, 'URL:Replay Live Helper')
        winreg.SetValueEx(key, 'URL Protocol', 0, winreg.REG_SZ, '')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Classes\replay-live-helper\shell\open\command') as key:
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, f'"{exe}" --open "%1"')
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Uninstall\ReplayLiveHelper') as key:
        for name, value in {'DisplayName': NAME, 'UninstallString': f'"{exe}" --uninstall', 'InstallLocation': str(root)}.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def occupied(port=17833):
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1', port)) == 0


def stop_installed():
    path = state_root() / 'control.json'
    if not occupied(): return
    if not path.exists(): raise RuntimeError('이전 개발용 도우미를 먼저 종료한 뒤 다시 설치하세요.')
    token = json.loads(path.read_text())['token']
    req = urllib.request.Request('http://127.0.0.1:17833/desktop/stop', data=b'', method='POST', headers={
        'Origin': ORIGINS[0], 'X-Replay-Local': '1', 'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=5) as result:
        if result.status != 202: raise RuntimeError('실행 중인 도우미를 종료하지 못했습니다.')
    for _ in range(100):
        if not occupied(): return
        time.sleep(.1)
    raise RuntimeError('가져오기가 종료될 때까지 기다린 뒤 다시 시도하세요.')


def install(source, target):
    if source == target: return
    if target.is_symlink(): raise RuntimeError('설치 경로를 확인해 주세요.')
    stop_installed()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Copy completely before replacing the prior installation.
    staging = target.with_name(target.name + '.installing')
    if staging.exists(): raise RuntimeError('이전 설치가 진행 중입니다. 설치 경로를 확인해 주세요.')
    shutil.copytree(source, staging, symlinks=True)
    backup = target.with_name(target.name + '.previous')
    if backup.exists(): raise RuntimeError('이전 설치 백업을 확인해 주세요.')
    try:
        if target.exists(): target.rename(backup)
        staging.rename(target)
    except Exception:
        if backup.exists() and not target.exists(): backup.rename(target)
        raise
    if backup.exists(): shutil.rmtree(backup)


def uninstall(root):
    set_startup(root, False)
    stop_installed()
    if sys.platform == 'darwin':
        subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister', '-u', str(root)], capture_output=True)
        shutil.rmtree(root)
    else:
        import winreg
        for path in [r'Software\Classes\replay-live-helper\shell\open\command', r'Software\Classes\replay-live-helper\shell\open',
                     r'Software\Classes\replay-live-helper\shell', r'Software\Classes\replay-live-helper',
                     r'Software\Microsoft\Windows\CurrentVersion\Uninstall\ReplayLiveHelper']:
            try: winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError: pass
        # Windows cannot delete this running executable. Remove it next login.
        # Do not use arbitrary shell command interpolation for directory removal.
        notice('자동 시작과 웹 연결 등록을 해제했습니다. 도우미가 종료되면 설치 폴더를 삭제하세요:\n' + str(root))
    (state_root() / 'control.json').unlink(missing_ok=True)


def prepare_media_tools():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'media-bin'
    if not base.is_dir(): raise RuntimeError('설치 파일의 영상 도구를 확인해 주세요.')
    os.environ['PATH'] = str(base) + os.pathsep + os.environ.get('PATH', '')
    for name in ('ffmpeg', 'ffprobe'):
        tool = base / (name + ('.exe' if sys.platform == 'win32' else ''))
        subprocess.run([str(tool), '-version'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def serve():
    if occupied(): return
    prepare_media_tools()
    import uvicorn
    from fastapi import Request
    from fastapi.responses import Response
    from server.local_import_daemon import LocalImports, create_local_import_app
    settings = configuration()
    manager = LocalImports(cloud_url=settings['cloud_url'])
    app = create_local_import_app(origins=ORIGINS, manager=manager)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=17833, access_log=False, log_config=None))
    token = secrets.token_urlsafe(32)

    async def stop(request: Request):
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            return Response(status_code=403)
        server.should_exit = True
        return Response(status_code=202)
    # Imported locally; avoid unresolved forward-reference annotations in FastAPI.
    stop.__annotations__['request'] = Request
    app.post('/desktop/stop')(stop)
    path = state_root() / 'control.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream: json.dump({'token': token}, stream)
    try: server.run()
    finally:
        manager.close()
        if path.exists() and json.loads(path.read_text()).get('token') == token: path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('launch_url', nargs='?')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--open')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args(argv)
    if args.launch_url:
        if args.open: raise ValueError('Duplicate launch URL')
        args.open = args.launch_url
    if args.open is not None and args.open != SCHEME: raise ValueError('Invalid helper launch URL')
    if args.self_test:
        prepare_media_tools(); configuration()
        import uvicorn, yt_dlp
        from server.local_import_daemon import create_local_import_app
        from server.device_import_worker import run_device_import
        from server.media_runtime import validate_media
        from fastapi.testclient import TestClient
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / 'self-test.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=10',
                '-f', 'lavfi', '-i', 'sine=frequency=400', '-t', '1', '-c:v', 'libx264', '-pix_fmt',
                'yuv420p', '-c:a', 'aac', str(media)], check=True, timeout=30)
            assert validate_media(media)['duration'] >= 1
            with TestClient(create_local_import_app(origins=ORIGINS), base_url='http://127.0.0.1:17833') as client:
                headers = {'Origin': ORIGINS[0], 'X-Replay-Local': '1'}
                result = client.get('/pairing-code', headers=headers)
                assert result.status_code == 200
                paired = client.post('/pair', json={'code': result.json()['code']}, headers=headers)
                assert paired.status_code == 200
                assert client.get('/session', headers={**headers, 'Authorization': 'Bearer ' + paired.json()['token']}).status_code == 200
        print('Bundled media decode and local pairing: OK')
        return
    if args.serve:
        serve(); return
    if not getattr(sys, 'frozen', False): raise RuntimeError('설치용 패키지에서 실행하세요.')
    source, target = bundle_root(), install_root()
    if args.uninstall:
        if confirm('도우미의 자동 실행과 연결 등록을 해제하고 제거하시겠어요?'): uninstall(target)
        return
    if source != target:
        if args.open: raise RuntimeError('도우미를 먼저 설치하세요.')
        if not confirm('이 PC에 Replay Live Helper를 설치합니다. 영상 링크를 가져올 때 이 PC의 네트워크와 임시 저장 공간을 사용합니다. 설치하시겠어요?'): return
        install(source, target); register_protocol(target)
        enabled = confirm('PC에 로그인할 때 도우미를 자동 실행하시겠어요? 취소를 누르면 웹의 도우미 실행 버튼으로 직접 시작할 수 있습니다.')
        set_startup(target, enabled)
    elif not args.open:
        if sys.platform == 'darwin':
            script = 'choose from list {"도우미 실행", "자동 시작 켜기", "자동 시작 끄기", "도우미 제거"} with title "Replay Live Helper" with prompt "도우미 설정"'
            choice = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True, check=True).stdout.strip()
            if choice == 'false': return
            if choice == '도우미 제거':
                if confirm('도우미를 제거하시겠어요?'): uninstall(target)
                return
            if choice.startswith('자동 시작'): set_startup(target, choice.endswith('켜기'))
        else:
            if confirm('자동 시작을 켜시겠어요? 취소를 누르면 자동 시작을 끕니다. 제거는 Windows 설치된 앱에서 할 수 있습니다.'):
                set_startup(target, True)
            else: set_startup(target, False)
    if not occupied():
        subprocess.Popen([str(executable(target)), '--serve'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **({'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS} if sys.platform == 'win32' else {'start_new_session': True}))
    if source != target: notice('설치했습니다. 웹으로 돌아가면 자동으로 연결됩니다. 연결되지 않으면 다시 확인을 누르세요.')


if __name__ == '__main__':
    try:
        if len(sys.argv) > 5 and sys.argv[1] == '--bounded-exec':
            from server.media_runtime import _exec_with_limits
            _exec_with_limits()
        else: main()
    except Exception:
        if '--serve' not in sys.argv and '--self-test' not in sys.argv:
            notice('도우미를 실행하지 못했습니다. 진행 중인 가져오기를 마치고 이전 도우미를 종료한 뒤 다시 시도하세요.')
        raise SystemExit(1)
