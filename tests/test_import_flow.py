"""Import queue -> real validation -> object -> existing file broadcast integration.

Only the public source transport is replaced by a generated local MP4 fixture.
No external platform is contacted or streamed to by this test.
"""
import hashlib

from test_commercial_api import commercial, run_claimed
from test_poc import clip
import server.worker as worker


def test_imported_recording_can_be_reused_for_existing_broadcast(commercial, clip, monkeypatch):
    client, repo, _ = commercial
    content = clip.read_bytes()
    def source_download(source, output, **limits):
        assert source['provider'] == 'direct'
        limits['check_active']()
        assert len(content) <= limits['max_bytes']
        output.write_bytes(content)
        return {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'name': 'import.mp4'}
    monkeypatch.setattr(worker, 'download_source', source_download)
    request = {'provider': 'direct', 'url': 'https://media.example/own.mp4?signature=synthetic', 'name': '내 녹화본.mp4'}
    created = client.post('/api/media/imports', json=request, headers={'Idempotency-Key': 'source-flow-1'})
    assert created.status_code == 202, created.text
    item = created.json()['media']
    assert item['status'] == 'importing'
    assert 'synthetic' not in created.text
    replay = client.post('/api/media/imports', json=request, headers={'Idempotency-Key': 'source-flow-1'})
    assert replay.status_code == 202 and replay.json()['media']['id'] == item['id']
    assert len(repo.list_jobs('alpha')) == 1
    assert client.post('/api/broadcasts', json={'media_id': item['id'], 'title': '아직 준비 중', 'target': 'local'},
                       headers={'Idempotency-Key': 'source-not-ready'}).status_code == 409
    claimed = run_claimed(client)
    assert claimed['target'] == 'import' and claimed['input'] is None
    ready = client.get('/api/media').json()[0]
    assert ready['id'] == item['id'] and ready['status'] == 'ready' and ready['bytes'] == len(content)
    assert client.get('/api/broadcasts').json() == []
    assert client.get('/api/media', headers={'Authorization': 'Bearer beta'}).json() == []
    usage = client.get('/api/usage').json()
    assert usage['storage_bytes'] == len(content) and usage['storage_reserved_bytes'] == 0
    signed = client.get('/api/media/' + item['id'] + '/preview').json()
    assert client.get(signed['url']).content == content
    broadcast = client.post('/api/broadcasts', json={'media_id': item['id'], 'title': '가져온 내 영상 재송출', 'target': 'local'},
                            headers={'Idempotency-Key': 'import-broadcast-1'})
    assert broadcast.status_code == 201, broadcast.text
    run_claimed(client)
    job = client.get('/api/broadcasts/' + broadcast.json()['id']).json()
    assert job['state'] == 'completed'
    result = client.get('/api/broadcasts/' + job['id'] + '/output').json()
    output = client.get(result['url']).content
    assert output.startswith(b'FLV') and len(output) > 0
    assert repo.get_media('alpha', item['id'])['status'] == 'ready'
