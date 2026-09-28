"""Release sequencing, stale-commit protection and rollback without cloud writes."""
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('production_deploy',
    Path(__file__).resolve().parents[1] / 'scripts/deploy-production.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = 'a' * 40


class Platform:
    def __init__(self):
        self.aliases = {'api': 'dpl_oldapi', 'web': 'dpl_oldweb'}
        self.events = []
        self.stage_failure = None
        self.promote_failure = None
        self.rollback_failure = None

    def current(self, role):
        return self.aliases[role]

    def stage(self, role, *_args):
        self.events.append(('stage', role))
        if role == self.stage_failure:
            raise release.ReleaseError('CANDIDATE_NOT_READY')
        return 'dpl_new' + role

    def promote(self, role, identifier, _directory):
        self.events.append(('promote', role))
        self.aliases[role] = identifier
        if role == self.promote_failure:
            raise release.ReleaseError('LOST_RESPONSE_AFTER_PROMOTION')

    def rollback(self, role, identifier, _directory):
        self.events.append(('rollback', role))
        if role == self.rollback_failure:
            raise release.ReleaseError('ROLLBACK_UNAVAILABLE')
        self.aliases[role] = identifier


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(release, 'assert_current_main', lambda _sha: None)
    monkeypatch.setattr(release, 'smoke', lambda *_args: None)
    return Platform(), tmp_path, {'status': 'preflight'}, []


def publish(runtime):
    platform, directory, report, saved = runtime
    release.publish(platform, directory, SHA, 'snap_test', report,
                    lambda: saved.append(json.loads(json.dumps(report))))


def test_both_candidates_ready_before_aliases_change(runtime):
    publish(runtime)
    platform, _, report, saved = runtime
    assert platform.events == [('stage', 'api'), ('stage', 'web'), ('promote', 'api'), ('promote', 'web')]
    assert report['status'] == 'deployed'
    assert report['previous'] == {'api': 'dpl_oldapi', 'web': 'dpl_oldweb'}
    assert saved[0]['previous'] == report['previous']
    assert saved[-1]['status'] == 'deployed'


@pytest.mark.parametrize('role', ['api', 'web'])
def test_failed_build_never_changes_production(runtime, role):
    runtime[0].stage_failure = role
    with pytest.raises(release.ReleaseError, match='CANDIDATE_NOT_READY'):
        publish(runtime)
    assert runtime[0].aliases == {'api': 'dpl_oldapi', 'web': 'dpl_oldweb'}
    assert not any(event[0] == 'promote' for event in runtime[0].events)


@pytest.mark.parametrize('role', ['api', 'web'])
def test_lost_promotion_response_still_restores_changed_aliases(runtime, role):
    runtime[0].promote_failure = role
    with pytest.raises(release.ReleaseError, match='PRODUCTION_RELEASE_FAILED'):
        publish(runtime)
    assert runtime[2]['status'] == 'rolled_back'
    assert runtime[0].aliases == {'api': 'dpl_oldapi', 'web': 'dpl_oldweb'}


def test_failed_post_deploy_smoke_rolls_back_both(runtime, monkeypatch):
    def fail(*_args):
        raise release.ReleaseError('PRODUCTION_SMOKE_FAILED')
    monkeypatch.setattr(release, 'smoke', fail)
    with pytest.raises(release.ReleaseError):
        publish(runtime)
    assert runtime[2]['rollback_confirmed'] == ['web', 'api']
    assert runtime[2]['status'] == 'rolled_back'


def test_rollback_failure_is_not_reported_as_success(runtime):
    runtime[0].promote_failure = 'web'
    runtime[0].rollback_failure = 'api'
    with pytest.raises(release.ReleaseError):
        publish(runtime)
    assert runtime[2]['status'] == 'manual_recovery_required'
    assert runtime[2]['rollback_confirmed'] == ['web']


def test_concurrent_release_is_not_overwritten_during_rollback(runtime, monkeypatch):
    def fail(*_args):
        runtime[0].aliases['web'] = 'dpl_otherrelease'
        raise release.ReleaseError('PRODUCTION_SMOKE_FAILED')
    monkeypatch.setattr(release, 'smoke', fail)
    with pytest.raises(release.ReleaseError):
        publish(runtime)
    assert runtime[0].aliases['web'] == 'dpl_otherrelease'
    assert runtime[2]['status'] == 'manual_recovery_required'


def test_newer_main_commit_during_build_prevents_promotion(runtime, monkeypatch):
    def stale(_sha):
        raise release.ReleaseError('SUPERSEDED_MAIN_COMMIT')
    monkeypatch.setattr(release, 'assert_current_main', stale)
    with pytest.raises(release.ReleaseError, match='SUPERSEDED_MAIN_COMMIT'):
        publish(runtime)
    assert runtime[0].events == [('stage', 'api'), ('stage', 'web')]


@pytest.mark.parametrize(('ref', 'event', 'head', 'remote', 'expected'), [
    ('refs/heads/develop', 'push', SHA, SHA, 'MAIN_COMMIT_REQUIRED'),
    ('refs/heads/main', 'pull_request', SHA, SHA, 'MAIN_COMMIT_REQUIRED'),
    ('refs/heads/main', 'push', 'b' * 40, SHA, 'MAIN_COMMIT_REQUIRED'),
    ('refs/heads/main', 'push', SHA, 'b' * 40, 'SUPERSEDED_MAIN_COMMIT'),
])
def test_unsafe_or_stale_trigger_is_rejected(monkeypatch, ref, event, head, remote, expected):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'hhj4861/replay-live')
    monkeypatch.setenv('GITHUB_REF', ref)
    monkeypatch.setenv('GITHUB_EVENT_NAME', event)
    monkeypatch.setattr(release, 'run', lambda args: head if 'rev-parse' in args else remote + '\trefs/heads/main')
    with pytest.raises(release.ReleaseError, match=expected):
        release.assert_current_main(SHA)


