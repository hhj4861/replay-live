/* Real editor handlers + shared field validation; deferred synthetic calls only. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const react = require('../node_modules/react');
const plain = value => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));
const target = { id: 'twitch', label: 'Twitch', default_server_url: 'rtmps://live.twitch.tv:443/app',
  requires_stream_key: true, requires_server_url: true, note: '', setup_url: null };
const youtube = { ...target, id: 'youtube', label: 'YouTube', default_server_url: 'rtmps://a.rtmps.youtube.com:443/live2' };
const source = { server_url: target.default_server_url, stream_key: 'synthetic-original-key', channel_url: 'https://www.twitch.tv/synthetic_channel', broadcast_url: 'https://www.twitch.tv/videos/123' };
function nodes(node) { return Array.isArray(node) ? node.flatMap(nodes) : !node || typeof node !== 'object' ? [] : [node, ...nodes(node.props?.children)]; }
const textOf = tree => nodes(tree).flatMap(node => [node.props?.children].flat()).filter(value => typeof value === 'string').join(' ');
function load(relative, resolve) {
  const filename = path.join(__dirname, relative); const module = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { module, exports: module.exports, require: resolve, URL, TextEncoder }, { filename });
  return module.exports;
}
function environment() {
  let active; let identity = 'synthetic-owner';
  const loads = []; const saves = []; const saved = []; const cancelled = [];
  const hooks = { ...react,
    useState(initial) { const index = active.cursor++; const state = active;
      if (!(index in state.slots)) state.slots[index] = typeof initial === 'function' ? initial() : initial;
      return [state.slots[index], value => { state.slots[index] = typeof value === 'function' ? value(state.slots[index]) : value; state.updates++; }]; },
    useRef(initial) { const index = active.cursor++; return active.slots[index] ||= { current: initial }; },
    useEffect(effect, deps) { const index = active.cursor++; const state = active; const previous = state.slots[index];
      if (!previous || !deps || !previous.deps || deps.some((value, i) => value !== previous.deps[i])) {
        state.effects.push(() => { previous?.cleanup?.(); state.slots[index] = { deps, cleanup: effect() }; });
      } },
  };
  const watch = load('../lib/watch-links.ts', () => ({}));
  const connections = load('../lib/stream-connections.ts', name => name === './watch-links' ? watch : {});
  function resolve(name) {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/lib/api') return { sessionIdentity: () => identity };
    if (name === '@/lib/watch-links') return watch;
    if (name === '@/lib/stream-connections') return connections;
    if (name === '@/components/ui/button') return { Button: 'button' };
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => String(key) });
    if (name.endsWith('.css')) return {};
    throw new Error(`Unexpected module ${name}`);
  }
  const platforms = load('../app/platform-picker.tsx', resolve);
  const editor = load('../app/connection-editor.tsx', name => name === './platform-picker' ? platforms : resolve(name));
  function renderer(component, initialProps) {
    const state = { slots: [], cursor: 0, effects: [], updates: 0 }; let props = initialProps;
    return {
      render(next) { if (next) props = { ...props, ...next }; active = state; state.cursor = 0;
        const tree = component(props); state.effects.splice(0).forEach(effect => effect()); return tree; },
      settle(next) { let tree = this.render(next); let previous; let count = 0;
        do { previous = state.updates; tree = this.render(); } while (state.updates !== previous && ++count < 10);
        return tree; },
      dispose() { state.slots.forEach(slot => slot?.cleanup?.()); },
      get updates() { return state.updates; },
    };
  }
  const props = { identity, targets: [target, youtube, { id: 'local', label: '파일 테스트', requires_stream_key: false }],
    connections: [], location: 'account', disabled: false,
    onLoad: id => new Promise((resolve, reject) => loads.push({ id, resolve, reject })),
    onSave: (id, value) => new Promise((resolve, reject) => saves.push({ id, value, resolve, reject })),
    onSaved: id => saved.push(id), onCancel: () => cancelled.push(true) };
  function form(existing = false, extra = {}) {
    const root = renderer(editor.default, { ...props, initialTarget: 'twitch', connections: existing ? [{ target: 'twitch' }] : [], ...extra });
    const node = root.settle();
    assert.equal(node.type.name, 'ConnectionForm');
    return renderer(node.type, node.props);
  }
  const fieldsNode = ui => nodes(ui.settle()).find(node => node.type === platforms.ConnectionFields);
  const edit = (ui, changes) => { const node = fieldsNode(ui); assert.ok(node, 'editable fields are available'); node.props.onChange({ ...node.props.destination, ...changes }); };
  const submit = ui => { const form = nodes(ui.settle()).find(node => node.type === 'form'); assert.ok(form, 'save form is available'); form.props.onSubmit({ preventDefault() {} }); };
  return { props, platforms, renderer, root: extra => renderer(editor.default, { ...props, ...extra }), form, fieldsNode, edit, submit,
    loads, saves, saved, cancelled, switchAccount: () => { identity = 'other-owner'; } };
}

test('account editor offers only live platforms and selecting a new platform does not decrypt anything', () => {
  const env = environment(); const root = env.root(); const tree = root.settle();
  const options = nodes(tree).filter(node => node.type === 'button' && node.props['aria-label']);
  assert.deepEqual(options.map(node => node.props['aria-label']), ['Twitch 연결 추가', 'YouTube 연결 추가']);
  options[0].props.onClick(); const node = root.settle();
  const form = env.renderer(node.type, node.props); form.settle();
  assert.equal(env.loads.length, 0); assert.equal(env.saves.length, 0);
});

test('failed save preserves the exact draft, shows a generic error and allows an explicit retry', async () => {
  const env = environment(); const ui = env.form(); ui.settle();
  env.edit(ui, source); env.submit(ui);
  assert.equal(env.saves.length, 1);
  env.saves[0].reject(new Error('upstream synthetic-secret-do-not-echo')); await flush();
  const fields = env.fieldsNode(ui);
  assert.deepEqual(plain(fields.props.destination), { server_url: source.server_url, stream_key: source.stream_key, channel_url: source.channel_url });
  assert.match(textOf(ui.settle()), /입력한 내용은 유지됩니다/);
  assert.doesNotMatch(textOf(ui.settle()), /synthetic-secret-do-not-echo/);
  env.submit(ui); assert.equal(env.saves.length, 2);
  env.saves[1].resolve(); await flush(); assert.deepEqual(env.saved, ['twitch']);
  assert.equal(env.fieldsNode(ui).props.destination.stream_key, '');
});

test('two submit events before a React rerender make one save and disable cancellation while saving', async () => {
  const env = environment(); const ui = env.form(); ui.settle(); env.edit(ui, source);
  const form = nodes(ui.settle()).find(node => node.type === 'form');
  form.props.onSubmit({ preventDefault() {} }); form.props.onSubmit({ preventDefault() {} });
  assert.equal(env.saves.length, 1);
  const tree = ui.settle();
  assert.ok(nodes(tree).filter(node => node.type === 'button' && ['취소', '저장 중…'].some(text => textOf(node).includes(text))).every(node => node.props.disabled));
  env.saves[0].resolve(); await flush(); assert.equal(env.saved.length, 1);
});

test('editing loads once, masks the stored key and excludes one-time broadcast URLs from fields and save payload', async () => {
  const env = environment(); const ui = env.form(true); ui.settle();
  assert.equal(env.loads.length, 1); assert.equal(env.fieldsNode(ui), undefined);
  env.loads[0].resolve(source); await flush();
  const node = env.fieldsNode(ui); assert.equal(node.props.purpose, 'connection');
  const fields = env.renderer(node.type, node.props); const tree = fields.settle();
  assert.equal(nodes(tree).find(node => node.props?.id === 'key-twitch').props.type, 'password');
  assert.ok(nodes(tree).some(node => node.props?.id === 'channel-url-twitch'));
  assert.ok(!nodes(tree).some(node => node.props?.id === 'broadcast-url-twitch'));
  assert.equal(Object.hasOwn(node.props.destination, 'broadcast_url'), false);
  ui.settle({ onLoad: () => { throw new Error('polling must not reload'); } }); assert.equal(env.loads.length, 1);
  env.submit(ui); assert.deepEqual(plain(env.saves[0].value), { server_url: source.server_url, stream_key: source.stream_key, channel_url: source.channel_url });
  assert.equal(Object.hasOwn(env.saves[0].value, 'broadcast_url'), false);
  env.saves[0].resolve(); await flush(); ui.dispose();
});

for (const boundary of ['cancel', 'account change', 'platform change']) test(`late load is discarded after ${boundary}`, async () => {
  const env = environment(); const ui = env.form(true); ui.settle();
  if (boundary === 'account change') env.switchAccount();
  else {
    if (boundary === 'cancel') nodes(ui.settle()).find(node => node.type === 'button' && textOf(node) === '취소').props.onClick();
    ui.dispose();
  }
  const updates = ui.updates;
  env.loads[0].resolve(source); await flush();
  assert.equal(ui.updates, updates); assert.equal(env.saves.length, 0); assert.deepEqual(env.saved, []);
  if (boundary === 'cancel') assert.deepEqual(env.cancelled, [true]);
});

test('a late successful save cannot close or notify a replacement account or an unmounted editor', async () => {
  for (const boundary of ['account', 'unmount']) {
    const env = environment(); const ui = env.form(); ui.settle(); env.edit(ui, source); env.submit(ui);
    if (boundary === 'account') env.switchAccount(); else ui.dispose();
    const updates = ui.updates; env.saves[0].resolve(); await flush();
    assert.deepEqual(env.saved, []); assert.equal(ui.updates, updates);
  }
});

test('saved metadata arriving after manual input does not overwrite the new connection draft', async () => {
  const env = environment(); const ui = env.form(); ui.settle(); env.edit(ui, { ...source, stream_key: 'synthetic-new-user-input' });
  ui.settle({ existing: true });
  if (env.loads.length) { env.loads[0].resolve(source); await flush(); }
  assert.equal(env.fieldsNode(ui).props.destination.stream_key, 'synthetic-new-user-input');
});
