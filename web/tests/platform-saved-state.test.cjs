/* Metadata-only saved markers and actual field handlers, without a storage backend. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const react = require('../node_modules/react');
const target = { id: 'twitch', label: 'Twitch', default_server_url: 'rtmps://live.twitch.tv:443/app',
  requires_stream_key: true, requires_server_url: true, note: '', setup_url: null };
const youtube = { ...target, id: 'youtube', label: 'YouTube Live' };
const connection = { server_url: target.default_server_url, stream_key: 'synthetic-loaded-key', channel_url: '' };
const metadata = { target: 'twitch', server_url: target.default_server_url, updated_at: 100, has_stream_key: true };
const succeeded = { loading: false, error: '', appliedVersion: 1 };
function nodes(node) { return Array.isArray(node) ? node.flatMap(nodes) : !node || typeof node !== 'object' ? [] : [node, ...nodes(node.props?.children)]; }
const textOf = tree => nodes(tree).flatMap(node => [node.props?.children].flat()).filter(value => typeof value === 'string').join(' ');
const input = tree => nodes(tree).find(node => node.props?.id === 'key-twitch');
const editButton = tree => nodes(tree).find(node => node.type === 'button' && /정보 수정/.test(textOf(node)));
function load(relative, resolve) {
  const filename = path.join(__dirname, relative); const module = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { module, exports: module.exports, require: resolve, URL, TextEncoder }, { filename });
  return module.exports;
}
function environment() {
  let active; const calls = { save: [], use: [], remove: [], change: [], toggle: [] };
  const hooks = { ...react,
    useId() { const index = active.cursor++; return active.slots[index] ||= `synthetic-${active.id}-${index}`; },
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
  const exported = load('../app/platform-picker.tsx', name => {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/lib/stream-connections') return connections;
    if (name === '@/lib/watch-links') return watch;
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => String(key) });
    if (name.endsWith('.css')) return {};
    throw new Error(`Unexpected module ${name}`);
  });
  let nextId = 0;
  function renderer(component, initialProps) {
    const state = { slots: [], cursor: 0, effects: [], updates: 0, id: ++nextId }; let props = initialProps;
    return { render(next) { if (next) props = { ...props, ...next }; let tree; let previous; let repeats = 0;
      do { previous = state.updates; active = state; state.cursor = 0; tree = component(props); state.effects.splice(0).forEach(effect => effect()); }
      while (previous !== state.updates && ++repeats < 10);
      return tree;
    } };
  }
  const storage = (overrides = {}) => ({ location: 'account', ready: true, saved: { twitch: metadata },
    loads: { twitch: succeeded }, error: '', onSave: id => calls.save.push(id), onUse: id => calls.use.push(id),
    onRemove: id => calls.remove.push(id), ...overrides });
  const fields = (overrides = {}) => renderer(exported.ConnectionFields, { target, destination: connection, storage: storage(),
    disabled: false, onChange: value => calls.change.push(value), ...overrides });
  const picker = (overrides = {}) => renderer(exported.default, { catalog: { targets: [target, youtube] }, targets: [], destinations: {},
    maxDestinations: 4, disabled: false, savedConnections: storage(), onToggle: id => calls.toggle.push(id), onDestinationChange() {}, ...overrides });
  return { ...exported, calls, renderer, storage, fields, picker };
}

test('saved markers use metadata alone, preserve platform names, and describe the saved state accessibly', () => {
  const env = environment();
  const saved = { ...metadata };
  Object.defineProperty(saved, 'stream_key', { get() { throw new Error('badge must never read a stream key'); } });
  const ui = env.picker({ savedConnections: env.storage({ saved: { twitch: saved } }) });
  const tree = ui.render(); const button = nodes(tree).find(node => node.props?.id === 'platform-twitch');
  assert.equal(button.props['aria-label'], 'Twitch'); assert.equal(button.props['aria-pressed'], false);
  assert.match(textOf(button), /키\s*저장됨/); assert.ok(nodes(button).some(node => node.type === 'KeyRound'));
  const descriptions = button.props['aria-describedby'].split(/\s+/).map(id => nodes(tree).find(node => node.props?.id === id));
  assert.ok(descriptions.every(Boolean)); assert.ok(descriptions.some(node => /키\s*저장됨/.test(textOf(node))));
  assert.doesNotMatch(textOf(nodes(tree).find(node => node.props?.id === 'platform-youtube')), /키\s*저장됨/);
  assert.deepEqual(env.calls.use, []); assert.deepEqual(env.calls.save, []);
});

test('selection, deselection and readonly mode retain only metadata-backed saved markers', () => {
  const env = environment(); const ui = env.picker();
  for (const props of [{ targets: [] }, { targets: ['twitch'] }, { targets: [] }, { disabled: true }]) {
    const tree = ui.render(props); const button = nodes(tree).find(node => node.props?.id === 'platform-twitch');
    assert.match(textOf(button), /키\s*저장됨/);
    if (props.targets) assert.equal(button.props['aria-pressed'], props.targets.includes('twitch'));
    if (props.disabled) assert.equal(button.props.disabled, true);
  }
  const tree = ui.render({ disabled: false, targets: ['youtube'], destinations: { youtube: connection }, savedConnections: env.storage({ saved: {} }) });
  assert.doesNotMatch(textOf(nodes(tree).find(node => node.props?.id === 'platform-youtube')), /키\s*저장됨/);
  assert.deepEqual(env.calls.use, []);
});

test('deletion and account metadata reset remove a marker despite old valid draft/load state', () => {
  const env = environment(); const ui = env.picker({ targets: ['twitch'], destinations: { twitch: connection } });
  assert.match(textOf(nodes(ui.render()).find(node => node.props?.id === 'platform-twitch')), /키\s*저장됨/);
  for (const savedConnections of [env.storage({ saved: {} }), undefined]) {
    const tree = ui.render({ savedConnections }); const button = nodes(tree).find(node => node.props?.id === 'platform-twitch');
    assert.doesNotMatch(textOf(button), /키\s*저장됨/);
  }
  assert.deepEqual(env.calls.use, []);
});

test('only successful autoload compacts broadcast fields and information editing makes no extra decrypt call', () => {
  const env = environment(); const ui = env.fields(); const tree = ui.render();
  assert.equal(input(tree), undefined); assert.ok(editButton(tree));
  editButton(tree).props.onClick(); const expanded = ui.render();
  assert.equal(input(expanded).props.type, 'password'); assert.equal(input(expanded).props.value, connection.stream_key);
  input(expanded).props.onChange({ target: { value: 'synthetic-edited-key' } });
  const edited = ui.render({ destination: env.calls.change[0] });
  assert.equal(input(edited).props.value, 'synthetic-edited-key'); assert.equal(env.destinationReady(target, env.calls.change[0]), true);
  assert.deepEqual(env.calls.use, []); assert.deepEqual(env.calls.save, []); assert.deepEqual(env.calls.remove, []);
});

test('missing metadata, no applied load, in-progress or failed loading and invalid values never hide editable fields', () => {
  const env = environment();
  const cases = [
    { storage: env.storage({ saved: {} }) },
    { storage: env.storage({ ready: false }) },
    { storage: env.storage({ loads: {} }) },
    { storage: env.storage({ loads: { twitch: { ...succeeded, appliedVersion: 0 } } }) },
    { storage: env.storage({ loads: { twitch: { ...succeeded, loading: true } } }) },
    { storage: env.storage({ loads: { twitch: { ...succeeded, error: '합성 불러오기 오류' } } }) },
    { storage: env.storage({ error: '합성 저장소 오류' }) },
    { destination: { ...connection, stream_key: '' } },
  ];
  for (const props of cases) {
    const tree = env.fields(props).render(); assert.ok(input(tree));
    const error = props.storage?.error || props.storage?.loads?.twitch?.error;
    if (error) assert.ok(textOf(tree).includes(error));
  }
  assert.deepEqual(env.calls.use, []);
});

test('a newly loaded key is masked again after an earlier key was explicitly revealed', () => {
  const env = environment(); const ui = env.fields(); editButton(ui.render()).props.onClick();
  nodes(ui.render()).find(node => node.props?.['aria-label'] === 'Twitch 스트림 키 보기').props.onClick();
  assert.equal(input(ui.render()).props.type, 'text');
  const newer = ui.render({ storage: env.storage({ loads: { twitch: { ...succeeded, appliedVersion: 2 } } }),
    destination: { ...connection, stream_key: 'synthetic-newly-loaded-key' } });
  assert.equal(input(newer), undefined); editButton(newer).props.onClick();
  assert.equal(input(ui.render()).props.type, 'password'); assert.deepEqual(env.calls.use, []);
});

test('account connection editing always exposes fields without broadcast URL or nested storage controls', () => {
  const env = environment(); const tree = env.fields({ purpose: 'connection' }).render();
  assert.ok(input(tree)); assert.equal(input(tree).props.disabled, false);
  assert.ok(!nodes(tree).some(node => node.props?.id === 'broadcast-url-twitch'));
  for (const label of ['Twitch 연결 저장', 'Twitch 저장한 연결 불러오기', 'Twitch 저장 삭제']) {
    assert.ok(!nodes(tree).some(node => node.props?.['aria-label'] === label));
  }
  assert.ok(!editButton(tree)); assert.deepEqual(env.calls.use, []);
  const readonly = env.fields({ purpose: 'connection', disabled: true }).render();
  assert.equal(input(readonly).props.disabled, true);
});
