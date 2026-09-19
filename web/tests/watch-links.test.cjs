/* Synthetic URLs only; the validator never opens links or accesses accounts. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const filename = path.join(__dirname, '../lib/watch-links.ts');
const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const moduleValue = { exports: {} };
vm.runInNewContext(code, { module: moduleValue, exports: moduleValue.exports }, { filename });
const { normalizeWatchUrl, watchUrlError } = moduleValue.exports;
const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/watch-links.json'), 'utf8'));

test('viewing links match the Python contract for all shared platform and safety cases', () => {
  for (const item of fixtures) {
    if (item.expected === null) {
      assert.throws(() => normalizeWatchUrl(item.target, item.input, item.kind), undefined, JSON.stringify(item));
      assert.equal(typeof watchUrlError(item.target, item.input, item.kind), 'string');
    } else {
      const normalized = normalizeWatchUrl(item.target, item.input, item.kind);
      assert.equal(normalized, item.expected, JSON.stringify(item));
      assert.equal(normalizeWatchUrl(item.target, normalized, item.kind), normalized, 'normalization is idempotent');
      assert.equal(watchUrlError(item.target, item.input, item.kind), null);
    }
  }
});

test('invalid values, excessive encoded output and error messages do not leak supplied secrets', () => {
  for (const input of [null, undefined, 42, {}, 'https://www.twitch.tv/test?access_token=synthetic-private-value',
    `https://www.youtube.com/@${'가'.repeat(300)}`, `https://example.com/${'a'.repeat(2050)}`]) {
    assert.throws(() => normalizeWatchUrl('twitch', input, 'channel'), error => !error.message.includes('synthetic-private-value'));
  }
  assert.throws(() => normalizeWatchUrl('youtube', `https://www.youtube.com/@${'가'.repeat(300)}`, 'channel'));
  assert.throws(() => normalizeWatchUrl('custom', `https://example.com/${'가'.repeat(300)}`, 'broadcast'));
  assert.throws(() => normalizeWatchUrl('unknown', '', 'channel'));
  assert.throws(() => normalizeWatchUrl('youtube', '', 'other'));
});
