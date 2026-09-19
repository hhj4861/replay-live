"""Package only the validated static web build for Vercel Build Output API."""
import json
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent.parent / 'web'
source = root / 'dist-vercel'
if not (source / 'index.html').is_file():
    raise SystemExit('Run npm run build:vercel first')
output = root / '.vercel' / 'output'
output.mkdir(parents=True, exist_ok=True)
shutil.copytree(source, output / 'static', dirs_exist_ok=True)
config = {'version': 3, 'routes': [
    {'src': '/.*', 'headers': {'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY'}},
    {'handle': 'filesystem'}
]}
(output / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
print('Packaged static assets only')
