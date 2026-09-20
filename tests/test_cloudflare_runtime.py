import threading
import time
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

from server.cloudflare_media import create_media_app
from server.worker import run_job
from test_commercial_api import commercial, prepare
from test_poc import clip


def test_cloudflare_job_endpoint_runs_real_validation_once(commercial, clip, monkeypatch):
    cloud, repo, _ = commercial
    prepared = prepare(cloud, clip)
    job = cloud.post('/internal/claim', headers={'Authorization': 'Bearer ' + 'c' * 32},
                     json={'worker_id': 'cloudflare-test', 'version': 'integration-test'}).json()['job']
    job['mode'] = 'production'
    job['callback_base'] = job['callback_base'].replace('http://', 'https://')
    job['input']['url'] = job['input']['url'].replace('http://', 'https://')
    job['deadline'] = min(job['deadline'], time.time() + 300)
    monkeypatch.setenv('REPLAY_VERSION', 'integration-test')
    finished = threading.Event()
    results = []
    def transport(request):
        u = urlsplit(str(request.url))
        response = cloud.request(request.method, u.path + ('?' + u.query if u.query else ''),
                                 headers=dict(request.headers), content=request.read())
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    def runner(value):
        try:
            with httpx.Client(transport=httpx.MockTransport(transport)) as http:
                results.append(run_job(value, client=http))
        finally: finished.set()
    with TestClient(create_media_app(runner=runner)) as media:
        assert media.post('/run', json=job).status_code == 202
        assert media.post('/run', json=job).status_code == 409
        assert finished.wait(30)
    assert results[0]['state'] == 'completed'
    assert repo.get_media('alpha', prepared['media']['id'])['status'] == 'ready'


def test_cloudflare_worker_refuses_wrong_release_expired_job_and_cloud_download(monkeypatch):
    monkeypatch.setenv('REPLAY_VERSION', 'release-a')
    ran = []
    with TestClient(create_media_app(runner=ran.append)) as client:
        base = {'version': 'release-a', 'mode': 'production', 'target': 'validate', 'deadline': time.time() + 60}
        for change in [{'version': 'release-b'}, {'mode': 'development'}, {'target': 'import'}, {'deadline': 1}]:
            assert client.post('/run', json=base | change).status_code == 400
        assert client.post('/run', content=b'x' * 50000).status_code == 413
    assert ran == []
