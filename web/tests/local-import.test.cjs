/* Real browser helper module with synthetic loopback responses. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../lib/local-import.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const token = 'p'.repeat(43);
const job = { id: 'a'.repeat(32), state: 'ready', bytes: 4, sha256: 'b'.repeat(64) };
const input = { provider: 'youtube', url: 'https://youtu.be/example', name: '내 영상', maxBytes: 1024, maxDuration: 120 };

function environment(handler) {
  let identity = 'account-a'; const calls = []; const module = { exports: {} };
  vm.runInNewContext(code, { module, exports: module.exports, Headers, Response, File, crypto,
    AbortSignal, AbortController, setTimeout, clearTimeout, Uint8Array,
    require: () => ({ sessionIdentity: () => identity, assertSessionIdentity(value) { if (value !== identity) throw new Error('account changed'); } }),
    fetch: async (url, options) => {
      const route = new URL(url).pathname; calls.push({ url, options, route });
      if (handler) { const result = await handler(route, options); if (result) return result; }
      if (route === '/pair') return Response.json({ token, version: 1 });
      if (options.method === 'DELETE') return new Response(null, { status: 204 });
      if (route === '/imports' || route === `/imports/${job.id}`) return Response.json(job);
      if (route.endsWith('/file')) return new Response(new Uint8Array([1, 2, 3, 4]));
      throw new Error('unexpected route');
    },
  });
  return { client: module.exports.createLocalImporter(), calls, switchAccount: () => { identity = 'account-b'; } };
}

test('paired import reads the bounded MP4 and cleans it, without cloud credentials or endpoint selection', async () => {
  const env = environment(); await env.client.pair('code');
  const result = await env.client.run(input, new AbortController().signal, () => {});
  assert.equal(result.file.name, '내 영상.mp4');
  assert.equal(result.file.size, 4); assert.equal(result.sha256, job.sha256);
  assert.equal(env.calls.at(-1).route, `/imports/${job.id}`);
  assert.equal(env.calls.at(-1).options.method, 'DELETE');
  for (const { url, options, route } of env.calls) {
    assert.ok(url.startsWith('http://127.0.0.1:17833/'));
    assert.equal(options.credentials, 'omit'); assert.equal(options.redirect, 'error');
    assert.equal(options.targetAddressSpace, 'loopback');
    assert.equal(options.headers.get('X-Replay-Local'), '1');
    assert.equal(options.headers.get('Authorization'), route === '/pair' ? null : `Bearer ${token}`);
  }
});

test('without pairing, no network request or cloud fallback occurs', async () => {
  const env = environment();
  await assert.rejects(env.client.run(input, new AbortController().signal, () => {}), /먼저 연결/);
  assert.equal(env.calls.length, 0);
});

test('explicit source failure is surfaced once and its local job is removed', async () => {
  const env = environment(route => route === '/imports' ? Response.json({ ...job, state: 'failed', error_code: 'SOURCE_BOT_CHECK_REQUIRED' }) : null);
  await env.client.pair('code');
  await assert.rejects(env.client.run(input, new AbortController().signal, () => {}), /SOURCE_BOT_CHECK_REQUIRED/);
  assert.equal(env.calls.filter(call => call.route === '/imports').length, 1);
  assert.equal(env.calls.at(-1).options.method, 'DELETE');
});

test('oversized and incomplete file bodies cannot reach cloud upload', async () => {
  for (const size of [3, 5]) {
    const env = environment(route => route.endsWith('/file') ? new Response(new Uint8Array(size)) : null);
    await env.client.pair('code');
    await assert.rejects(env.client.run(input, new AbortController().signal, () => {}), size < 4 ? /SOURCE_INCOMPLETE/ : /SOURCE_TOO_LARGE/);
    assert.equal(env.calls.at(-1).options.method, 'DELETE');
  }
});

test('account change after job creation cancels the old job using its captured local capability', async () => {
  const env = environment(route => { if (route === '/imports') { env.switchAccount(); return Response.json(job); } });
  await env.client.pair('code');
  await assert.rejects(env.client.run(input, new AbortController().signal, () => {}), /account changed/);
  assert.equal(env.client.isConnected(), false);
  assert.equal(env.calls.at(-1).options.method, 'DELETE');
  assert.equal(env.calls.filter(call => call.route.endsWith('/file')).length, 0);
});

test('logout during pairing revokes late local credentials instead of retaining them', async () => {
  const env = environment(route => { if (route === '/pair') env.switchAccount(); });
  await assert.rejects(env.client.pair('code'), /account changed/);
  assert.equal(env.client.isConnected(), false);
  assert.equal(env.calls.at(-1).route, '/session');
});

test('user cancellation cleans the pending job without downloading a file', async () => {
  const controller = new AbortController();
  const env = environment(route => {
    if (route === '/imports') { controller.abort(); return Response.json({ ...job, state: 'downloading' }); }
  });
  await env.client.pair('code');
  await assert.rejects(env.client.run(input, controller.signal, () => {}), { name: 'AbortError' });
  assert.equal(env.calls.at(-1).options.method, 'DELETE');
  assert.equal(env.calls.filter(call => call.route.endsWith('/file')).length, 0);
});

test('lost local authentication requires reconnecting', async () => {
  const env = environment(route => route === '/imports' ? Response.json({ detail: '연결 만료' }, { status: 401 }) : null);
  await env.client.pair('code');
  await assert.rejects(env.client.run(input, new AbortController().signal, () => {}), /연결 만료/);
  assert.equal(env.client.isConnected(), false);
});

test('stopped daemon clears the connected state and does not try the cloud importer', async () => {
  const env = environment(route => { if (route === '/imports') throw new TypeError('Failed to fetch'); });
  await env.client.pair('code');
  await assert.rejects(env.client.run(input, new AbortController().signal, () => {}), /도우미를 실행/);
  assert.equal(env.client.isConnected(), false);
  assert.equal(env.calls.filter(call => call.route === '/imports').length, 1);
});
