#!/usr/bin/env python3
"""Serve the existing commercial UI against the separate local live-test VM."""
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
API = 'http://127.0.0.1:18092'
WEB = 'http://127.0.0.1:13102'


def main():
    spec = importlib.util.spec_from_file_location('local_preview', ROOT / 'scripts/commercial-preview.py')
    preview = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preview)
    preview.require_free_ports((13102,))
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    node = Path('/opt/homebrew/opt/node@24/bin/node')
    if not node.is_file():
        raise RuntimeError('PREVIEW_NODE24_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='replay-live-test-web-') as folder:
        scratch = Path(folder)
        config = preview.preview_build_files(scratch, API, platform_live=True)
        env = preview.preview_build_environment(API, node)
        vite = str(ROOT / 'web/node_modules/vite/bin/vite.js')
        result = subprocess.run([str(node), vite, 'build', '--config', str(config), '--outDir', str(scratch / 'site')],
                                cwd=ROOT / 'web', env=env, capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError('LIVE_TEST_WEB_BUILD_FAILED')
        with (scratch / 'web.log').open('w') as log:
            child = subprocess.Popen([str(node), vite, 'preview', '--config', str(config), '--outDir', str(scratch / 'site'),
                                      '--host', '127.0.0.1', '--port', '13102', '--strictPort'],
                                     cwd=ROOT / 'web', env=env, stdout=log, stderr=log)
            try:
                print(json.dumps({'url': WEB + '/', 'api': API, 'web_process_started': True, 'scratch': folder}), flush=True)
                while not stopping.wait(.5):
                    if child.poll() is not None:
                        raise RuntimeError('LIVE_TEST_WEB_STOPPED')
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': 'LIVE_TEST_WEB_FAILED', 'type': type(error).__name__}), flush=True)
        raise SystemExit(1)
