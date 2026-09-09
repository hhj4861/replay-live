"""Generate a copyright-free 15 second H.264/AAC test clip."""
from pathlib import Path
import subprocess

path = Path(__file__).resolve().parent.parent / 'data' / 'sample.mp4'
path.parent.mkdir(exist_ok=True)
subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                '-t', '15', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-movflags', '+faststart', str(path)], check=True)
print(path)
