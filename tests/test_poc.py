import json
from pathlib import Path
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from server.app import create_app
from server.service import Service

HEADERS = {'X-Replay-Client': '1'}


@pytest.fixture(scope='session')
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp('clips') / 'test.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x180:rate=30', '-f', 'lavfi', '-i', 'sine=frequency=440',
                    '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(path)], check=True)
    return path


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'data')
    with TestClient(app, headers=HEADERS) as c:
        yield c


def upload(client, clip):
    response = client.post('/api/media', files={'file': ('test.mp4', clip.read_bytes(), 'video/mp4')})
    assert response.status_code == 201, response.text
    return response.json()


def until(client, job_id, states, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = client.get(f'/api/broadcasts/{job_id}').json()
        if job['state'] in states:
            return job
        time.sleep(.15)
    raise AssertionError(f'Expected {states}, got {job}')


def test_real_upload_stream_download(client, clip, tmp_path):
    media = upload(client, clip)
    assert media['duration'] >= 3 and media['width'] == 320
    assert 'path' not in media
    response = client.post('/api/broadcasts', json={'media_id': media['id'], 'title': '송출 E2E', 'target': 'local'})
    assert response.status_code == 201, response.text
    job = until(client, response.json()['id'], {'completed', 'failed'})
    assert job['state'] == 'completed', job
    assert job['progress'] == job['duration'] and job['attempt'] == 1
    result = client.get(f"/api/broadcasts/{job['id']}/output")
    assert result.status_code == 200 and result.content[:3] == b'FLV'
    output = tmp_path / 'actual.flv'
    output.write_bytes(result.content)
    data = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(output)]))
    assert float(data['format']['duration']) >= 2.8
    assert {s['codec_name'] for s in data['streams']} == {'h264', 'aac'}
    assert client.get(f"/api/broadcasts/{job['id']}/events").json()


def test_schedule_cancel_and_single_worker(client, clip):
    media = upload(client, clip)
    from datetime import datetime, timezone, timedelta
    schedule = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    payload = {'media_id': media['id'], 'title': '예약 송출', 'scheduled_at': schedule}
    first = client.post('/api/broadcasts', json=payload).json()
    second = client.post('/api/broadcasts', json=payload).json()
    time.sleep(.5)
    assert client.get(f"/api/broadcasts/{first['id']}").json()['state'] == 'scheduled'
    until(client, first['id'], {'starting', 'streaming'})
    assert client.get(f"/api/broadcasts/{second['id']}").json()['state'] == 'scheduled'
    client.post(f"/api/broadcasts/{second['id']}/stop")
    assert until(client, second['id'], {'stopped'})['attempt'] == 0
    client.post(f"/api/broadcasts/{first['id']}/stop")
    assert until(client, first['id'], {'stopped'})['state'] == 'stopped'


def test_retries_on_real_ffmpeg_failure(client, clip):
    media = upload(client, clip)
    service = client.app.state.service
    service.retry_delay = .05
    # Force an actual FFmpeg output failure; no external network destination is contacted.
    original = service.command
    service.command = lambda path, target, offset: original(path, str(service.root / 'missing' / 'out.flv'), offset)
    job = client.post('/api/broadcasts', json={'media_id': media['id'], 'title': '실패 테스트'}).json()
    done = until(client, job['id'], {'failed'})
    assert done['attempt'] == 3
    assert len([e for e in client.get(f"/api/broadcasts/{job['id']}/events").json() if '재시도' in e['message']]) == 2


def test_validation_and_secret_encryption(client, clip):
    assert client.post('/api/media', files={'file': ('bad.mp4', b'not a video')}).status_code == 400
    assert client.post('/api/media', files={'file': ('bad.mov', b'not a video')}).status_code == 400
    assert client.post('/api/broadcasts', json={'media_id': 'missing', 'title': 'test'}).status_code == 400
    media = upload(client, clip)
    from datetime import datetime, timezone, timedelta
    payload = {'media_id': media['id'], 'title': '키 보안', 'target': 'youtube', 'stream_key': 'test-private-key-1234567890',
               'scheduled_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
    response = client.post('/api/broadcasts', json=payload)
    assert response.status_code == 201
    job_id = response.json()['id']
    assert payload['stream_key'] not in response.text
    with client.app.state.service.db() as db:
        stored = db.execute('SELECT secret FROM jobs WHERE id=?', (job_id,)).fetchone()['secret']
    assert stored != payload['stream_key']
    assert client.app.state.service.cipher.decrypt(stored.encode()).decode() == payload['stream_key']
    assert payload['stream_key'] not in client.get('/api/broadcasts').text
    payload['target'] = 'invalid'
    rejected = client.post('/api/broadcasts', json=payload)
    assert rejected.status_code == 422 and payload['stream_key'] not in rejected.text
    assert client.post('/api/broadcasts', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/health', headers={'Host': 'evil.example'}).status_code == 400
    client.post(f'/api/broadcasts/{job_id}/stop')


def test_restart_retains_queue_and_prevents_duplicate_instance(tmp_path, clip):
    root = tmp_path / 'restart'
    first = Service(root)
    media = first.add_media(clip, 'test.mp4')
    job = first.create_job(media['id'], '예약 보존', 'local', scheduled=time.time() + 3600)
    with pytest.raises(RuntimeError):
        Service(root)
    first.close()
    second = Service(root)
    assert second.get_job(job['id'])['state'] == 'scheduled'
    with second.db() as db:
        db.execute("UPDATE jobs SET state='streaming' WHERE id=?", (job['id'],))
    second.close()
    third = Service(root)
    assert third.get_job(job['id'])['state'] == 'failed'
    assert '재시작' in third.get_job(job['id'])['error']
    third.close()
