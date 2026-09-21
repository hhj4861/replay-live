const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const code = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../lib/helper-release.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
async function load(value) {
  const module = { exports: {} };
  vm.runInNewContext(code, { module, exports: module.exports, fetch: async () => Response.json(value) });
  return module.exports.helperRelease(new AbortController().signal);
}
test('no phantom download links before publication', async () => {
  assert.equal(await load({ version: null, downloads: [] }), null);
});
test('only versioned first-party artifacts with checksums become install links', async () => {
  const artifact = { label: 'macOS Apple Silicon', url: 'https://github.com/hhj4861/replay-live/releases/download/helper-v0.2.0/ReplayLiveHelper-0.2.0-macos-arm64.zip', sha256: 'a'.repeat(64) };
  assert.equal((await load({ version: '0.2.0', downloads: [artifact] })).downloads.length, 1);
  for (const patch of [{ url: 'https://attacker.invalid/setup.exe' }, { url: artifact.url + '?redirect=evil' }, { sha256: '' }, { label: 'unknown' }]) {
    await assert.rejects(load({ version: '0.2.0', downloads: [{ ...artifact, ...patch }] }));
  }
  await assert.rejects(load(null));
});
