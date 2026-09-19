import os
from pathlib import Path
from .app import create_app, BASE

origins = [v.strip() for v in os.environ.get('REPLAY_PUBLIC_ORIGINS', '').split(',') if v.strip()]
if not origins:
    raise RuntimeError('REPLAY_PUBLIC_ORIGINS에 Vercel 화면의 정확한 HTTPS origin을 지정하세요.')
hosts = [v.strip() for v in os.environ.get('REPLAY_PUBLIC_HOSTS', '').split(',') if v.strip()]
app = create_app(Path(os.environ.get('REPLAY_PUBLIC_DATA', BASE / 'data-public')), public_origins=origins,
                 allowed_hosts=hosts or None, deadline_file=os.environ.get('REPLAY_CLOUD_DEADLINE_FILE'))
