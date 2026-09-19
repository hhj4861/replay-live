/** Run synthetic PostgreSQL/Linux integration checks in one disposable VM.
 * No production data, invite codes, stream keys or environment files are uploaded.
 * Existing Vercel CLI authentication is held in memory only; no inbound ports.
 */
import { Sandbox } from '../web/node_modules/@vercel/sandbox/dist/index.js';
import { readFile, readdir, writeFile, mkdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const linked = JSON.parse(await readFile(path.join(root, 'web/.vercel/project.json'), 'utf8'));
const cli = JSON.parse(await readFile(path.join(homedir(), 'Library/Application Support/com.vercel.cli/auth.json'), 'utf8'));
if (!cli.token) throw new Error('Existing Vercel CLI sign-in is required');
const name = `replay-pg-test-${Date.now().toString(36)}`;
const started = new Date().toISOString();
let sandbox;
let result = { started, sandbox_name: name, passed: false, stopped: false };
try {
  sandbox = await Sandbox.create({ name, image: 'vercel/sandbox/universal',
    token: cli.token, teamId: linked.orgId, projectId: linked.projectId,
    timeout: 10 * 60 * 1000, resources: { vcpus: 2 }, ports: [], persistent: false });
  console.log(JSON.stringify({ event: 'synthetic_postgres_test_started', sandbox_name: name }));
  const remote = '/vercel/sandbox/replay-test';
  const files = ['requirements.lock'];
  for (const dir of ['server', 'tests', 'migrations']) {
    for (const file of await readdir(path.join(root, dir))) {
      if (file.endsWith(dir === 'migrations' ? '.sql' : '.py')) files.push(`${dir}/${file}`);
    }
  }
  for (const file of ['commercial-migrate.py', 'commercial-backup.py', 'commercial-restore.py', 'commercial-verify.py']) {
    files.push(`scripts/${file}`);
  }
  for (const file of await readdir(path.join(root, 'deploy/commercial'))) {
    if (file.endsWith('.py')) files.push(`deploy/commercial/${file}`);
  }
  await sandbox.writeFiles(await Promise.all(files.map(async file => ({
    path: `${remote}/${file}`, content: await readFile(path.join(root, file)),
  }))));
  const setup = await sandbox.runCommand({ cmd: 'bash', args: ['-lc', [
    'set -euo pipefail',
    `cd ${remote}`,
    'sudo apt-get update -qq >/tmp/replay-pg-setup.log 2>&1',
    'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql ffmpeg >>/tmp/replay-pg-setup.log 2>&1',
    'python3 -m venv .venv',
    '.venv/bin/pip install -q -r requirements.lock >>/tmp/replay-pg-setup.log 2>&1',
    'PGTEST_BIN="$(dirname "$(ls /usr/lib/postgresql/*/bin/initdb | head -n 1)")"',
    'mkdir -p /vercel/sandbox/replay-pg-socket',
    '"$PGTEST_BIN/initdb" -D /vercel/sandbox/replay-pg-data -A trust --no-locale --encoding=UTF8 >>/tmp/replay-pg-setup.log 2>&1',
    '"$PGTEST_BIN/pg_ctl" -D /vercel/sandbox/replay-pg-data -l /tmp/replay-pg.log -o "-h 127.0.0.1 -p 55432 -k /vercel/sandbox/replay-pg-socket" -w start >>/tmp/replay-pg-setup.log 2>&1',
    '"$PGTEST_BIN/createdb" -h 127.0.0.1 -p 55432 replay_test',
    '"$PGTEST_BIN/createdb" -h 127.0.0.1 -p 55432 replay_test_restore',
    '"$PGTEST_BIN/postgres" --version',
    'ffmpeg -version | head -n 1',
  ].join('\n')], timeoutMs: 360000 });
  const setupOut = (await setup.stdout()).trim();
  if (setup.exitCode !== 0) {
    const logs = await sandbox.runCommand({ cmd: 'tail', args: ['-n', '25', '/tmp/replay-pg-setup.log'] });
    console.error((await logs.stdout()).slice(-4500));
    throw new Error(`Synthetic database setup failed (${setup.exitCode})`);
  }
  console.log(setupOut);
  const tests = await sandbox.runCommand({ cmd: 'bash', args: ['-lc', [
    'set -euo pipefail',
    `cd ${remote}`,
    'export REPLAY_TEST_POSTGRES_URL="postgresql+psycopg://$(id -un)@127.0.0.1:55432/replay_test"',
    '.venv/bin/python -m pytest tests/test_postgres.py tests/test_repository.py tests/test_storage.py tests/test_access_policy.py tests/test_reliability.py tests/test_worker.py -q --tb=short',
    'export REPLAY_DATABASE_URL="$REPLAY_TEST_POSTGRES_URL"',
    'export REPLAY_RESTORE_DATABASE_URL="postgresql+psycopg://$(id -un)@127.0.0.1:55432/replay_test_restore"',
    '.venv/bin/python scripts/commercial-migrate.py',
    '.venv/bin/python scripts/commercial-migrate.py',
    '.venv/bin/python scripts/commercial-verify.py --database-check --seed-fixture',
    '.venv/bin/python scripts/commercial-backup.py /vercel/sandbox/replay-fixture.dump',
    '.venv/bin/python scripts/commercial-restore.py /vercel/sandbox/replay-fixture.dump --confirm-database replay_test_restore',
    '.venv/bin/python scripts/commercial-verify.py --database-check --database-env REPLAY_RESTORE_DATABASE_URL --assert-restored-fixture',
  ].join('\n')], timeoutMs: 180000 });
  const stdout = (await tests.stdout()).trim();
  const stderr = (await tests.stderr()).trim();
  console.log(stdout);
  if (stderr) console.error(stderr.slice(-3000));
  result = { ...result, passed: tests.exitCode === 0, exit_code: tests.exitCode,
    platform: setupOut, test_output: stdout, ended: new Date().toISOString() };
  if (tests.exitCode !== 0) process.exitCode = 1;
} catch (error) {
  result = { ...result, failure: error instanceof Error ? error.message.split('\n')[0].slice(0, 160) : 'Test execution failed' };
  console.error(result.failure);
  process.exitCode = 1;
} finally {
  if (sandbox) {
    try { await sandbox.stop(); result.stopped = true; }
    catch { console.error('Temporary VM stop was not acknowledged; its ten-minute expiry remains active'); }
  }
  await mkdir(path.join(root, 'docs'), { recursive: true });
  await writeFile(path.join(root, 'docs/commercial-postgres-evidence.json'), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify({ event: 'synthetic_postgres_test_finished', passed: result.passed, stopped: result.stopped }));
}
