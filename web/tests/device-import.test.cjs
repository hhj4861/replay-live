const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const source = fs.readFileSync(require('node:path').join(__dirname, '../lib/local-import.ts'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const id = 'a'.repeat(32);
const localToken = 'L'.repeat(43);

function fixture(options = {}) {
  let identity = 'user-a';
  const cloud = [], local = [];
  const module = { exports: {} };
  vm.runInNewContext(code, { module, exports: module.exports, Headers, Response, File, crypto,
    AbortSignal, AbortController, setTimeout, clearTimeout, Uint8Array,
    require: () => ({ sessionIdentity: () => identity, assertSessionIdentity(value) { assert.equal(value, identity); },
      api: async (path, init) => {
        cloud.push({ path, init });
        if (init?.method === 'DELETE') return {};
        if (path === '/device-imports') {
          options.onTicket?.(() => { identity = 'user-b'; });
          return { id, token: 'single-task-token', state: 'queued', expires_at: 123, error_code: null };
        }
        return { state: 'completed', media: { id, status: 'pending' } };
      } }),
    fetch: async (url, init) => {
      local.push({ url, init });
      const path = new URL(url).pathname;
      if (path === '/pair') return Response.json({ token: localToken, version: 1,
        features: options.oldDaemon ? [] : ['cloud-direct-upload'] });
      if (init.method === 'DELETE') return new Response(null, { status: 204 });
      if (path === '/cloud-imports') {
        options.onStart?.();
        return Response.json(options.failed ? { id, state: 'failed', error_code: options.errorCode || 'SOURCE_BOT_CHECK_REQUIRED', phase: options.phase }
          : { id, state: 'ready', media_id: id });
      }
      throw new Error('Browser attempted an unexpected file transfer');
    },
  });
  return { client: module.exports.createLocalImporter(), cloud, local };
}
const input = { provider: 'youtube', url: 'https://youtu.be/GcOe4ILS6Ow', name: '영상' };

test('browser delegates a single cloud ticket, never reads or uploads video bytes', async () => {
  const f = fixture(); await f.client.pair('code');
  const media = await f.client.runCloud(input, new AbortController().signal, () => {});
  assert.equal(media.id, id);
  assert.deepEqual(f.cloud.map(c => c.path), ['/device-imports', '/device-imports/' + id]);
  const start = f.local.find(c => c.url.endsWith('/cloud-imports'));
  assert.deepEqual(JSON.parse(start.init.body), { id, token: 'single-task-token' });
  assert.equal(start.init.headers.get('Authorization'), 'Bearer ' + localToken);
  assert.equal(start.init.body.includes(input.url), false);
  assert.equal(f.local.at(-1).init.method, 'DELETE');
});

test('old daemon is rejected before cloud registration; no cloud-downloader fallback', async () => {
  const f = fixture({ oldDaemon: true }); await f.client.pair('code');
  await assert.rejects(f.client.runCloud(input, new AbortController().signal, () => {}), /최신 버전/);
  assert.equal(f.cloud.length, 0);
});

test('account change during ticket response cannot delegate old account work to the PC', async () => {
  const f = fixture({ onTicket: change => change() }); await f.client.pair('code');
  await assert.rejects(f.client.runCloud(input, new AbortController().signal, () => {}));
  assert.equal(f.local.some(c => c.url.endsWith('/cloud-imports')), false);
  assert.equal(f.cloud.some(c => c.init?.method === 'DELETE'), false);
});

test('daemon failure cancels the cloud task, without browser upload or server source fallback', async () => {
  const f = fixture({ failed: true }); await f.client.pair('code');
  await assert.rejects(f.client.runCloud(input, new AbortController().signal, () => {}), /SOURCE_BOT_CHECK_REQUIRED/);
  assert.equal(f.cloud.at(-1).path, '/device-imports/' + id);
  assert.equal(f.cloud.at(-1).init.method, 'DELETE');
});

test('user cancellation after delegation cannot report success or transfer media', async () => {
  const controller = new AbortController();
  const f = fixture({ onStart: () => controller.abort() }); await f.client.pair('code');
  await assert.rejects(f.client.runCloud(input, controller.signal, () => {}), { name: 'AbortError' });
  assert.equal(f.cloud.at(-1).init.method, 'DELETE');
});

for (const [phase, errorCode] of [['requesting', 'DEVICE_IMPORT_TASK_FAILED'], ['downloading', 'SOURCE_BOT_CHECK_REQUIRED'], ['uploading', 'DEVICE_IMPORT_UPLOAD_FAILED'], ['validating', 'DEVICE_IMPORT_COMPLETE_FAILED']]) {
  test(`terminal ${phase} failure preserves its phase even when no progress poll preceded it`, async () => {
    const f = fixture({ failed: true, phase, errorCode }); await f.client.pair('code');
    const phases = [];
    await assert.rejects(f.client.runCloud(input, new AbortController().signal, value => phases.push(value)), error => {
      assert.equal(error.name, 'LocalImportError'); assert.equal(error.message, errorCode); assert.equal(error.phase, phase); return true;
    });
    assert.equal(phases.at(-1), phase);
    assert.equal(f.cloud.at(-1).init.method, 'DELETE');
  });
}
