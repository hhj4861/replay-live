"""Generated MP4 through daemon -> normal tenant upload -> real FFmpeg validation.

The source downloader is a deterministic fixture. No live platform is contacted.
"""
import hashlib
import time
import uuid

from fastapi.testclient import TestClient
from server.local_import_daemon import LocalImports, create_local_import_app
from test_commercial_api import commercial, run_claimed
from test_poc import clip


def test_daemon_result_uses_existing_tenant_upload_and_validation(commercial, clip, tmp_path):
    cloud, repo, _ = commercial
    original = clip.read_bytes()
    digest = hashlib.sha256(original).hexdigest()

    def source_download(source, output, **limits):
        assert source == {'provider': 'direct', 'url': 'https://media.example/owned.mp4'}
        assert limits['max_bytes'] >= len(original)
        output.write_bytes(original)
        return {'bytes': len(original), 'sha256': digest}

    manager = LocalImports(root=tmp_path, downloader=source_download, pairing_code='TEST-CODE')
    with TestClient(create_local_import_app(manager=manager), base_url='http://127.0.0.1:17833',
                    headers={'Origin': 'https://replay-live-poc.vercel.app', 'X-Replay-Local': '1'}) as local:
        pairing = local.post('/pair', json={'code': 'TEST-CODE'}).json()
        capability = {'Authorization': 'Bearer ' + pairing['token']}
        created = local.post('/imports', headers=capability, json={'request_id': str(uuid.uuid4()),
            'provider': 'direct', 'url': 'https://media.example/owned.mp4',
            'max_bytes': 50 * 1024**2, 'max_duration': 120})
        assert created.status_code == 202
        id = created.json()['id']
        for _ in range(100):
            status = local.get('/imports/' + id, headers=capability).json()
            if status['state'] == 'ready':
                break
            time.sleep(.01)
        assert status['state'] == 'ready'
        received = local.get('/imports/' + id + '/file', headers=capability).content
        assert hashlib.sha256(received).hexdigest() == status['sha256'] == digest
        assert local.delete('/imports/' + id, headers=capability).status_code == 204
        assert not list(manager.root.iterdir())

        # Browser's existing authenticated cloud API owns tenant/quota selection.
        intent_response = cloud.post('/api/uploads', json={'name': '도우미 영상.mp4',
            'bytes': len(received), 'sha256': digest})
        assert intent_response.status_code == 201, intent_response.text
        intent = intent_response.json()
        signed = intent['upload']
        assert cloud.request(signed['method'], signed['url'], headers=signed['headers'], content=received).status_code in {200, 201, 204}
        media_id = intent['media']['id']
        assert cloud.post(f'/api/uploads/{media_id}/complete').status_code == 202
        assert cloud.post(f'/api/uploads/{media_id}/complete', headers={'Authorization': 'Bearer beta'}).status_code == 404
        claimed = run_claimed(cloud)
        assert claimed['target'] == 'validate'
        assert not any(item['target'] == 'import' for item in repo.list_jobs('alpha'))
        ready = cloud.get('/api/media').json()[0]
        assert ready['id'] == media_id and ready['status'] == 'ready'
        assert ready['bytes'] == len(original) and ready['duration'] > 0
        assert cloud.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []
        preview = cloud.get(f'/api/media/{media_id}/preview').json()
        assert cloud.get(preview['url']).content == original
