"""Build a standalone, opt-in installer; no upload or release publication."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', default='0.2.0')
    parser.add_argument('--output', type=Path, default=ROOT / 'release-artifacts/helper')
    parser.add_argument('--cloudflare', action='store_true', help='Use only after API/Pages production cutover')
    args = parser.parse_args()
    if not re.fullmatch(r'\d+\.\d+\.\d+', args.version): parser.error('Use a semantic release version')
    if sys.platform not in ('darwin', 'win32'): parser.error('Build on macOS or Windows')
    names = ['ffmpeg', 'ffprobe'] if sys.platform == 'darwin' else ['ffmpeg.exe', 'ffprobe.exe']
    tools = [shutil.which(name) for name in names]
    if not all(tools): parser.error('FFmpeg and ffprobe must be installed on the build host')
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='replay-helper-build-') as temp:
        work = Path(temp); media = work / 'media-bin'; media.mkdir()
        for source, name in zip(tools, names): shutil.copy2(source, media / name)
        config = work / 'helper-config.json'
        config.write_text(json.dumps({'version': args.version,
            'cloud_url': 'https://replay-live-api.guswhd1085.workers.dev' if args.cloudflare else 'https://replay-live-api.vercel.app',
            'web_origin': 'https://replay-live.pages.dev' if args.cloudflare else 'https://replay-live-poc.vercel.app'}))
        env = dict(os.environ, REPLAY_HELPER_MEDIA_BIN=str(media), REPLAY_HELPER_MEDIA_NAMES=','.join(names),
                   REPLAY_HELPER_CONFIG=str(config), REPLAY_HELPER_VERSION=args.version)
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', str(output / 'dist'),
                        '--workpath', str(work / 'pyinstaller'), str(ROOT / 'desktop/helper.spec')], env=env, cwd=ROOT, check=True)
        product = output / 'dist' / ('Replay Live Helper.app' if sys.platform == 'darwin' else 'ReplayLiveHelper')
        exe = product / ('Contents/MacOS/ReplayLiveHelper' if sys.platform == 'darwin' else 'ReplayLiveHelper.exe')
        subprocess.run([str(exe), '--self-test'], check=True, timeout=90)
        arch = 'arm64' if platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
        filename = f'ReplayLiveHelper-{args.version}-{"macos" if sys.platform == "darwin" else "windows"}-{arch}'
        archive = output / (filename + '.zip')
        if sys.platform == 'darwin':
            subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(product), str(archive)], check=True)
        else: shutil.make_archive(str(output / filename), 'zip', product.parent, product.name)
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        (output / (filename + '.sha256')).write_text(f'{sha}  {archive.name}\n')
        (output / (filename + '.json')).write_text(json.dumps({'version': args.version,
            'label': 'macOS Apple Silicon' if sys.platform == 'darwin' and arch == 'arm64' else 'macOS Intel' if sys.platform == 'darwin' else 'Windows x64',
            'url': f'https://github.com/hhj4861/replay-live/releases/download/helper-v{args.version}/{archive.name}',
            'sha256': sha, 'bytes': archive.stat().st_size}, indent=2))
        print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': sha}))


if __name__ == '__main__': main()
