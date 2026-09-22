const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const source = fs.readFileSync(path.join(__dirname, '../lib/helper-platform.ts'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
function api(document) {
  const module = { exports: {} };
  vm.runInNewContext(code, { module, exports: module.exports, setTimeout, clearTimeout, document });
  return module.exports;
}
const hints = (platform, architecture, bitness = '64') => ({ userAgent: '', userAgentData: { platform, getHighEntropyValues: async keys => {
  assert.deepEqual(Array.from(keys), ['architecture', 'bitness']); return { architecture, bitness };
} } });
test('selects the exact available desktop package using local CPU hints', async () => {
  for (const [os, arch, label] of [['macOS', 'arm', 'macOS Apple Silicon'], ['macOS', 'x86', 'macOS Intel'], ['Windows', 'x86', 'Windows x64']]) {
    assert.equal((await api().detectHelperPlatform(hints(os, arch))).label, label);
  }
});
test('never guesses Intel from a reduced Mac UA and handles denied hardware hints', async () => {
  const nav = { userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)', platform: 'MacIntel' };
  assert.equal((await api().detectHelperPlatform(nav)).label, null);
  nav.userAgentData = { platform: 'macOS', getHighEntropyValues: async () => { throw new Error('denied'); } };
  assert.equal((await api().detectHelperPlatform(nav)).label, null);
  assert.equal((await api().detectHelperPlatform({ userAgent: 'Windows NT 10.0; Win64; x64', platform: 'Win32' })).label, null);
});
test('mobile, iPad desktop mode, Linux, Windows ARM and 32 bit never select x64 installers', async () => {
  for (const nav of [{ userAgent: 'Android', platform: 'Linux' }, { userAgent: 'Macintosh', platform: 'MacIntel', maxTouchPoints: 5 }, { userAgent: 'Linux x86_64' }, hints('Windows', 'arm'), hints('Windows', 'x86', '32')]) {
    const result = await api().detectHelperPlatform(nav);
    assert.equal(result.os, 'unsupported'); assert.equal(result.label, null);
  }
});
test('unknown Mac CPU offers only Mac choices; missing or unsupported releases do not offer downloads', () => {
  const release = { version: '0.2.0', downloads: ['macOS Apple Silicon', 'macOS Intel', 'Windows x64'].map(label => ({ label })) };
  assert.deepEqual(Array.from(api().platformDownloads(release, { os: 'macos', label: null }), x => x.label), ['macOS Apple Silicon', 'macOS Intel']);
  assert.equal(api().platformDownloads(null, { os: 'macos' }).length, 0);
  assert.equal(api().platformDownloads(release, { os: 'unsupported' }).length, 0);
});
test('explicit download request preserves the web page and removes its temporary anchor', () => {
  let clicks = 0, removes = 0;
  const anchor = { click: () => clicks++, remove: () => removes++ };
  const client = api({ createElement: () => anchor, body: { appendChild() {} } });
  assert.equal(clicks, 0);
  assert.equal(client.requestHelperDownload({ url: 'https://github.com/hhj4861/replay-live/releases/download/helper-v0.2.0/ReplayLiveHelper-0.2.0-macos-arm64.zip' }), true);
  assert.equal(clicks, 1); assert.equal(removes, 1); assert.equal(anchor.target, '_blank'); assert.equal(anchor.rel, 'noopener noreferrer');
  anchor.click = () => { throw new Error('blocked'); };
  assert.equal(client.requestHelperDownload({ url: anchor.href }), false); assert.equal(removes, 2);
});
test('a valid video import without a helper opens installation without creating a cloud job or clearing the URL', async () => {
  const commercial = fs.readFileSync(path.join(__dirname, '../app/commercial.tsx'), 'utf8');
  const body = commercial.slice(commercial.indexOf('  async function importSource('), commercial.indexOf('  async function create('));
  const js = ts.transpileModule(body + '\nmodule.exports = importSource;', { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} }; const seen = [];
  const context = { module, URL, action: async (name, task) => task(), sessionIdentity: () => 'account', importPending: false,
    sourcePlatform: {}, health: {}, sourceUrl: 'https://youtu.be/GcOe4ILS6Ow',
    localImporter: { isConnected: () => false }, setLocalConnected: value => seen.push(['connected', value]), setHelperInstallOpen: value => seen.push(['install', value]) };
  vm.runInNewContext(js, context);
  await module.exports({ preventDefault() {} });
  assert.deepEqual(seen, [['connected', false], ['install', true]]);
  assert.equal(context.sourceUrl, 'https://youtu.be/GcOe4ILS6Ow');
  // The form must remain clickable to reach that handler without a helper.
  const disabled = commercial.match(/className="source-import-button" disabled=\{([^}]+)\}/)[1];
  assert.doesNotMatch(disabled, /localConnected/);
});
