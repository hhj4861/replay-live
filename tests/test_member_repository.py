"""Account suspension shares the existing cancellation/admission transaction."""
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from server.repository import Repository, jobs, runtime_records


@pytest.fixture
def repo(tmp_path):
    value = Repository(f'sqlite:///{tmp_path}/members.db', create_schema=True,
                       tenant_concurrency=4, global_concurrency=8)
    yield value
    value.close()


def media(repo, tenant='member'):
    return repo.add_media(tenant, name='synthetic.mp4', object_key=f'replay/{tenant}/sample.mp4',
                          bytes=1000, duration=15, width=320, height=180, fps=30)


def job(repo, item, key, target='local'):
    return repo.create_job(item['tenant_id'], media_id=item['id'], title='Synthetic suspension test',
                          target=target, idempotency_key=key,
                          secret_ciphertext='synthetic-ciphertext' if target == 'twitch' else None)


def test_suspension_cancels_only_member_and_keeps_active_cleanup_lease(repo):
    item = media(repo)
    active = job(repo, item, 'active', 'twitch')
    claimed = repo.claim('synthetic-worker', 60)
    queued = job(repo, item, 'queued')
    other = job(repo, media(repo, 'other-member'), 'unaffected')
    with repo._transaction() as connection:
        repo._gate(connection)
        assert repo.cancel_member_jobs(connection, 'member', repo._now(connection)) == 2
    assert repo.get_job('member', queued['id'])['state'] == 'stopped'
    assert repo.get_job('other-member', other['id'])['state'] == other['state']
    with repo.engine.connect() as connection:
        rows = {row['id']: row for row in connection.execute(select(jobs)).mappings()}
        assert rows[queued['id']]['output_reserved_bytes'] == 0
        assert rows[queued['id']]['secret_ciphertext'] is None
        assert rows[active['id']]['state'] == 'stopping'
        assert rows[active['id']]['lease_token'] == claimed['lease_token']
        runtime = connection.execute(select(runtime_records)).mappings().one()
        assert runtime['state'] == 'stopping' and not runtime['cleaned']
    assert repo.heartbeat(active['id'], claimed['lease_token'])['cancel_requested'] is True
    finished = repo.finish(active['id'], claimed['lease_token'], state='completed', progress=15)
    assert finished['state'] == 'stopped'
    with repo.engine.connect() as connection:
        assert connection.execute(select(jobs.c.secret_ciphertext).where(jobs.c.id == active['id'])).scalar_one() is None


def test_suspension_cancellation_rolls_back_with_member_transaction(repo):
    created = job(repo, media(repo), 'queued')
    before = repo.get_job('member', created['id'])
    with pytest.raises(RuntimeError, match='audit unavailable'):
        with repo._transaction() as connection:
            repo._gate(connection)
            repo.cancel_member_jobs(connection, 'member', repo._now(connection))
            raise RuntimeError('audit unavailable')
    assert repo.get_job('member', created['id']) == before


def test_suspension_cancels_import_and_releases_held_storage(repo):
    imported = repo.create_import('member', name='synthetic source', object_key='replay/member/import.mp4',
        secret_ciphertext='synthetic-ciphertext', idempotency_key='import',
        request_fingerprint='a' * 64, max_bytes=1024)
    with repo._transaction() as connection:
        repo._gate(connection)
        repo.cancel_member_jobs(connection, 'member', repo._now(connection))
    assert repo.get_media('member', imported['media']['id'])['status'] == 'failed'
    assert repo.usage('member')['storage_reserved_bytes'] == 0
    assert repo.claim('synthetic-worker') is None


def test_suspended_admission_rejects_all_new_work_and_idempotent_replays(repo):
    item = media(repo)
    job(repo, item, 'existing')
    batch_args = dict(media_id=item['id'], title='Synthetic batch', destinations=[{'target': 'twitch',
                      'secret_ciphertext': 'synthetic-ciphertext'}], idempotency_key='batch')
    repo.create_jobs_batch('member', **batch_args)
    import_args = dict(name='synthetic import', object_key='replay/member/import.mp4',
        secret_ciphertext='synthetic-ciphertext', idempotency_key='import',
        request_fingerprint='b' * 64, max_bytes=1024)
    repo.create_import('member', **import_args)
    before = repo.usage('member')

    def suspended(connection, tenant_id):
        assert connection.in_transaction() and tenant_id == 'member'
        raise HTTPException(403, 'Account suspended')

    repo.admission_guard = suspended
    for action in (lambda: job(repo, item, 'existing'), lambda: job(repo, item, 'new'),
                   lambda: repo.create_jobs_batch('member', **batch_args),
                   lambda: repo.create_import('member', **import_args), lambda: media(repo)):
        with pytest.raises(HTTPException) as error:
            action()
        assert error.value.status_code == 403
    assert repo.usage('member') == before
