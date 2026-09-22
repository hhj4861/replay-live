# PyInstaller collects binary dependencies of FFmpeg/ffprobe as well as Python.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all
root = Path(SPECPATH).parent
media = Path(os.environ['REPLAY_HELPER_MEDIA_BIN'])
extra_data, extra_bins, hidden = collect_all('yt_dlp')
# Never package build host environment files, credentials or browser profiles.
a = Analysis([str(root / 'desktop/helper.py')], pathex=[str(root)],
    binaries=extra_bins + [(str(media / name), 'media-bin') for name in os.environ['REPLAY_HELPER_MEDIA_NAMES'].split(',')],
    datas=extra_data + [(os.environ['REPLAY_HELPER_CONFIG'], '.'), (str(root / 'desktop/README.md'), '.')],
    hiddenimports=hidden + ['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on'],
    excludes=['pytest', 'boto3', 'botocore', 'psycopg', 'sqlalchemy'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ReplayLiveHelper', console=False,
    argv_emulation=os.sys.platform == 'darwin', codesign_identity=os.environ.get('REPLAY_HELPER_SIGN_IDENTITY'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ReplayLiveHelper')
if os.sys.platform == 'darwin':
    app = BUNDLE(coll, name='Replay Live Helper.app', bundle_identifier='app.replaylive.helper',
        version=os.environ['REPLAY_HELPER_VERSION'], info_plist={
            'LSUIElement': True, 'CFBundleDisplayName': 'Replay Live Helper',
            'CFBundleURLTypes': [{'CFBundleURLName': 'Replay Live Helper', 'CFBundleURLSchemes': ['replay-live-helper']}]})
