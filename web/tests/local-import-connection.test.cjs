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
function environment(options = {}) {
  const listeners = new Map();
  const slots = []; let cursor = 0; const effects = []; const requests = []; const connections = []; const downloads = [];
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
  vm.runInNewContext(code, { module: loaded, exports: loaded.exports, AbortController, Error, Date, document: { activeElement: null }, HTMLElement: class {}, requestAnimationFrame: fn => fn(), window: { addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) }, setInterval, clearInterval, setTimeout, clearTimeout, require(name) {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/components/ui/button') return { Button: 'button' };
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => String(key) });
    if (name === '@/lib/helper-release') return { helperRelease: async () => options.release || null };
    if (name === '@/lib/helper-platform') return {
      detectHelperPlatform: async () => options.platform || { os: 'macos', label: 'macOS Apple Silicon', message: 'Mac 감지됨' },
      platformDownloads: release => release?.downloads || [],
      requestHelperDownload: item => { downloads.push(item); return true; },
    };
    if (name.endsWith('.css')) return {};
    throw new Error(name);
  } });
  let props = { onClose() {}, onUpload() {},
    onLoadCode: signal => new Promise((resolve, reject) => requests.push({ signal, resolve, reject })),
    onConnect: async value => { connections.push(value); }, onDisconnect() {} };
  function render(next = {}) { props = { ...props, ...next }; cursor = 0; const tree = loaded.exports.default(props); effects.splice(0).forEach(effect => effect()); return tree; }
  render();
  return { render, requests, connections, listeners, downloads,
    input: () => nodes(render()).find(node => node.type === 'input'),
    button: label => nodes(render()).find(node => node.type === 'button' && (node.props['aria-label'] || text(node.props.children)) === label),
    dispose: () => slots.forEach(slot => slot?.cleanup?.()),
  };
}

test('link popup discovers and pairs without manual information', async () => {
  const env = environment(); await flush(); assert.equal(env.requests.length, 1);
  env.requests[0].resolve('123456ABCDEF'); await flush();
  assert.deepEqual(env.connections, ['123456ABCDEF']);
  assert.equal(env.input(), undefined); env.dispose();
});
test('unmount discards late discovery without creating a local capability', async () => {
  const env = environment(); await flush(); env.dispose();
  assert.equal(env.requests[0].signal.aborted, true);
  env.requests[0].resolve('123456ABCDEF'); await flush();
  assert.equal(env.connections.length, 0);
});
test('unavailable helper has an error and retry uses automatic discovery', async () => {
  const env = environment(); await flush(); env.requests[0].reject(new Error('offline')); await flush();
  assert.match(text(env.render()), /offline/);
  assert.match(text(env.render()), /새 설치 파일은 준비 중/);
  env.button('다시 연결').props.onClick(); await flush();
  env.requests[1].resolve('ABCDEF123456'); await flush();
  assert.deepEqual(env.connections, ['ABCDEF123456']); env.dispose();
});
test('cancel immediately aborts discovery and prevents a late pair or retry', async () => {
  let closes = 0;
  const env = environment(); env.render({ onClose: () => closes++ }); await flush();
  env.button('도우미 연결 닫기').props.onClick();
  assert.equal(closes, 1); assert.equal(env.requests[0].signal.aborted, true);
  env.requests[0].resolve('ABCDEF123456'); await flush();
  env.button('다시 연결').props.onClick(); await flush();
  assert.equal(env.requests.length, 1); assert.equal(env.connections.length, 0); env.dispose();
});
const release = { version: '0.2.0', downloads: [{ label: 'macOS Apple Silicon', url: 'https://example.invalid/mac.zip', sha256: 'a'.repeat(64) }] };
test('only consent downloads the automatically selected package', async () => {
  const env = environment({ release }); await flush(); env.requests[0].reject(new Error('offline')); await flush();
  assert.equal(env.downloads.length, 0);
  const confirm = env.button('동의하고 다운로드'); assert.ok(!confirm.props.disabled);
  confirm.props.onClick(); await flush();
  assert.equal(env.downloads.length, 1); assert.equal(env.downloads[0].label, 'macOS Apple Silicon');
  assert.match(text(env.render()), /설치 후 자동으로 연결돼요/); env.dispose();
});
test('unpublished package cannot download and file upload cancels pending pairing', async () => {
  let uploads = 0;
  const env = environment(); env.render({ onUpload: () => uploads++ }); await flush();
  assert.equal(env.button('동의하고 다운로드'), undefined);
  env.button('MP4 파일 업로드').props.onClick();
  assert.equal(uploads, 1); assert.equal(env.requests[0].signal.aborted, true);
  assert.equal(env.downloads.length, 0); env.dispose();
});
test('unknown CPU never guesses a package or requests manual PC information', async () => {
  const env = environment({ release, platform: { os: 'macos', label: null, message: '자동 선택 불가' } }); await flush();
  assert.equal(env.button('동의하고 다운로드'), undefined);
  assert.equal(nodes(env.render()).some(node => node.type === 'select' || node.type === 'input'), false);
  assert.equal(env.downloads.length, 0); env.dispose();
});

test('offline guidance is concise and connection help starts collapsed', async () => {
  const env = environment(); await flush();
  env.requests[0].reject(new Error('영상 가져오기 도우미를 실행하고 브라우저의 내 컴퓨터 연결 권한을 허용해 주세요.')); await flush();
  const tree = env.render();
  const alert = nodes(tree).find(node => node.props?.role === 'alert');
  assert.equal(text(alert), '실행 중인 도우미에 연결하지 못했어요.');
  assert.equal(nodes(tree).find(node => node.type === 'details').props.open, undefined);
  assert.equal(env.button('취소'), undefined);
  assert.equal(env.button('동의하고 다운로드'), undefined);
  env.dispose();
});
