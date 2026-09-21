/* Real component handlers with deferred discovery responses. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const react = require('../node_modules/react');
const flush = () => new Promise(resolve => setTimeout(resolve, 5));
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/local-import-connection.tsx'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022,
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
function nodes(node) { return Array.isArray(node) ? node.flatMap(nodes) : !node || typeof node !== 'object' ? [] : [node, ...nodes(node.props?.children)]; }
const text = node => [node].flat().map(value => typeof value === 'string' ? value : value?.props ? text(value.props.children) : '').join('');
function environment() {
  const listeners = new Map();
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
  vm.runInNewContext(code, { module: loaded, exports: loaded.exports, AbortController, Date, window: { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }, setInterval, clearInterval, setTimeout, clearTimeout, require(name) {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/components/ui/button') return { Button: 'button' };
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => String(key) });
    if (name === '@/lib/helper-release') return { helperRelease: async () => null };
    if (name.endsWith('.css')) return {};
    throw new Error(name);
  } });
  let props = { connected: false, disabled: false,
    onLoadCode: signal => new Promise((resolve, reject) => requests.push({ signal, resolve, reject })),
    onConnect: async value => { connections.push(value); }, onDisconnect() {} };
  function render(next = {}) { props = { ...props, ...next }; cursor = 0; const tree = loaded.exports.default(props); effects.splice(0).forEach(effect => effect()); return tree; }
  render();
  return { render, requests, connections, listeners,
    input: () => nodes(render()).find(node => node.type === 'input'),
    button: label => nodes(render()).find(node => node.type === 'button' && text(node.props.children) === label),
    dispose: () => slots.forEach(slot => slot?.cleanup?.()),
  };
}

test('login auto-discovers and pairs without an extra connect click', async () => {
  const env = environment(); await flush(); assert.equal(env.requests.length, 1);
  env.requests[0].resolve('123456ABCDEF'); await flush();
  assert.deepEqual(env.connections, ['123456ABCDEF']);
  assert.equal(env.input().props.value, '');
  env.dispose();
});

test('unmount discards late discovery without creating a local capability', async () => {
  const env = environment(); await flush(); env.dispose();
  assert.equal(env.requests[0].signal.aborted, true);
  env.requests[0].resolve('123456ABCDEF'); await flush();
  assert.equal(env.connections.length, 0);
});

test('unavailable helper explains installation and manual retry succeeds', async () => {
  const env = environment(); await flush(); env.requests[0].reject(new Error('offline')); await flush();
  const tree = env.render();
  assert.match(text(nodes(tree).find(node => node.props?.role === 'alert')), /설치 여부와 실행 상태/);
  assert.ok(nodes(tree).find(node => node.props?.href === 'replay-live-helper://start'));
  assert.match(text(tree), /설치 파일은 아직 배포 준비 중/);
  env.button('다시 확인').props.onClick(); await flush();
  env.requests[1].resolve('ABCDEF123456'); await flush();
  assert.deepEqual(env.connections, ['ABCDEF123456']);
  env.dispose();
});

test('explicit disconnect prevents silent reconnect on focus and permits deliberate retry', async () => {
  const env = environment(); await flush(); env.requests[0].resolve('123456ABCDEF'); await flush();
  env.render({ connected: true });
  env.button('연결 해제').props.onClick();
  env.render({ connected: false });
  assert.equal(env.listeners.has('focus'), false);
  assert.equal(env.requests.length, 1);
  env.button('다시 확인').props.onClick(); await flush(); env.render();
  assert.equal(env.requests.length, 2); // no duplicate effect request
  env.requests[1].resolve('ABCDEF123456'); await flush();
  env.dispose();
});

test('reload gets a fresh code and disabled operations do not start pairing', async () => {
  const first = environment(); first.dispose();
  const second = environment(); await flush(); assert.equal(second.requests.length, 1);
  second.render({ disabled: true });
  second.requests[0].resolve('123456ABCDEF'); await flush();
  assert.equal(second.connections.length, 0);
  second.dispose();
});