def test_source_package_excludes_untracked_secrets_and_adds_public_identity(tmp_path, monkeypatch):
    root, output = tmp_path / 'repo', tmp_path / 'package'
    tracked = ['web/package.json', 'web/public/icon.svg', 'server/worker.py', 'main.py', 'requirements.txt']
    for name in [*tracked, 'web/.env.local', 'server/untracked-secret.txt',
                 'deploy/commercial/api.vercel.json', 'deploy/commercial/web.free.vercel.json']:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('{}')
    monkeypatch.setattr(release, 'ROOT', root)
    monkeypatch.setattr(release, 'run', lambda _args: '\0'.join(tracked))
    release.prepare_sources(output, SHA, 'snap_test')
    assert not (output / 'web/.env.local').exists()
    assert not (output / 'api/server/untracked-secret.txt').exists()
    assert (output / 'api/server/worker.py').is_file()
    assert json.loads((output / 'web/public/_replay-release.json').read_text()) == {
        'commit': SHA, 'snapshot_id': 'snap_test'}


def test_missing_token_stops_before_cloud_build(monkeypatch):
    monkeypatch.delenv('VERCEL_TOKEN', raising=False)
    with pytest.raises(release.ReleaseError, match='VERCEL_TOKEN_MISSING'):
        release.Vercel()


def test_cli_uses_environment_secret_and_never_prints_output(monkeypatch, tmp_path):
    monkeypatch.setenv('VERCEL_TOKEN', 'test-secret-must-not-appear-in-args')
    captured = []
    def command(args, **kwargs):
        captured.append((args, kwargs))
        return 'https://candidate.vercel.app'
    monkeypatch.setattr(release, 'run', command)
    platform = release.Vercel()
    monkeypatch.setattr(platform, 'api', lambda _path: {
        'readyState': 'READY', 'projectId': release.PROJECTS['web'][0], 'id': 'dpl_test'})
    assert platform.stage('web', tmp_path, 'rpl-test', 'snap_test', SHA) == 'dpl_test'
    args, kwargs = captured[0]
    assert 'test-secret-must-not-appear-in-args' not in ' '.join(args)
    assert kwargs['env']['VERCEL_TOKEN'] == 'test-secret-must-not-appear-in-args'
    assert '--skip-domain' in args and 'REPLAY_WORKER_SNAPSHOT_ID=snap_test' in args


def test_smoke_rejects_stale_web_even_when_api_is_current(monkeypatch):
    monkeypatch.setattr(release, 'public_json', lambda url: (
        {'status': 'alive', 'version': 'rpl-test'} if '/api/live' in url else {'commit': 'old'}))
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self, _size): return b'<script src="/assets/index.js"></script>'
    monkeypatch.setattr(release.urllib.request, 'urlopen', lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(release.time, 'sleep', lambda _seconds: None)
    with pytest.raises(release.ReleaseError, match='PRODUCTION_SMOKE_FAILED'):
        release.smoke(SHA, 'snap_test', 'rpl-test')

