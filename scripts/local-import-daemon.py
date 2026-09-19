#!/usr/bin/env python3
"""Start the local recording helper; Ctrl+C stops it and clears temporary files."""
import argparse
import logging
from pathlib import Path
import shutil
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import uvicorn
from server.local_import_daemon import DEFAULT_ORIGINS, PORT, LocalImports, create_local_import_app


def main():
    parser = argparse.ArgumentParser(description='Replay Live 영상 가져오기 도우미')
    parser.add_argument('--origin', action='append', default=[], help='추가로 허용할 정확한 웹 출처')
    parser.add_argument('--cloud-url', default='https://replay-live-api.vercel.app', help='신뢰하는 클라우드 API의 고정 HTTPS 출처')
    args = parser.parse_args()
    log = logging.getLogger('replay.local_import')
    log.setLevel(logging.INFO)
    log.addHandler(logging.StreamHandler())
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        parser.error('FFmpeg와 ffprobe를 먼저 설치하세요. macOS: brew install ffmpeg')
    try:
        with socket.socket() as check:
            check.bind(('127.0.0.1', PORT))
    except OSError:
        parser.error('17833 포트를 사용 중입니다. 이미 실행한 도우미의 연결 코드를 사용하세요.')
    manager = LocalImports(cloud_url=args.cloud_url)
    try:
        app = create_local_import_app(origins=(*DEFAULT_ORIGINS, *args.origin), manager=manager)
        print(f'Replay Live 영상 가져오기 도우미\n주소: http://127.0.0.1:{PORT}\n'
              f'연결 코드: {manager.pairing_code}\n웹의 「내 컴퓨터 연결」에 입력하세요. 종료: Ctrl+C', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=PORT, access_log=False, log_level='warning')
    finally:
        manager.close()


if __name__ == '__main__':
    main()
