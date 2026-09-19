from datetime import datetime, timezone
from pathlib import Path
import runpy
import time

from fastapi.testclient import TestClient

from server.app import create_app
from server.service import Service
from test_poc import clip

cloud_start = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/cloud-start.py'))
fail_previous_session_queue = cloud_start['fail_previous_session_queue']


def test_cloud_host_and_execution_window(tmp_path, clip):
    deadline = tmp_path / 'deadline'
    deadline.write_text(str(time.time() + 600))
    origin = 'https://replay.example'
    app = create_app(tmp_path / 'cloud', public_origins=[origin], invite_code='cloud-test-code',
                     allowed_hosts=['testserver', 'cloud.example'], deadline_file=deadline)
    with TestClient(app, headers={'Origin': origin, 'X-Replay-Client': '1'}) as client:
        response = client.post('/api/session', json={'code': 'cloud-test-code'})
        client.headers['Authorization'] = 'Bearer ' + response.json()['token']
        assert client.get('/api/health', headers={'Host': 'cloud.example'}).status_code == 200
        assert client.get('/api/health', headers={'Host': 'unexpected.example'}).status_code == 400
        assert client.get('/api/health').json()['server_expires_at'] == float(deadline.read_text())
        media = client.post('/api/media', files={'file': ('clip.mp4', clip.read_bytes())}).json()
        payload = {'media_id': media['id'], 'title': 'Cloud reservation',
                   'scheduled_at': datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat()}
        response = client.post('/api/broadcasts', json=payload)
        assert response.status_code == 201
        client.post(f"/api/broadcasts/{response.json()['id']}/stop")
        payload['scheduled_at'] = datetime.fromtimestamp(time.time() + 500, timezone.utc).isoformat()
        assert client.post('/api/broadcasts', json=payload).status_code == 400
        deadline.write_text(str(time.time() + 120))
        payload.pop('scheduled_at')
        assert client.post('/api/broadcasts', json=payload).status_code == 400
        deadline.unlink()
        assert client.post('/api/broadcasts', json=payload).status_code == 503


def test_resumed_cloud_session_does_not_launch_previous_queue(tmp_path, clip):
    data = tmp_path / 'cloud'
    now = time.time()
    roots = [data / ('a' * 64), data / ('b' * 64)]
    all_jobs = []
    for root in roots:
        service = Service(root)
        try:
            media = service.add_media(clip, 'clip.mp4')
            jobs = [service.create_job(media['id'], state, 'local', scheduled=now + 60)
                    for state in ('scheduled', 'retry_wait', 'completed', 'stopped', 'streaming')]
            with service.db() as db:
                for job, state in zip(jobs, ('scheduled', 'retry_wait', 'completed', 'stopped', 'streaming')):
                    db.execute('UPDATE jobs SET state=? WHERE id=?', (state, job['id']))
            all_jobs.append(jobs)
        finally:
            service.close()
    deadline_path = data / 'cloud-deadline'
    deadline_path.write_text(str(now - 60))
    assert fail_previous_session_queue(data, now + 2700, backend_ready=False) == 4
    # Repeating startup cleanup must not add duplicate events.
    assert fail_previous_session_queue(data, now + 2700, backend_ready=False) == 0
    assert float(deadline_path.read_text()) == now - 60
    for root, jobs in zip(roots, all_jobs):
        service = Service(root)
        try:
            for job in jobs[:2]:
                result = service.get_job(job['id'])
                assert result['state'] == 'failed' and result['attempt'] == 0
                assert '다시 등록' in result['error']
                assert sum('다시 등록' in event['message'] for event in service.events(job['id'])) == 1
            assert service.get_job(jobs[2]['id'])['state'] == 'completed'
            assert service.get_job(jobs[3]['id'])['state'] == 'stopped'
            assert '서버가 재시작' in service.get_job(jobs[4]['id'])['error']
            service.tick()
            assert not service.processes
        finally:
            service.close()


def test_cloud_queue_cleanup_preserves_initial_and_same_session(tmp_path, clip):
    data = tmp_path / 'cloud'
    service = Service(data / ('c' * 64))
    try:
        media = service.add_media(clip, 'clip.mp4')
        job = service.create_job(media['id'], 'Keep reservation', 'local', scheduled=time.time() + 60)
        deadline = time.time() + 2700
        assert fail_previous_session_queue(data, deadline, backend_ready=False) == 0
        (data / 'cloud-deadline').write_text(str(deadline))
        assert fail_previous_session_queue(data, deadline + 0.5, backend_ready=False) == 0
        assert fail_previous_session_queue(data, deadline + 3600, backend_ready=True) == 0
        assert service.get_job(job['id'])['state'] == 'scheduled'
        assert len(service.events(job['id'])) == 1
    finally:
        service.close()
