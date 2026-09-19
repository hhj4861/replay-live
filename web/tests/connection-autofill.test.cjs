/* Synthetic deferred stores only: no browser data, saved credentials or network. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');

function moduleAt(relative, requireModule) {
  const filename = path.join(__dirname, relative);
  const module = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { module, exports: module.exports, require: requireModule, URL, TextEncoder }, { filename });
  return module.exports;
}
const watchLinks = moduleAt('../lib/watch-links.ts', () => ({}));
const connections = moduleAt('../lib/stream-connections.ts', name => name === './watch-links' ? watchLinks : {});
const { createConnectionAutofill } = moduleAt('../lib/connection-autofill.ts', () => connections);
const flush = () => new Promise(resolve => setImmediate(resolve));
const value = target => ({ target, server_url: 'rtmps://stream.example.com:443/app', stream_key: `synthetic-${target}-key` });
function environment(location = 'browser') {
  const calls = [];
  const applied = [];
  const store = { location, identity: 'synthetic-owner', use(target) {
    return new Promise((resolve, reject) => calls.push({ target, resolve, reject }));
  } };
  let active = store;
  const controller = createConnectionAutofill({ isCurrentStore: candidate => active === candidate,
    apply: (target, connection, accept) => applied.push({ target, connection, accept }) });
  return { controller, store, calls, applied, retire: () => { active = undefined; } };
}

for (const location of ['browser', 'account']) test(`${location}: selecting saved platforms loads once and polling never reloads them`, async () => {
  const env = environment(location);
  env.controller.sync(env.store, ['twitch'], ['twitch', 'youtube']);
  assert.equal(env.calls.length, 1);
  assert.equal(env.controller.getSnapshot().twitch.loading, true);
  assert.equal(env.applied.length, 0, 'saved metadata alone cannot provide broadcast credentials');
  env.controller.sync(env.store, ['twitch'], ['twitch', 'youtube']);
  env.calls[0].resolve(value('twitch')); await flush();
  assert.equal(env.applied.length, 1); assert.equal(env.applied[0].accept(), true);
  assert.equal(env.controller.getSnapshot().twitch.loading, false);
  env.controller.sync(env.store, ['twitch', 'youtube'], ['twitch', 'youtube']);
  assert.deepEqual(env.calls.map(call => call.target), ['twitch', 'youtube']);
  env.controller.sync(env.store, ['twitch', 'youtube'], ['youtube', 'twitch']);
  assert.equal(env.calls.length, 2);
});

test('selection before owner initialization and metadata arrival is filled when both become available', async () => {
  const env = environment();
  env.controller.select('twitch', true);
  env.controller.sync(undefined, ['twitch'], []);
  env.controller.sync(env.store, ['twitch'], []);
  assert.equal(env.calls.length, 0);
  env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.calls[0].resolve(value('twitch')); await flush();
  assert.equal(env.applied.length, 1);
});

test('manual input including clearing a field survives delayed metadata, pending loads and other selections', async () => {
  const before = environment();
  before.controller.select('twitch', true); before.controller.edit('twitch');
  before.controller.sync(before.store, ['twitch'], ['twitch']);
  assert.equal(before.calls.length, 0);
  const pending = environment();
  pending.controller.sync(pending.store, ['twitch'], ['twitch']);
  pending.controller.edit('twitch');
  pending.calls[0].resolve(value('twitch')); await flush();
  pending.controller.sync(pending.store, ['twitch', 'youtube'], ['twitch', 'youtube']);
  assert.equal(pending.applied.length, 0);
  assert.deepEqual(pending.calls.map(call => call.target), ['twitch', 'youtube']);
  assert.equal(pending.controller.getSnapshot().twitch.loading, false);
});

test('deselection and reselection discard the previous request and allow a fresh load', async () => {
  const env = environment();
  env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.controller.select('twitch', false); env.controller.select('twitch', true);
  env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.calls[0].resolve(value('twitch')); await flush();
  assert.equal(env.applied.length, 0);
  env.calls[1].resolve(value('twitch')); await flush();
  assert.equal(env.applied.length, 1);
});

test('deletion invalidates pending reads immediately and stale metadata cannot resurrect them', async () => {
  const env = environment();
  env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.controller.remove('twitch');
  env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.calls[0].resolve(value('twitch')); await flush();
  assert.equal(env.applied.length, 0); assert.equal(env.calls.length, 1);
});

test('failed deletion keeps an immediate manual retry available without waiting for metadata polling', async () => {
  const env = environment(); env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.controller.remove('twitch');
  env.controller.retry('twitch');
  assert.equal(env.calls.length, 2);
  env.calls[0].resolve(value('twitch')); env.calls[1].resolve(value('twitch')); await flush();
  assert.equal(env.applied.length, 1);
});

test('account change, retired store, permission loss and reset reject pending responses', async () => {
  for (const boundary of ['reset', 'retire', 'permission']) {
    const env = environment();
    env.controller.sync(env.store, ['twitch'], ['twitch']);
    if (boundary === 'reset') env.controller.reset();
    if (boundary === 'retire') env.retire();
    if (boundary === 'permission') env.controller.sync(undefined, ['twitch'], []);
    env.calls[0].resolve(value('twitch')); await flush();
    assert.equal(env.applied.length, 0, boundary);
  }
});

test('failure is safe, never retries on polling, and explicit retry can succeed', async () => {
  const env = environment();
  env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.calls[0].reject(new Error('synthetic-secret-in-backend-error')); await flush();
  const failed = env.controller.getSnapshot().twitch;
  assert.equal(failed.loading, false); assert.match(failed.error, /다시/);
  assert.equal(failed.error.includes('synthetic-secret'), false);
  env.controller.sync(env.store, ['twitch'], ['twitch']); assert.equal(env.calls.length, 1);
  env.controller.retry('twitch'); env.calls[1].resolve(value('twitch')); await flush();
  assert.equal(env.applied.length, 1); assert.equal(env.controller.getSnapshot().twitch.error, '');
});

test('invalid saved values never apply and deferred React updates are fenced after manual edits', async () => {
  const invalid = environment(); invalid.controller.sync(invalid.store, ['twitch'], ['twitch']);
  invalid.calls[0].resolve({ ...value('twitch'), stream_key: 'abc#def' }); await flush();
  assert.equal(invalid.applied.length, 0); assert.match(invalid.controller.getSnapshot().twitch.error, /다시/);
  const env = environment(); env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.calls[0].resolve(value('twitch')); await flush();
  const delayedUpdate = env.applied[0];
  env.controller.edit('twitch');
  assert.equal(delayedUpdate.accept(), false, 'a deferred state updater must not overwrite newer input');
});

test('broadcast completion clears inputs without automatically repopulating selected channels', async () => {
  const env = environment(); env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.calls[0].resolve(value('twitch')); await flush();
  env.controller.edit('twitch');
  env.controller.sync(env.store, ['twitch', 'youtube'], ['twitch']);
  assert.equal(env.calls.length, 1);
  env.controller.retry('twitch'); assert.equal(env.calls.length, 2);
});

test('loaded keys return to password mode and UI readiness requires actual valid values', () => {
  const slots = []; let cursor = 0;
  const react = require('../node_modules/react');
  const hooks = { ...react, useId: () => 'synthetic-id', useEffect() {}, useRef(initial) {
    const index = cursor++; return slots[index] ||= { current: initial };
  }, useState: initial => {
    const index = cursor++; if (!(index in slots)) slots[index] = initial;
    return [slots[index], next => { slots[index] = typeof next === 'function' ? next(slots[index]) : next; }];
  } };
  const picker = moduleAt('../app/platform-picker.tsx', name => {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/lib/stream-connections') return connections;
    if (name === '@/lib/watch-links') return watchLinks;
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => String(key) });
    if (name.endsWith('.css')) return {};
    throw new Error(`Unexpected module ${name}`);
  });
  const target = { id: 'twitch', label: 'Twitch', default_server_url: value('twitch').server_url, requires_stream_key: true };
  const controls = { location: 'browser', ready: true, saved: { twitch: { has_stream_key: true } }, loads: {}, error: '', onSave() {}, onUse() {}, onRemove() {} };
  function all(node) {
    if (Array.isArray(node)) return node.flatMap(all);
    if (!node || typeof node !== 'object') return [];
    return [node, ...all(node.props?.children)];
  }
  const tree = picker.default({ catalog: { targets: [target] }, targets: ['twitch'], destinations: {}, maxDestinations: 4, savedConnections: controls });
  const fields = all(tree).find(node => typeof node.type === 'function' && node.type.name === 'ConnectionFields');
  function render(destination, load) {
    cursor = 0;
    return all(fields.type({ ...fields.props, destination, storage: { ...controls, loads: { twitch: load } } }));
  }
  const text = nodes => nodes.flatMap(node => [node.props?.children].flat()).filter(item => typeof item === 'string').join(' ');
  let nodes = render(undefined, { loading: false, error: '', appliedVersion: 0 });
  assert.match(text(nodes), /입력 필요/); assert.doesNotMatch(text(nodes), /입력 완료/);
  nodes.find(node => node.props?.['aria-label'] === 'Twitch 스트림 키 보기').props.onClick();
  nodes = render(undefined, { loading: true, error: '', appliedVersion: 0 });
  assert.match(text(nodes), /연결 불러오는 중/);
  nodes = render(value('twitch'), { loading: false, error: '', appliedVersion: 1 });
  assert.equal(nodes.find(node => node.props?.id === 'key-twitch'), undefined, 'successful auto-load presents its compact summary first');
  nodes.find(node => node.type === 'button' && text([node]).includes('정보 수정')).props.onClick();
  nodes = render(value('twitch'), { loading: false, error: '', appliedVersion: 1 });
  assert.equal(nodes.find(node => node.props?.id === 'key-twitch').props.type, 'password');
  assert.match(text(nodes), /입력 완료/);
});

test('automatic restore preserves the reusable channel while discarding unrelated fields', async () => {
  const env = environment();
  env.controller.sync(env.store, ['twitch'], ['twitch']);
  env.calls[0].resolve({ ...value('twitch'), channel_url: 'https://www.twitch.tv/synthetic', broadcast_url: 'must-not-restore' });
  await flush();
  assert.equal(env.applied[0].connection.channel_url, 'https://www.twitch.tv/synthetic');
  assert.equal(env.applied[0].connection.broadcast_url, undefined);
});
