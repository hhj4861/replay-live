/** Provision only the POC engine, its shared sample, and the existing invite code. */
import { Sandbox } from '../web/node_modules/@vercel/sandbox/dist/index.js';
import { readFile, readdir, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { homedir } from 'node:os';

const root = fileURLToPath(new URL('../', import.meta.url));
const remote = '/vercel/sandbox/replay';
const name = process.env.REPLAY_SANDBOX_NAME || 'replay-live-poc';
// Reuse the signed-in CLI credential in memory. Do not download production envs.
const linked = JSON.parse(await readFile(path.join(root, 'web/.vercel/project.json'), 'utf8'));
const auth = JSON.parse(await readFile(path.join(homedir(), 'Library/Application Support/com.vercel.cli/auth.json'), 'utf8'));
if (!auth.token) throw new Error('Sign in with the Vercel CLI before provisioning');
const credentials = { token: auth.token, teamId: linked.orgId, projectId: linked.projectId };
const sandbox = await Sandbox.getOrCreate({ ...credentials, name, image: 'vercel/sandbox/universal', ports: [8081],
  timeout: 45 * 60 * 1000, resources: { vcpus: 2 }, persistent: true,
  snapshotExpiration: 7 * 24 * 3600 * 1000, keepLastSnapshots: { count: 2 }, resume: true });

console.log(JSON.stringify({ sandbox: sandbox.name, status: sandbox.status, api_origin: sandbox.domain(8081) }));
const files = (await readdir(path.join(root, 'server'))).filter(name => name.endsWith('.py')).map(name => `server/${name}`);
files.push('requirements.lock', 'scripts/cloud-start.py', 'data-public/invite-code.txt');
await sandbox.writeFiles(await Promise.all(files.map(async file => ({ path: `${remote}/${file}`, content: await readFile(path.join(root, file)) }))));
await sandbox.writeFiles([
  { path: `${remote}/data-public/shared/sample.mp4`, content: await readFile(path.join(root, 'data/sample.mp4')) },
  { path: `${remote}/start-api.sh`, content: Buffer.from('#!/bin/bash\nset -euo pipefail\ncd /vercel/sandbox/replay\nexec .venv/bin/python scripts/cloud-start.py "$@"\n') },
]);

const setup = await sandbox.runCommand({ cmd: 'bash', args: ['-lc',
  'set -euo pipefail\ncd /vercel/sandbox/replay\nchmod 600 data-public/invite-code.txt\nif ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then sudo apt-get update -qq; sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg; fi\npython3 -m venv .venv\n.venv/bin/pip install -q -r requirements.lock\ncommand -v ffmpeg\ncommand -v ffprobe'], timeoutMs: 240000 });
console.log((await setup.stdout()).trim());
if (setup.exitCode !== 0) {
  console.error((await setup.stderr()).slice(-3500));
  throw new Error(`Sandbox setup failed (${setup.exitCode})`);
}
const info = { sandbox_name: sandbox.name, api_origin: sandbox.domain(8081),
  ui_origin: 'https://replay-live-poc.vercel.app', expires_at: sandbox.expiresAt?.getTime() / 1000 };
await mkdir(path.join(root, 'data-public'), { recursive: true });
await writeFile(path.join(root, 'data-public/vercel-runtime.json'), `${JSON.stringify(info, null, 2)}\n`);
console.log(JSON.stringify(info));
