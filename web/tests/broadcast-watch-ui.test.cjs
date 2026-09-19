/* Synthetic component state and deferred media metadata only. No account or network access. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const react = require('../node_modules/react');
function load(relative, requireModule = () => ({})) {
  const filename = path.join(__dirname, relative);
  const module = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { module, exports: module.exports, require: requireModule, URL, TextEncoder }, { filename });
  return module.exports;
}
const watch = load('../lib/watch-links.ts');
const connections = load('../lib/stream-connections.ts', name => name === './watch-links' ? watch : {});
const { createMediaSelection } = load('../app/media-selection.ts');
const plain = value => JSON.parse(JSON.stringify(value));
function component(relative) {
  const slots = []; let cursor = 0;
  const exported = load(relative, name => {
    if (name === 'react') return { ...react, useId: () => 'synthetic-id', useEffect() {}, useRef(initial) {
      const index = cursor++; return slots[index] ||= { current: initial };
    }, useState(initial) {
      const index = cursor++; if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }];
    } };
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/lib/watch-links') return watch;
    if (name === '@/lib/stream-connections') return connections;
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => String(key) });
    if (name.endsWith('.css')) return {};
    throw new Error(`Unexpected module ${name}`);
  });
  return { ...exported, render(props, Component = exported.default) { cursor = 0; return Component(props); } };
}
function all(node) {
  if (Array.isArray(node)) return node.flatMap(all);
  if (!node || typeof node !== 'object') return [];
  return [node, ...all(node.props?.children)];
}
const textOf = tree => all(tree).flatMap(node => [node.props?.children].flat()).filter(value => typeof value === 'string').join(' ');
const job = { id: 'synthetic-job', target: 'twitch', title: 'Synthetic previous broadcast', channel_url: 'https://twitch.tv/fixture', broadcast_url: 'https://www.twitch.tv/videos/12345' };

test('history prefers an exact broadcast link, safely falls back to the channel, and rejects unsafe/local links', () => {
  const ui = component('../app/broadcast-watch-links.tsx');
  assert.deepEqual(plain(ui.broadcastWatchLink('twitch', job)), { url: job.broadcast_url, kind: 'broadcast', label: '방송 보기' });
  const channel = ui.broadcastWatchLink('twitch', { ...job, broadcast_url: 'javascript:alert(1)' });
  assert.equal(channel.kind, 'channel'); assert.equal(channel.url, 'https://www.twitch.tv/fixture');
  assert.equal(ui.broadcastWatchLink('local', job), undefined);
  assert.equal(ui.broadcastWatchLink('twitch', { channel_url: 'https://evil.example/fixture' }), undefined);
  assert.equal(ui.broadcastWatchLink('twitch', { broadcast_url: 'https://www.twitch.tv/videos/123?token=synthetic' }), undefined);
});

test('history links open safely, viewers cannot edit, and channel fallback explains that it is not this archived broadcast', () => {
  const ui = component('../app/broadcast-watch-links.tsx');
  const tree = ui.render({ job: { ...job, broadcast_url: '' }, canEdit: false, disabled: false, onSave: async () => {} });
  const link = all(tree).find(node => node.type === 'a');
  assert.equal(link.props.target, '_blank'); assert.equal(link.props.rel, 'noopener noreferrer');
  assert.equal(all(tree).some(node => node.type === 'button'), false);
  assert.match(textOf(tree), /현재 채널 화면/); assert.match(textOf(tree), /녹화본과 다를 수/);
  assert.equal(ui.render({ job: { ...job, target: 'local' }, canEdit: true, disabled: false, onSave: async () => {} }), null);
});

test('history editing keeps its draft across metadata polling and supports clearing both URLs without touching saved connections', async () => {
  const ui = component('../app/broadcast-watch-links.tsx');
  const submitted = [];
  let props = { job, canEdit: true, disabled: false, onSave: async links => { submitted.push(links); } };
  all(ui.render(props)).find(node => node.type === 'button').props.onClick();
  props = { ...props, job: { ...job, channel_url: 'https://twitch.tv/changedbyanotherrequest' } };
  let tree = ui.render(props);
  assert.equal(all(tree).find(node => node.props?.id === 'history-channel-synthetic-job').props.value, job.channel_url);
  for (const id of ['history-channel-synthetic-job', 'history-broadcast-synthetic-job']) {
    all(tree).find(node => node.props?.id === id).props.onChange({ target: { value: '' } });
    tree = ui.render(props);
  }
  await all(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {} });
  assert.deepEqual(plain(submitted), [{ channel_url: '', broadcast_url: '' }]);
  assert.equal(all(ui.render(props)).some(node => node.type === 'form'), false);
});

test('channel and broadcast URL inputs survive server/key edits, and invalid optional URLs block readiness', () => {
  const ui = component('../app/platform-picker.tsx');
  const target = { id: 'twitch', label: 'Twitch', default_server_url: 'rtmps://stream.example.com:443/app', requires_stream_key: true };
  const destination = { server_url: target.default_server_url, stream_key: 'synthetic-key', channel_url: job.channel_url, broadcast_url: job.broadcast_url };
  assert.equal(ui.destinationReady(target, destination), true);
  assert.equal(ui.destinationReady(target, { ...destination, broadcast_url: 'https://kick.com/fixture' }), false);
  const edits = [];
  const picker = ui.render({ catalog: { targets: [target] }, targets: ['twitch'], destinations: { twitch: destination }, maxDestinations: 4, onDestinationChange: (_, value) => edits.push(value) });
  const fields = all(picker).find(node => typeof node.type === 'function' && node.type.name === 'ConnectionFields');
  const tree = ui.render(fields.props, fields.type);
  all(tree).find(node => node.props?.id === 'key-twitch').props.onChange({ target: { value: 'new-synthetic-key' } });
  assert.equal(edits[0].channel_url, job.channel_url); assert.equal(edits[0].broadcast_url, job.broadcast_url);
  all(tree).find(node => node.props?.id === 'server-twitch').props.onChange({ target: { value: target.default_server_url } });
  assert.equal(edits[1].channel_url, job.channel_url); assert.equal(edits[1].broadcast_url, job.broadcast_url);
  assert.match(textOf(tree), /연결에는 저장하지 않습니다/);
});

test('empty viewing links preserve the exact legacy single and batch request bytes for idempotent retries', () => {
  const ui = component('../app/platform-picker.tsx');
  const target = { id: 'twitch', default_server_url: 'rtmps://stream.example.com:443/app' };
  const common = { media_id: 'fixture-media', title: 'Fixture', scheduled_at: null };
  const legacy = { target: 'twitch', server_url: target.default_server_url, stream_key: 'synthetic-key' };
  for (const links of [{}, { channel_url: '', broadcast_url: '' }, { channel_url: '  ', broadcast_url: ' ' }]) {
    const destination = ui.broadcastDestination(target, { stream_key: 'synthetic-key', ...links });
    assert.equal(JSON.stringify({ ...common, ...destination }), JSON.stringify({ ...common, ...legacy }));
    assert.equal(JSON.stringify({ ...common, destinations: [destination] }), JSON.stringify({ ...common, destinations: [legacy] }));
  }
  const withLink = ui.broadcastDestination(target, { stream_key: 'synthetic-key', channel_url: job.channel_url });
  assert.equal(withLink.channel_url, 'https://www.twitch.tv/fixture');
  assert.equal(Object.hasOwn(withLink, 'broadcast_url'), false);
});

test('new upload B becomes selected only after B is ready, never because previous A was already ready', () => {
  const selection = createMediaSelection();
  const ticket = selection.begin('synthetic-user');
  selection.register(ticket, 'B');
  const generation = selection.capture();
  assert.equal(selection.reconcile([{ id: 'A', status: 'ready' }, { id: 'B', status: 'validating' }], 'synthetic-user', generation).kind, 'pending');
  assert.deepEqual(plain(selection.reconcile([{ id: 'A', status: 'ready' }, { id: 'B', status: 'ready' }], 'synthetic-user', generation)), { kind: 'ready', id: 'B' });
});

test('upload/import completions cannot cross a new selection, newer preparation, or account reset', () => {
  const selection = createMediaSelection();
  const old = selection.begin('old-user'); const oldGeneration = selection.capture();
  selection.select(); assert.equal(selection.register(old, 'old-file'), false);
  assert.equal(selection.reconcile([{ id: 'old-file', status: 'ready' }], 'old-user', oldGeneration).kind, 'stale');
  const earlier = selection.begin('old-user'); const latest = selection.begin('old-user');
  assert.equal(selection.register(earlier, 'earlier-file'), false); assert.equal(selection.register(latest, 'latest-file'), true);
  assert.equal(selection.reconcile([{ id: 'latest-file', status: 'ready' }], 'new-user', selection.capture()).kind, 'stale');
  selection.reset(); assert.equal(selection.register(latest, 'latest-file'), false);
});

test('failed upload does not silently reselect an older recording and initial library fallback remains available', () => {
  const selection = createMediaSelection();
  assert.equal(selection.reconcile([], 'synthetic-user', selection.capture()).allowFirst, true);
  const ticket = selection.begin('synthetic-user'); selection.register(ticket, 'B');
  const media = [{ id: 'A', status: 'ready' }, { id: 'B', status: 'failed' }];
  assert.equal(selection.reconcile(media, 'synthetic-user', selection.capture()).kind, 'failed');
  assert.equal(selection.reconcile(media, 'synthetic-user', selection.capture()).allowFirst, false);
});
