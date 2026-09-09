"""Exercise the running UI proxy -> API -> SQLite -> FFmpeg -> FLV path."""
import json
from pathlib import Path
import subprocess
import time
import httpx

base = Path(__file__).resolve().parent.parent
with httpx.Client(base_url='http://127.0.0.1:3000', headers={'X-Replay-Client': '1'}, timeout=60) as client:
    assert client.get('/').status_code == 200
    assert client.get('/api/health').json()['ffmpeg']
    with (base / 'data/sample.mp4').open('rb') as file:
        response = client.post('/api/media', files={'file': ('POC-test-15s.mp4', file, 'video/mp4')})
    response.raise_for_status()
    media = response.json()
    response = client.post('/api/broadcasts', json={'media_id': media['id'], 'title': 'POC 검증 · 15초 테스트 송출', 'target': 'local'})
    response.raise_for_status()
    job_id = response.json()['id']
    observed = []
    for _ in range(90):
        job = client.get(f'/api/broadcasts/{job_id}').json()
        if not observed or observed[-1] != job['state']:
            observed.append(job['state'])
        if job['state'] in {'completed', 'failed'}:
            break
        time.sleep(.5)
    assert job['state'] == 'completed', job
    output = client.get(f'/api/broadcasts/{job_id}/output')
    output.raise_for_status()
    path = base / 'data/smoke-output.flv'
    path.write_bytes(output.content)
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
    assert float(probe['format']['duration']) > 14
    result = {'result': 'passed', 'path': 'HTTP UI proxy -> API -> SQLite -> FFmpeg -> FLV', 'states': observed,
              'duration': float(probe['format']['duration']), 'codecs': [s['codec_name'] for s in probe['streams']],
              'bytes': len(output.content), 'job_id': job_id}
    (base / 'docs/smoke-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
