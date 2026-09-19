"""Free hosting configuration and durable wake deadlines; no live credentials."""
import json

import httpx
import pytest

from server.dispatch_wakeup import DispatchWakeup
from server.repository import Repository
from server.settings import Settings


def settings(**changes):
    values = dict(mode='production', database_url='postgresql+psycopg://synthetic.invalid/replay',
                  public_url='https://api.synthetic.invalid', origins=('https://web.synthetic.invalid',),
                  control_token='control-synthetic-' + 'c' * 32, callback_key='callback-synthetic-' + 'k' * 32,
                  version='test-release', storage_provider='vercel-blob', secret_provider='env-aesgcm',
                  blob_control_url='https://web.synthetic.invalid/api/blob-control', max_upload_bytes=50 * 1024**2,
                  max_output_bytes=128 * 1024**2, dispatch_mode='queue',
                  dispatch_wakeup_url='https://web.synthetic.invalid/api/wake')
    return Settings(**(values | changes))


def test_explicit_production_providers_do_not_need_aws():
    cfg = settings()
    assert cfg.mode == 'production' and not cfg.bucket and not cfg.kms_key_id
    assert cfg.control_token not in repr(cfg)


@pytest.mark.parametrize('changes', [
    {'storage_provider': 'local'}, {'secret_provider': 'local'},
    {'storage_provider': 's3'}, {'secret_provider': 'aws-kms'},
    {'database_url': 'sqlite:///:memory:'}, {'public_url': 'http://api.synthetic.invalid'},
    {'blob_control_url': 'https://user:secret@web.synthetic.invalid/api/blob-control'},
    {'blob_control_url': 'https://web.synthetic.invalid/api/blob-control?secret=canary'},
    {'max_upload_bytes': 50 * 1024**2 + 1}, {'max_output_bytes': 128 * 1024**2 + 1},
    {'dispatch_mode': 'unsupported'}, {'dispatch_wakeup_url': 'http://web.synthetic.invalid/api/wake'},
])
def test_production_provider_guards(changes):
    with pytest.raises(ValueError):
        settings(**changes)


def test_queue_notification_is_metadata_only_and_health_throttled():
    cfg = settings()
    requests = []
    clock = [100.0]

    def handler(request):
        requests.append(request)
        assert request.url == cfg.dispatch_wakeup_url
        assert request.headers['authorization'] == 'Bearer ' + cfg.control_token
        assert json.loads(request.content) == {'v': 1}
        return httpx.Response(202, json={'queued': True})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        wake = DispatchWakeup(cfg, client=client, clock=lambda: clock[0])
        assert wake.notify(health=True) and wake.notify(health=True)
        assert len(requests) == 1
        assert wake.notify()  # A real job must bypass health throttling.
        clock[0] += 31
        assert wake.notify(health=True)
        assert len(requests) == 3
        wake.close()
        assert not client.is_closed


@pytest.mark.parametrize('status', [301, 401, 429, 500])
def test_queue_failures_are_not_accepted(status):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        assert DispatchWakeup(settings(), client=client).notify() is False


def test_delayed_deletion_remains_a_durable_wakeup(tmp_path):
    clock = [1_000_000.0]
    repo = Repository(f'sqlite:///{tmp_path}/wake.sqlite3', clock=lambda: clock[0], create_schema=True)
    try:
        assert repo.next_wakeup() is None
        item = repo.add_media('a', id='recording', name='sample.mp4', object_key='replay/a/media/recording.mp4',
                             bytes=1000, duration=17, width=360, height=640, fps=30)
        repo.delete_media('a', item['id'])
        assert repo.pending_deletions() == []
        assert repo.next_wakeup() == clock[0] + 900
        clock[0] += 901
        assert repo.next_wakeup() < clock[0]
        deletion = repo.pending_deletions()[0]
        repo.deletion_failed(deletion['id'])
        assert repo.pending_deletions() == []
        assert repo.next_wakeup() == clock[0] + 60
        clock[0] += 61
        repo.confirm_deletion(deletion['id'])
        assert repo.next_wakeup() is None
    finally:
        repo.close()


def test_scheduled_and_active_job_keep_a_wakeup(tmp_path):
    clock = [1_000_000.0]
    repo = Repository(f'sqlite:///{tmp_path}/job.sqlite3', clock=lambda: clock[0], create_schema=True)
    try:
        item = repo.add_media('a', id='recording', name='sample.mp4', object_key='replay/a/media/recording.mp4',
                             bytes=1000, duration=17, width=360, height=640, fps=30)
        repo.create_job('a', media_id=item['id'], title='synthetic', target='youtube', scheduled=clock[0] + 600,
                        idempotency_key='schedule-1', secret_ciphertext='synthetic')
        assert repo.next_wakeup() == clock[0] + 600
        clock[0] += 601
        job = repo.claim('worker', lease_seconds=180)
        assert job is not None
        assert repo.next_wakeup() == clock[0] + 180
    finally:
        repo.close()
