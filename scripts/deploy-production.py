"""Deploy a verified deploy/replay commit without exporting production secrets.

Remote Vercel builds use project-managed secrets. Only release identifiers are
overridden. Candidates are built before either production alias is promoted.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
TEAM = 'team_RUHKd04LgBmYVYUwwRTJlW8b'
PROJECTS = {
    'api': ('prj_51pf6Z4DEzAHsgeFdP9f3NKdlmnw', 'replay-live-api.vercel.app'),
    'web': ('prj_I5pZ7r5Dnx9VUNm5d7qghhTIvnzK', 'replay-live-poc.vercel.app'),
}


class ReleaseError(RuntimeError):
    pass


def run(args, *, cwd=ROOT, env=None, timeout=900):
    # CLI errors can contain signed URLs; never relay their raw output.
    try:
        result = subprocess.run(args, cwd=cwd, env=env, text=True,
                                capture_output=True, timeout=timeout, check=True)
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError('COMMAND_FAILED') from None


def assert_current_release(sha):
    if (os.environ.get('GITHUB_REPOSITORY') != 'hhj4861/replay-live'
            or os.environ.get('GITHUB_REF') != 'refs/heads/deploy/replay'
            or os.environ.get('GITHUB_REF_PROTECTED') != 'true'
            or os.environ.get('GITHUB_EVENT_NAME') not in {'push', 'workflow_dispatch'}
            or not re.fullmatch(r'[0-9a-f]{40}', sha)
            or run(['git', 'rev-parse', 'HEAD']) != sha):
        raise ReleaseError('PROTECTED_RELEASE_COMMIT_REQUIRED')
    remote = run(['git', 'ls-remote', 'origin', 'refs/heads/deploy/replay']).split()
    if remote != [sha, 'refs/heads/deploy/replay']:
        raise ReleaseError('SUPERSEDED_RELEASE_COMMIT')


def prepare_sources(destination, sha, snapshot):
    """Copy tracked source only; local credentials/build caches cannot be uploaded."""
    files = run(['git', 'ls-files', '-z']).split('\0')
    for relative in files:
        if not relative:
            continue
        src = ROOT / relative
        if relative.startswith('web/'):
            target = destination / relative
        elif relative in {'main.py', 'requirements.txt', 'requirements.lock'} or relative.startswith(('server/', 'migrations/')):
            target = destination / 'api' / relative
        else:
            continue
        if src.is_symlink() or any(part.startswith('.env') or part in {'.vercel', 'node_modules'}
                                   for part in Path(relative).parts):
            raise ReleaseError('UNSAFE_RELEASE_SOURCE')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    for role, (project, _) in PROJECTS.items():
        directory = destination / role
        (directory / '.vercel').mkdir(parents=True)
        (directory / '.vercel/project.json').write_text(json.dumps({'projectId': project, 'orgId': TEAM}))
        config = 'api.vercel.json' if role == 'api' else 'web.free.vercel.json'
        shutil.copyfile(ROOT / 'deploy/commercial' / config, directory / 'vercel.json')
    public = destination / 'web/public'
    public.mkdir(exist_ok=True)
    (public / '_replay-release.json').write_text(json.dumps({'commit': sha, 'snapshot_id': snapshot}))


class Vercel:
    def __init__(self):
        self.token = os.environ.get('VERCEL_TOKEN')
        if not self.token:
            raise ReleaseError('VERCEL_TOKEN_MISSING')

    def api(self, path):
        request = urllib.request.Request('https://api.vercel.com' + path + '?teamId=' + TEAM,
                                         headers={'Authorization': 'Bearer ' + self.token})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except Exception:
            raise ReleaseError('VERCEL_READ_FAILED') from None

    def current(self, role):
        project, host = PROJECTS[role]
        data = self.api('/v4/aliases/' + host)
        identifier = data.get('deploymentId')
        if data.get('projectId') != project or not re.fullmatch(r'dpl_[A-Za-z0-9]+', identifier or ''):
            raise ReleaseError('PRODUCTION_ALIAS_MISMATCH')
        return identifier

    def cli(self, args, directory, role):
        env = {**os.environ, 'VERCEL_ORG_ID': TEAM, 'VERCEL_PROJECT_ID': PROJECTS[role][0],
               'VERCEL_TELEMETRY_DISABLED': '1', 'NO_COLOR': '1'}
        return run(['vercel', *args, '--scope', 'dean-10'], cwd=directory, env=env)

    def stage(self, role, directory, version, snapshot, sha):
        args = ['deploy', '--prod', '--skip-domain', '--yes', '--archive=tgz',
                '--env', 'REPLAY_VERSION=' + version, '--meta', 'replayCommit=' + sha]
        if role == 'web':
            args += ['--env', 'REPLAY_WORKER_SNAPSHOT_ID=' + snapshot,
                     '--env', 'REPLAY_COMMERCIAL=1', '--build-env', 'REPLAY_COMMERCIAL=1']
        output = self.cli(args, directory, role)
        matches = re.findall(r'^https://([a-zA-Z0-9-]+\.vercel\.app)$', output, re.MULTILINE)
        if len(matches) != 1:
            raise ReleaseError('DEPLOYMENT_URL_MISSING')
        data = self.api('/v13/deployments/' + matches[0])
        if (data.get('readyState') != 'READY' or data.get('projectId') != PROJECTS[role][0]
                or not re.fullmatch(r'dpl_[A-Za-z0-9]+', data.get('id', ''))):
            raise ReleaseError('CANDIDATE_NOT_READY')
        return data['id']

    def promote(self, role, identifier, directory):
        self.cli(['promote', identifier, '--yes'], directory, role)

    def rollback(self, role, identifier, directory):
        self.cli(['rollback', identifier, '--yes'], directory, role)


def public_json(url):
    request = urllib.request.Request(url, headers={'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise ReleaseError('PUBLIC_SMOKE_FAILED')
        return json.load(response)


def smoke(sha, snapshot, version):
    for attempt in range(12):
        try:
            api = public_json('https://' + PROJECTS['api'][1] + '/api/live')
            web = public_json('https://' + PROJECTS['web'][1] + '/_replay-release.json?commit=' + sha)
            with urllib.request.urlopen('https://' + PROJECTS['web'][1], timeout=15) as response:
                html = response.read(1024 * 1024).decode()
                if (isinstance(api, dict) and api.get('status') == 'alive'
                        and api.get('version') == version
                        and web == {'commit': sha, 'snapshot_id': snapshot}
                        and response.status == 200 and '/assets/' in html):
                    return
        except Exception:
            pass
        if attempt != 11:
            time.sleep(5)
    raise ReleaseError('PRODUCTION_SMOKE_FAILED')


def publish(platform, directory, sha, snapshot, report, save):
    version = 'rpl-' + sha[:16]
    previous = {role: platform.current(role) for role in PROJECTS}
    report.update(previous=previous, version=version, snapshot_id=snapshot, candidates={})
    save()
    for role in PROJECTS:
        report['phase'] = 'stage_' + role
        save()
        report['candidates'][role] = platform.stage(role, directory / role, version, snapshot, sha)
        save()
    assert_current_release(sha)
    if any(platform.current(role) != previous[role] for role in PROJECTS):
        raise ReleaseError('PRODUCTION_CHANGED_DURING_BUILD')
    touched = []
    try:
        for role in PROJECTS:
            # Record before the request: its response can be lost after promotion.
            touched.append(role)
            report['phase'] = 'promote_' + role
            report['promotion_attempted'] = touched.copy()
            save()
            platform.promote(role, report['candidates'][role], directory / role)
        if any(platform.current(role) != report['candidates'][role] for role in PROJECTS):
            raise ReleaseError('PROMOTION_NOT_CONFIRMED')
        report['phase'] = 'smoke'
        save()
        smoke(sha, snapshot, version)
        report['status'] = 'deployed'
        save()
    except Exception:
        restored = []
        for role in reversed(touched):
            try:
                current = platform.current(role)
                if current not in {previous[role], report['candidates'][role]}:
                    raise ReleaseError('CONCURRENT_PRODUCTION_CHANGE')
                if current != previous[role]:
                    platform.rollback(role, previous[role], directory / role)
                if platform.current(role) != previous[role]:
                    raise ReleaseError('ROLLBACK_NOT_CONFIRMED')
                restored.append(role)
            except Exception:
                pass
        report['rollback_confirmed'] = restored
        report['status'] = 'rolled_back' if len(restored) == len(touched) else 'manual_recovery_required'
        save()
        raise ReleaseError('PRODUCTION_RELEASE_FAILED') from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    sha = os.environ.get('GITHUB_SHA', '')
    report = {'commit': sha, 'status': 'preflight'}
    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'status': report['status'], 'phase': report.get('phase', 'preflight')}), flush=True)
    try:
        if not args.execute:
            raise ReleaseError('EXPLICIT_EXECUTE_REQUIRED')
        assert_current_release(sha)
        platform = Vercel()
        with tempfile.TemporaryDirectory(prefix='replay-release-') as temp:
            directory = Path(temp)
            artifact = directory / 'worker.json'
            env = {**os.environ, 'REPLAY_VERSION': 'rpl-' + sha[:16],
                   'VERCEL_PROJECT_ID': PROJECTS['web'][0], 'VERCEL_ORG_ID': TEAM}
            report['phase'] = 'build_worker'
            save()
            run(['node', 'scripts/build-worker-snapshot.mjs', '--create', '--output', str(artifact)], env=env)
            worker = json.loads(artifact.read_text())
            snapshot = worker.get('snapshot_id', '')
            if worker.get('version') != env['REPLAY_VERSION'] or not re.fullmatch(r'snap_[A-Za-z0-9]+', snapshot):
                raise ReleaseError('SNAPSHOT_IDENTITY_MISMATCH')
            report['worker'] = worker  # Builder's secret-free release manifest only.
            save()
            prepare_sources(directory, sha, snapshot)
            publish(platform, directory, sha, snapshot, report, save)
    except Exception as error:
        report['error'] = str(error) if isinstance(error, ReleaseError) else 'RELEASE_FAILED'
        if report['status'] == 'preflight':
            report['status'] = 'failed_before_promotion'
        save()
        print(json.dumps({'status': report['status'], 'error': report['error']}))
        return 1
    print(json.dumps({'status': report['status'], 'url': 'https://' + PROJECTS['web'][1]}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
