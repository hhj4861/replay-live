/* Real component handlers with deferred discovery responses. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const react = require('../node_modules/react');
const flush = () => new Promise(resolve => setImmediate(resolve));
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/local-import-connection.tsx'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
function nodes(node) { return Array.isArray(node) ? node.flatMap(nodes) : !node || typeof node !== 'object' ? [] : [node, ...nodes(node.props?.children)]; }
const text = node => [node].flat().map(value => typeof value === 'string' ? value : value?.props ? text(value.props.children) : '').join('');
function environment() {
  const slots = []; let cursor = 0; const effects = []; const requests = []; const connections = [];
  const hooks = { ...react,
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = initial; return [slots[i], value => { slots[i] = value; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial }; },
    useCallback(callback, deps) { const i = cursor++; const old = slots[i];
      if (!old || deps.some((value, j) => value !== old.deps[j])) slots[i] = { deps, callback };
      return slots[i].callback;
    },
    useEffect(effect, deps) { const i = cursor++; const old = slots[i];
      if (!old || deps.some((value, j) => value !== old.deps[j])) effects.push(() => { old?.cleanup?.(); slots[i] = { deps, cleanup: effect() }; });
    },
  };
  const loaded = { exports: {} };
  vm.runInNewContext(code, { module: loaded, exports: loaded.exports, AbortController, require(name) {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/components/ui/button') return { Button: 'button' };
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => String(key) });
    if (name.endsWith('.css')) return {};
    throw new Error(name);
  } });
  let props = { connected: false, busy: false, disabled: false,
    onLoadCode: signal => new Promise((resolve, reject) => requests.push({ signal, resolve, reject })),
    onConnect: async value => { connections.push(value); }, onDisconnect() {} };
  function render(next = {}) { props = { ...props, ...next }; cursor = 0; const tree = loaded.exports.default(props); effects.splice(0).forEach(effect => effect()); return tree; }
  render();
  return { render, requests, connections,
    input: () => nodes(render()).find(node => node.type === 'input'),
    button: label => nodes(render()).find(node => node.type === 'button' && text(node.props.children) === label),
    dispose: () => slots.forEach(slot => slot?.cleanup?.()),
  };
}

test('initial mount fills the code, button fetches fresh value, and connection stays explicit', async () => {
  const env = environment(); assert.equal(env.requests.length, 1);
  assert.equal(env.button('내 컴퓨터 연결').props.disabled, true);
  env.requests[0].resolve('123456ABCDEF'); await flush();
  assert.equal(env.input().props.value, '123456ABCDEF');
  assert.equal(env.connections.length, 0);
  env.button('코드 불러오기').props.onClick();
  assert.equal(env.input().props.value, '');
  env.requests[1].resolve('ABCDEF123456'); await flush();
  await env.button('내 컴퓨터 연결').props.onClick();
  assert.deepEqual(env.connections, ['ABCDEF123456']);
});

test('manual input and unmount discard delayed auto-load results', async () => {
  const env = environment();
  env.input().props.onChange({ target: { value: 'manual-code' } });
  assert.equal(env.requests[0].signal.aborted, true);
  env.requests[0].resolve('123456ABCDEF'); await flush();
  assert.equal(env.input().props.value, 'manual-code');
  env.button('코드 불러오기').props.onClick(); env.dispose();
  assert.equal(env.requests[1].signal.aborted, true);
  env.requests[1].resolve('123456ABCDEF'); await flush();
  assert.equal(env.input().props.value, '');
});

test('unavailable helper shows inline recovery and retries successfully', async () => {
  const env = environment(); env.requests[0].reject(new Error('offline')); await flush();
  const alert = nodes(env.render()).find(node => node.props?.role === 'alert');
  assert.match(text(alert), /연결 코드를 불러오지 못했습니다/);
  assert.equal(env.button('코드 불러오기').props.disabled, false);
  assert.equal(env.button('내 컴퓨터 연결').props.disabled, true);
  env.button('코드 불러오기').props.onClick();
  env.requests[1].resolve('123456ABCDEF'); await flush();
  assert.equal(nodes(env.render()).some(node => node.props?.role === 'alert'), false);
  assert.equal(env.button('내 컴퓨터 연결').props.disabled, false);
});

test('remount after page reload or sign-in always fetches and disconnect refreshes the code', async () => {
  const before = environment(); before.requests[0].resolve('123456ABCDEF'); await flush(); before.dispose();
  const after = environment(); assert.equal(after.input().props.value, ''); assert.equal(after.requests.length, 1);
  after.requests[0].resolve('ABCDEF123456'); await flush();
  assert.equal(after.input().props.value, 'ABCDEF123456');
  after.render({ connected: true }); after.render({ connected: false });
  assert.equal(after.requests.length, 2);
});
