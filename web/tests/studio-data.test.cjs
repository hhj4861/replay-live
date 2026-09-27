const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const code = ts.transpileModule(fs.readFileSync(require.resolve('../lib/studio-data.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const exported = {};
vm.runInNewContext(code, { exports: exported, Date, Number, Math });
const { createStudioConfigCache, sourceDurationFailure, storageBreakdown } = exported;

test('a 50 MiB import reservation does not change the stored-file total', () => {
  const mib = 1024 ** 2, stored = 5.9 * mib;
  for (const reserved of [0, 50 * mib, 0]) {
    const result = storageBreakdown({ storage_bytes: stored + reserved, storage_reserved_bytes: reserved });
    assert.ok(Math.abs(result.stored - stored) < .001);
    assert.equal(result.reserved, reserved);
  }
  assert.equal(storageBreakdown({ storage_bytes: 5, storage_reserved_bytes: 10 }).stored, 0);
});

test('configuration is reused for one minute and isolated by session', async () => {
  let now = 0, calls = 0;
  const cache = createStudioConfigCache(() => now);
  const load = async () => ({ generation: ++calls });
  const first = await cache.read('account-a', load);
  now = 59_999;
  assert.equal(await cache.read('account-a', load), first);
  assert.equal(calls, 1);
  now = 60_000;
  assert.notEqual(await cache.read('account-a', load), first);
  await cache.read('account-b', load);
  assert.equal(calls, 3);
});

test('logout and newer sessions fence old in-flight configuration results', async () => {
  const cache = createStudioConfigCache();
  let finish;
  const old = cache.read('old-session', () => new Promise(resolve => { finish = resolve; }));
  cache.clear();
  const current = await cache.read('new-session', async () => ({ generation: 'new' }));
  finish({ generation: 'old' });
  await old;
  assert.equal(await cache.read('new-session', () => assert.fail('must reuse current configuration')), current);
});

test('a failed configuration request is retried, not cached', async () => {
  const cache = createStudioConfigCache();
  await assert.rejects(cache.read('account', async () => { throw Error('offline'); }), /offline/);
  assert.equal(await cache.read('account', async () => 'recovered'), 'recovered');
});

test('duration errors show the configured maximum, with an unloaded-limit fallback', () => {
  assert.match(sourceDurationFailure(120), /최대 2분 0초/);
  assert.match(sourceDurationFailure(180), /최대 3분 0초/);
  assert.match(sourceDurationFailure(), /허용 재생 시간/);
});
