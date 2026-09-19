"""Verify the external HTTPS API with isolated invited test sessions."""
import json
from pathlib import Path
import time
from datetime import datetime, timezone, timedelta
import httpx

base = Path(__file__).resolve().parent.parent
config = json.loads((base / 'data-public/runtime.json').read_text())
headers = {'Origin': config['ui_origin'], 'X-Replay-Client': '1'}
code = (base / 'data-public/invite-code.txt').read_text().strip()
with httpx.Client(timeout=60) as client:
    page = client.get(config['ui_origin'])
    assert page.status_code == 200, ('UI status', page.status_code)
with httpx.Client(base_url=config['api_origin'], headers=headers, timeout=90) as client:
    assert client.get('/api/media').status_code == 401
    preflight = client.options('/api/media', headers={'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,x-replay-client'})
    assert preflight.status_code == 200 and preflight.headers['access-control-allow-origin'] == config['ui_origin']
    response = client.post('/api/session', json={'code': code})
    assert response.status_code == 200, response.status_code
    client.headers['Authorization'] = 'Bearer ' + response.json()['token']
    shared = client.get('/api/media').json()
    assert len(shared) == 1 and shared[0]['id'] == 'shared-sample'
    assert client.get('/api/media/shared-sample/preview').content == (base / 'data/sample.mp4').read_bytes()
    with (base / 'data/sample.mp4').open('rb') as file:
        response = client.post('/api/media', files={'file': ('external-test-15s.mp4', file, 'video/mp4')})
    assert response.status_code == 201, response.text
    media = response.json()
    response = client.post('/api/broadcasts', json={'media_id': media['id'], 'title': '외부 HTTPS 통합 테스트', 'target': 'local'})
    assert response.status_code == 201, response.text
    job = response.json()
    states = []
    for _ in range(90):
        job = client.get(f"/api/broadcasts/{job['id']}").json()
        if not states or states[-1] != job['state']:
            states.append(job['state'])
        if job['state'] in {'completed', 'failed'}:
            break
        time.sleep(.5)
    assert job['state'] == 'completed', job
    output = client.get(f"/api/broadcasts/{job['id']}/output")
    assert output.status_code == 200 and output.content[:3] == b'FLV'
    scheduled = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    pending = client.post('/api/broadcasts', json={'media_id': media['id'], 'title': '외부 예약 취소 검증', 'target': 'local', 'scheduled_at': scheduled}).json()
    cancelled = client.post(f"/api/broadcasts/{pending['id']}/stop")
    assert cancelled.status_code == 200 and cancelled.json()['state'] == 'stopped', cancelled.text
    other = client.post('/api/session', json={'code': code}).json()
    client.headers['Authorization'] = 'Bearer ' + other['token']
    assert client.get('/api/media').json() == shared
    assert client.get('/api/media/shared-sample/preview').content == (base / 'data/sample.mp4').read_bytes()
    assert client.get('/api/broadcasts').json() == []
    assert client.get(f"/api/broadcasts/{job['id']}").status_code == 404
    blocked_stop = client.post(f"/api/broadcasts/{job['id']}/stop")
    assert blocked_stop.status_code == 404, (blocked_stop.status_code, blocked_stop.text)
    result = {'result': 'passed', 'ui_origin': config['ui_origin'], 'api_origin': config['api_origin'],
              'unauthenticated_blocked': True, 'cors_verified': True, 'cross_session_isolation': True, 'stop_verified': True,
              'shared_sample_verified': True,
              'states': states, 'duration': job['progress'], 'output_bytes': len(output.content),
              'youtube_broadcast': False}
    (base / 'docs/public-smoke-result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
