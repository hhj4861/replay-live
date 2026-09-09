"""Real RTMP publisher -> local FFmpeg listener. No external broadcast."""
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.service import Service

base = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'rtmp://127.0.0.1:{port}/live/poc-test'
    output = root / 'received.flv'
    receiver = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-listen', '1', '-i', url, '-c', 'copy', '-f', 'flv', str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    service = Service(root / 'service')
    try:
        time.sleep(.5)
        command = service.command(str(base / 'data/sample.mp4'), url)
        sender = subprocess.run(command, capture_output=True, timeout=40)
        assert sender.returncode == 0, sender.stderr.decode()
        receiver.wait(timeout=10)
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(output)]))
        assert float(info['format']['duration']) >= 14
        assert {s['codec_name'] for s in info['streams']} == {'h264', 'aac'}
        result = {'result': 'passed', 'transport': 'RTMP loopback', 'duration': float(info['format']['duration']), 'bytes': output.stat().st_size,
                  'codecs': [s['codec_name'] for s in info['streams']], 'external_network': False}
        (base / 'docs/rtmp-result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    finally:
        if receiver.poll() is None:
            receiver.kill()
        receiver.wait()
        receiver.stderr.close()
        service.close()
