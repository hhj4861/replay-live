const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');

function fixture(commercial) {
  const filename = path.join(__dirname, '../api/session.ts');
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }, fileName: filename,
  }).outputText;
  let cloudCalls = 0;
  const imports = { 'node:crypto': require('node:crypto'), 'node:timers/promises': require('node:timers/promises'),
    '@vercel/sandbox': { Sandbox: { get: async () => { cloudCalls += 1; throw new Error('Synthetic cloud unavailable'); } } } };
  const module = { exports: {} };
  vm.runInNewContext(code, { module, exports: module.exports, Headers, Response, Buffer, URL, AbortSignal, setTimeout, clearTimeout,
    process: { env: { REPLAY_COMMERCIAL: commercial, REPLAY_INVITE_CODE: 'synthetic-invite-code', REPLAY_SANDBOX_NAME: 'synthetic-poc' } },
    require: name => { if (!(name in imports)) throw new Error('Unexpected import'); return imports[name]; },
    fetch: () => { throw new Error('Unmocked networking is forbidden'); } }, { filename });
  return { handle: module.exports.default.fetch, cloudCalls: () => cloudCalls };
}

test('commercial deployment retires the invite endpoint before any request, body or cloud access', async () => {
  const f = fixture('1');
  const unreadable = new Proxy({}, { get() { throw new Error('Request must not be inspected'); } });
  const response = await f.handle(unreadable);
  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), { detail: 'Not found' });
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(f.cloudCalls(), 0);
});

test('POC deployment keeps invite validation before its existing Sandbox path', async () => {
  const f = fixture('0');
  const request = code => new Request('https://replay-live-poc.vercel.app/api/session', {
    method: 'POST', headers: { Origin: 'https://replay-live-poc.vercel.app', 'X-Replay-Client': '1', 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
  assert.equal((await f.handle(request('incorrect'))).status, 401);
  assert.equal(f.cloudCalls(), 0);
  assert.equal((await f.handle(request('synthetic-invite-code'))).status, 503);
  assert.equal(f.cloudCalls(), 1);
});
