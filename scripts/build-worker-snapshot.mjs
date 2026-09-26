#!/usr/bin/env node
/** Build an immutable worker snapshot from an explicit, secret-free file allowlist. */
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Sandbox } from '../web/node_modules/@vercel/sandbox/dist/index.js';

const root = fileURLToPath(new URL('../', import.meta.url));
const remote = '/vercel/sandbox/replay';
const files = ['requirements.lock', 'server/__init__.py', 'server/worker.py', 'server/media_runtime.py', 'server/media_sources.py', 'server/output_policy.py', 'server/stream_targets.py'];
const version = process.env.REPLAY_VERSION || '';
const ffmpegPackage = process.env.REPLAY_FFMPEG_PACKAGE_VERSION || '';
const digest = value => createHash('sha256').update(value).digest('hex');
const output = process.argv.includes('--output') ? process.argv[process.argv.indexOf('--output') + 1] : path.join(tmpdir(), `replay-worker-${version}.json`);

async function credentials() {
  const linked = process.env.VERCEL_PROJECT_ID && process.env.VERCEL_ORG_ID
    ? { projectId: process.env.VERCEL_PROJECT_ID, orgId: process.env.VERCEL_ORG_ID }
    : JSON.parse(await readFile(path.join(root, 'web/.vercel/project.json'), 'utf8'));
  if (process.env.VERCEL_TOKEN) return { projectId: linked.projectId, teamId: linked.orgId, token: process.env.VERCEL_TOKEN };
  if (process.env.VERCEL_OIDC_TOKEN) return { projectId: linked.projectId, teamId: linked.orgId };
  for (const file of ['Library/Application Support/com.vercel.cli/auth.json', '.local/share/com.vercel.cli/auth.json']) {
    try {
      const auth = JSON.parse(await readFile(path.join(homedir(), file), 'utf8'));
      if (auth.token) return { projectId: linked.projectId, teamId: linked.orgId, token: auth.token };
    } catch { /* Try the other platform's CLI path. Never print credential contents. */ }
  }
  throw new Error('Project-scoped Vercel authentication is required');
}

async function main() {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(version) || !/^[A-Za-z0-9.+:~_-]{1,120}$/.test(ffmpegPackage)) {
    throw new Error('REPLAY_VERSION and an exact REPLAY_FFMPEG_PACKAGE_VERSION are required');
  }
  if (!process.argv.includes('--create') && !process.argv.includes('--dry-run')) throw new Error('Use --dry-run or explicitly --create');
  const sources = await Promise.all(files.map(async file => ({ file, content: await readFile(path.join(root, file)) })));
  const lock = sources[0].content.toString('utf8').trim().split('\n');
  if (lock.some(line => !/^[A-Za-z0-9_.-]+==[A-Za-z0-9.+!-]+$/.test(line))) throw new Error('Every requirements.lock entry must be exactly version pinned');
  const sourceHash = createHash('sha256');
  for (const source of sources) sourceHash.update(source.file + '\0').update(source.content).update('\0');
  const artifact = { version, source_sha256: sourceHash.digest('hex'), ffmpeg_package_version: ffmpegPackage,
    files: Object.fromEntries(sources.map(source => [source.file, digest(source.content)])), sdk_version: '3.2.2' };
  if (process.argv.includes('--dry-run')) {
    console.log(JSON.stringify({ ...artifact, created: false, credential_files_read: false, live_stream_started: false }));
    return;
  }
  // Reserve the artifact path before spending on a cloud build; never overwrite a release.
  await writeFile(output, '', { flag: 'wx', mode: 0o600 });
  let sandbox;
  try {
    sandbox = await Sandbox.create({ ...await credentials(), image: 'vercel/sandbox/universal', timeout: 15 * 60_000,
      persistent: false, resources: { vcpus: 2 }, ports: [], env: {}, tags: { app: 'replay-worker-build', release: version } });
    await sandbox.writeFiles(sources.map(source => ({ path: `${remote}/${source.file}`, content: source.content })));
    await sandbox.writeFiles([{ path: `${remote}/release-input.json`, content: Buffer.from(JSON.stringify(artifact)) }]);
    const setup = await sandbox.runCommand({ cmd: 'bash', args: ['-c', `set -euo pipefail
cd /vercel/sandbox/replay
sudo apt-get update -qq
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "ffmpeg=$1"
test "$(dpkg-query -W -f='\${Version}' ffmpeg)" = "$1"
python3 -m venv .venv
.venv/bin/pip install --no-cache-dir --disable-pip-version-check -r requirements.lock
.venv/bin/pip check
node --version
.venv/bin/python -c 'import yt_dlp_ejs'
.venv/bin/python - <<'PY'
import hashlib,json,platform,subprocess
from pathlib import Path
from server.media_runtime import validate_media,stream_command,run_stream,require_verified_rtmps
from server.output_policy import estimate_output_bytes
release=json.loads(Path('release-input.json').read_text())
release['rtmps_tls_backend']=require_verified_rtmps()
subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=30','-f','lavfi','-i','sine=frequency=440','-t','3','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac','sample.mp4'],check=True)
metadata=validate_media(Path('sample.mp4'))
result=run_stream(stream_command('sample.mp4','sample.flv'),expected_duration=metadata['duration'],local_output=Path('sample.flv'),max_output_bytes=estimate_output_bytes(metadata['duration']))
if not result.complete: raise RuntimeError('Worker local media smoke failed')
release.update(ffmpeg_version=subprocess.check_output(['ffmpeg','-version'],text=True).splitlines()[0],python_version=platform.python_version(),local_smoke={'duration':metadata['duration'],'progress':result.progress,'sha256':hashlib.sha256(Path('sample.flv').read_bytes()).hexdigest()},live_stream_started=False)
Path('release.json').write_text(json.dumps(release,sort_keys=True))
for name in ['sample.mp4','sample.flv','release-input.json']: Path(name).unlink()
PY
`, 'worker-build', ffmpegPackage], timeoutMs: 10 * 60_000 });
    if (setup.exitCode !== 0) throw new Error(`Worker package/build verification failed (${setup.exitCode})`);
    const report = JSON.parse((await sandbox.readFileToBuffer({ path: `${remote}/release.json` })).toString('utf8'));
    if (report.version !== version || report.source_sha256 !== artifact.source_sha256) throw new Error('Release identity mismatch');
    const snapshot = await sandbox.snapshot({ expiration: 0 });
    const release = { ...report, snapshot_id: snapshot.snapshotId, snapshot_expires_at: snapshot.expiresAt?.toISOString() ?? null,
      built_at: new Date().toISOString(), release_sha256: digest(JSON.stringify(report)) };
    await writeFile(output, JSON.stringify(release, null, 2) + '\n', { mode: 0o600 });
    console.log(JSON.stringify({ snapshot_id: release.snapshot_id, version, source_sha256: release.source_sha256, artifact: output }));
  } finally {
    if (sandbox) await sandbox.stop().catch(() => undefined);
  }
}

main().catch(() => {
  // SDK failures can contain auth or presigned URLs. Keep public output structured.
  console.error(JSON.stringify({ error: 'WORKER_SNAPSHOT_BUILD_FAILED', artifact: output }));
  process.exitCode = 1;
});
