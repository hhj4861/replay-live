/** Disposable cloud probe. No production env, customer data or local daemon. */
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const controlRoot = process.env.REPLAY_PROBE_CONTROL_ROOT || root;
const value = flag => process.argv.includes(flag) ? process.argv[process.argv.indexOf(flag) + 1] : undefined;
const videoId = value('--video-id');
const output = value('--output');
const snapshotId = value('--snapshot');
const targeted = process.argv.includes('--pot-always-only');
if (!process.argv.includes('--run') || !/^[\w-]{11}$/.test(videoId || '') || !output || !/^snap_[A-Za-z0-9]+$/.test(snapshotId || '')) {
  throw new Error('Use --run --video-id ID --snapshot snap_ID --output NEW_REPORT_PATH');
}
const { Sandbox } = await import(pathToFileURL(path.join(controlRoot, 'web/node_modules/@vercel/sandbox/dist/index.js')));
const linked = JSON.parse(await readFile(path.join(controlRoot, 'web/.vercel/project.json'), 'utf8'));
const auth = JSON.parse(await readFile(path.join(homedir(), 'Library/Application Support/com.vercel.cli/auth.json'), 'utf8'));
if (!auth.token) throw new Error('Existing Vercel CLI authentication required');
const credentials = { token: auth.token, teamId: linked.orgId, projectId: linked.projectId };
// This is the same public-only address policy used by the existing worker.
const policySource = await readFile(path.join(root, 'web/lib/worker-network-policy.ts'), 'utf8');
const cidrs = [...policySource.matchAll(/'([0-9.]+\/\d+)'/g)].map(match => match[1]);
if (cidrs.length < 50 || cidrs.includes('0.0.0.0/0')) throw new Error('Invalid public egress policy');
const sources = ['scripts/youtube-server-probe.py', 'server/__init__.py', 'server/media_sources.py', 'server/media_runtime.py', 'server/output_policy.py'];
const payload = await Promise.all(sources.map(async file => ({path: `/vercel/sandbox/replay/${file}`, content: await readFile(path.join(root, file))})));
const report = {started_at: new Date().toISOString(), video_id: videoId, base_snapshot: snapshotId,
  production_changed: false, local_daemon_used: false, youtube_credentials_used: false, proxy_used: false,
  files: Object.fromEntries(payload.map(f => [f.path.split('/replay/')[1], createHash('sha256').update(f.content).digest('hex')])), results: []};
await writeFile(output, '', {flag:'wx', mode:0o600});
let sandbox;
try {
  sandbox = await Sandbox.create({...credentials, source:{type:'snapshot',snapshotId}, timeout:12*60_000,
    persistent:false, resources:{vcpus:2}, ports:[], env:{}, networkPolicy:{subnets:{allow:cidrs}},
    tags:{app:'replay-youtube-probe'}});
  report.sandbox_name = sandbox.name;
  console.log(JSON.stringify({phase:'cloud_created', sandbox:sandbox.name}));
  await sandbox.writeFiles(payload);
  // The snapshot holds only worker dependencies; control credentials stay here.
  if (!targeted) {
    const baseline = await sandbox.runCommand({cmd:'/vercel/sandbox/replay/.venv/bin/python', args:[
      '/vercel/sandbox/replay/scripts/youtube-server-probe.py','--mode','baseline','--video-id',videoId], timeoutMs:250_000});
    if (baseline.exitCode !== 0) throw new Error('PROBE_COMMAND_FAILED');
    report.results.push(JSON.parse(await baseline.stdout()));
    console.log(JSON.stringify(report.results.at(-1)));
  }
  const setup = await sandbox.runCommand({cmd:'bash',args:['-c',`set -euo pipefail
cd /vercel/sandbox
python3 -m venv candidate
candidate/bin/pip install --disable-pip-version-check 'yt-dlp[default]==2026.8.19'
node --version
command -v ffmpeg
`],timeoutMs:180_000});
  report.setup_exit_code = setup.exitCode;
  if (setup.exitCode !== 0) throw new Error('CANDIDATE_SETUP_FAILED');
  const run = async mode => {
    const command = await sandbox.runCommand({cmd:'/vercel/sandbox/candidate/bin/python',args:[
      '/vercel/sandbox/replay/scripts/youtube-server-probe.py','--mode',mode,'--video-id',videoId], timeoutMs:250_000});
    if (command.exitCode !== 0) throw new Error('PROBE_COMMAND_FAILED');
    const result = JSON.parse(await command.stdout());
    report.results.push(result);
    console.log(JSON.stringify(result));
  };
  if (!targeted) await run('standard');
  if (targeted || !report.results.at(-1).ok) {
    const provider = await sandbox.runCommand({cmd:'bash',args:['-c',`set -euo pipefail
cd /vercel/sandbox
candidate/bin/pip install --disable-pip-version-check bgutil-ytdlp-pot-provider==2.0.0
git clone --depth 1 --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil
cd bgutil/server
npm ci --no-audit --no-fund
./node_modules/.bin/tsc
git rev-parse HEAD
`],timeoutMs:180_000});
    report.provider_setup_exit_code = provider.exitCode;
    if (provider.exitCode !== 0) throw new Error('PROVIDER_SETUP_FAILED');
    report.provider_commit = (await provider.stdout()).trim().split('\n').at(-1);
    await run(targeted ? 'pot-always' : 'pot');
  }
} catch (error) {
  const known = ['CANDIDATE_SETUP_FAILED','PROBE_COMMAND_FAILED','PROVIDER_SETUP_FAILED'];
  report.error = known.includes(error.message) ? error.message : 'CLOUD_PROBE_FAILED';
  report.error_type = error.constructor.name;
  report.http_status = error.response?.status;
  if (/^[a-z_]{1,60}$/.test(error.json?.error?.code || '')) report.api_error_code = error.json.error.code;
  // Do not serialize SDK errors: they may include credentials or signed URLs.
  process.exitCode = 1;
} finally {
  if (sandbox) {
    try { await sandbox.stop(); report.sandbox_stopped = true; }
    catch { report.sandbox_stopped = false; process.exitCode = 1; }
  }
  report.finished_at = new Date().toISOString();
  report.cloud_download_succeeded = report.results.some(r => r.ok && r.mode !== 'baseline');
  if (!report.cloud_download_succeeded && !process.exitCode) process.exitCode = 2;
  await writeFile(output, JSON.stringify(report,null,2)+'\n', {mode:0o600});
  console.log(JSON.stringify({phase:'finished',report:output,cloud_download_succeeded:report.cloud_download_succeeded,
    sandbox_stopped:report.sandbox_stopped,error:report.error}));
}
