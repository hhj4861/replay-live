/* Synthetic member records and deferred requests; no credentials or external calls. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const react = require('../node_modules/react');
const { renderToStaticMarkup } = require('../node_modules/react-dom/server');
const plain = value => JSON.parse(JSON.stringify(value));
function load(relative, resolve) {
  const filename = path.join(__dirname, relative); const module = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  vm.runInNewContext(code, { module, exports: module.exports, require: resolve, AbortSignal, AbortController }, { filename });
  return module.exports;
}
const member = { id: 'synthetic-member', email: 'member@example.test', enabled: true, roles: ['admin'],
  created_at: 100, updated_at: 200, stream_connection_count: 1, active_session_count: 2 };
const detail = { ...member, active_jobs_count: 1, connections: [{ target: 'twitch', updated_at: 200, has_stream_key: true }] };
function environment() {
  let identity = 'owner-one'; const calls = [];
  const request = (path, init) => new Promise((resolve, reject) => calls.push({ path, init, resolve, reject }));
  const exported = load('../lib/member-management.ts', () => ({ api: request, request, assertSessionIdentity(expected) {
    if (expected !== identity) throw new Error('identity changed');
  } }));
  return { ...exported, client: exported.createMemberManagementClient(identity), calls, switchAccount: () => { identity = 'owner-two'; } };
}

test('tenant admin and a permission flag alone cannot expose site administration', () => {
  const env = environment();
  assert.equal(env.canManageMembers(), false);
  for (const account of [{ roles: ['admin'] }, { roles: ['admin'], permissions: { manage_members: true } },
    { roles: ['site_admin'], permissions: { manage_members: false } }]) assert.equal(env.canManageMembers(account), false);
  assert.equal(env.canManageMembers({ roles: ['admin', 'site_admin'], permissions: { manage_members: true } }), true);
});

test('member search is bounded, encoded, paged and never cached', async () => {
  const env = environment(); const pending = env.client.list('  a+b&admin@example.test  ', 25);
  assert.equal(env.calls[0].path, '/admin/members?q=a%2Bb%26admin%40example.test&offset=25&limit=25');
  assert.equal(env.calls[0].init.cache, 'no-store');
  assert.ok(env.calls[0].init.signal instanceof AbortSignal);
  env.calls[0].resolve({ items: [member], total: 26, offset: 25, limit: 25, secret: 'synthetic-extra' });
  assert.deepEqual(plain(await pending), { items: [member], total: 26, offset: 25, limit: 25 });
  await assert.rejects(env.client.list('x'.repeat(255)), /회원 정보/);
  await assert.rejects(env.client.list('', -1), /회원 정보/);
  assert.equal(env.calls.length, 1);
});

test('admin metadata discards server URLs, plaintext, ciphertext and unrecognized record fields', async () => {
  const env = environment(); const pending = env.client.detail(member.id);
  env.calls[0].resolve({ ...detail, stream_key: 'synthetic-plaintext', ciphertext: 'synthetic-ciphertext',
    connections: [{ ...detail.connections[0], stream_key: 'synthetic-plaintext', server_url: 'rtmps://synthetic.example/app',
      channel_url: 'https://synthetic.example/channel', encrypted: 'synthetic-ciphertext' }] });
  const result = await pending;
  assert.deepEqual(plain(result), detail);
  assert.doesNotMatch(JSON.stringify(result), /synthetic-plaintext|synthetic-ciphertext|rtmps:|channel_url/);
});

test('retired clients cannot start requests or apply responses belonging to a previous login', async () => {
  const env = environment(); const pending = env.client.detail(member.id); env.switchAccount();
  env.calls[0].resolve(detail); await assert.rejects(pending, /identity changed/);
  await assert.rejects(env.client.list(''), /identity changed/);
  await assert.rejects(env.client.setEnabled(member.id, false), /identity changed/);
  await assert.rejects(env.client.revokeSessions(member.id), /identity changed/);
  await assert.rejects(env.client.removeConnection(member.id, 'twitch'), /identity changed/);
  assert.equal(env.calls.length, 1);
});

test('status/session/delete actions use dedicated metadata routes and bounded bodies', async () => {
  const env = environment();
  const status = env.client.setEnabled(member.id, false);
  assert.equal(env.calls[0].path, '/admin/members/synthetic-member/status');
  assert.equal(env.calls[0].init.method, 'PUT'); assert.equal(env.calls[0].init.body, '{"enabled":false}');
  env.calls[0].resolve({ ...detail, enabled: false }); assert.equal((await status).enabled, false);
  const revoke = env.client.revokeSessions(member.id);
  assert.equal(env.calls[1].path, '/admin/members/synthetic-member/revoke-sessions');
  assert.equal(env.calls[1].init.method, 'POST'); assert.equal(env.calls[1].init.body, '{}');
  env.calls[1].resolve({ revoked: true }); await revoke;
  const remove = env.client.removeConnection(member.id, 'twitch');
  assert.equal(env.calls[2].path, '/admin/members/synthetic-member/stream-connections/twitch');
  assert.equal(env.calls[2].init.method, 'DELETE'); assert.equal(env.calls[2].init.cache, 'no-store');
  assert.equal(env.calls[2].init.body, undefined);
  env.calls[2].resolve({ status: 204 }); await remove;
  assert.ok(env.calls.every(call => !call.path.endsWith('/use')));
});

test('path injection and malformed metadata fail without retaining their content', async () => {
  const env = environment();
  await assert.rejects(env.client.detail('../secrets'), /회원 정보/);
  await assert.rejects(env.client.removeConnection(member.id, 'twitch/use'), /회원 정보/);
  assert.equal(env.calls.length, 0);
  const pending = env.client.detail(member.id);
  env.calls[0].resolve({ ...detail, connections: [{ target: 'twitch', stream_key: 'synthetic-do-not-echo' }] });
  await assert.rejects(pending, error => !error.message.includes('synthetic-do-not-echo') && /회원 정보/.test(error.message));
});

test('connection deletion is fenced even when the account changes during its response', async () => {
  const env = environment(); const pending = env.client.removeConnection(member.id, 'twitch');
  env.switchAccount(); env.calls[0].resolve({ status: 204 }); await assert.rejects(pending, /identity changed/);
});

function render(props) {
  const env = environment();
  const { default: Component } = load('../app/member-management.tsx', name => {
    if (name === 'react') return react;
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/lib/member-management') return env;
    if (name === '@/lib/api') return { sessionIdentity: () => 'owner-one' };
    if (name === '@/components/ui/button') return { Button: ({ variant: _variant, ...props }) => react.createElement('button', props) };
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === './connection-editor') return { default: () => null };
    if (name === './platform-picker') return { PlatformMark: () => react.createElement('span', { 'aria-hidden': true }) };
    if (name === 'lucide-react') return new Proxy({}, { get: () => () => react.createElement('svg') });
    if (name.endsWith('.css')) return {};
    throw new Error(`Unexpected module ${name}`);
  });
  return renderToStaticMarkup(react.createElement(Component, { identity: 'owner-one', mode: 'account',
    account: { tenant_id: member.id, subject: 'synthetic-subject', roles: ['admin'], profile: member }, targets: [{ id: 'twitch', label: 'Twitch' }],
    connections: [], location: 'account', connectionError: '', canEditConnections: true, disabled: false,
    onClose() {}, onUseConnection() {}, onRemoveConnection: async () => {}, onSessionRevoked() {}, ...props }));
}

test('my account explains encrypted storage and uses metadata without rendering a supplied secret', () => {
  const html = render({ connections: [{ target: 'twitch', updated_at: 200, has_stream_key: true,
    stream_key: 'synthetic-never-display', server_url: 'rtmps://synthetic-hidden.example/app' }] });
  assert.match(html, /내 계정/); assert.match(html, /암호화/); assert.match(html, /방송에 사용/); assert.match(html, /Twitch/);
  assert.doesNotMatch(html, /synthetic-never-display|synthetic-hidden/);
  assert.match(render({ location: 'browser' }), /다른 브라우저에는 동기화되지 않습니다/);
});

test('readonly account connections keep storage provenance and saved metadata visible while disabling edits and use', () => {
  const connection = { target: 'twitch', updated_at: 200, has_stream_key: true };
  Object.defineProperty(connection, 'stream_key', { get() { throw new Error('account cards must not read a key'); } });
  for (const location of ['account', 'browser']) {
    for (const access of [{ canEditConnections: false }, { disabled: true }]) {
      const html = render({ connections: [connection], location, ...access });
      assert.match(html, /키 저장됨/);
      assert.match(html, new RegExp(`<dt>저장 위치</dt><dd>${location === 'browser' ? '이 브라우저' : '내 계정'}</dd>`));
      const buttons = html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g);
      const actions = buttons.filter(button => /방송에 사용|연결 추가|aria-label="Twitch (?:연결 수정|저장 연결 삭제)"/.test(button));
      assert.equal(actions.length, 4);
      assert.ok(actions.every(button => /<button\b[^>]*\bdisabled=""/.test(button)));
    }
  }
  assert.doesNotMatch(render({ connections: [{ ...connection, has_stream_key: false }] }), /키 저장됨/);
});

test('the members view renders only for the separate site administrator permission', () => {
  assert.doesNotMatch(render({ mode: 'members' }), /id="member-search"/);
  assert.match(render({ mode: 'members', account: { tenant_id: member.id, subject: 'synthetic-subject',
    roles: ['admin', 'site_admin'], permissions: { manage_members: true } } }), /id="member-search"/);
});

// Exercise the actual detail component's event handlers and effect cleanup with
// deferred requests, including navigation away before a destructive response.
function detailHarness(ownId = member.id) {
  const env = environment(); let active;
  const changed = []; const started = []; const deleted = []; const revoked = [];
  const hooks = { ...react,
    useState(initial) { const index = active.cursor++; const state = active;
      if (!(index in state.slots)) state.slots[index] = typeof initial === 'function' ? initial() : initial;
      return [state.slots[index], value => { state.slots[index] = typeof value === 'function' ? value(state.slots[index]) : value; }]; },
    useRef(initial) { const index = active.cursor++; return active.slots[index] ||= { current: initial }; },
    useMemo(factory, deps) { const index = active.cursor++; const previous = active.slots[index];
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) active.slots[index] = { value: factory(), deps };
      return active.slots[index].value; },
    useCallback(callback, deps) { return hooks.useMemo(() => callback, deps); },
    useEffect(effect, deps) { const index = active.cursor++; const state = active; const previous = state.slots[index];
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) {
        state.effects.push(() => { previous?.cleanup?.(); state.slots[index] = { deps, cleanup: effect() }; });
      } },
  };
  const { default: Component } = load('../app/member-management.tsx', name => {
    if (name === 'react') return hooks;
    if (name === 'react/jsx-runtime') return require('../node_modules/react/jsx-runtime');
    if (name === '@/lib/member-management') return env;
    if (name === '@/lib/api') return { sessionIdentity: () => 'owner-one' };
    if (name === '@/components/ui/button') return { Button: 'button' };
    if (name === '@/components/ui/input') return { Input: 'input' };
    if (name === './connection-editor') return { default: () => null };
    if (name === './platform-picker') return { PlatformMark: 'span' };
    if (name === 'lucide-react') return new Proxy({}, { get: (_, key) => String(key) });
    if (name.endsWith('.css')) return {};
    throw new Error(`Unexpected module ${name}`);
  });
  function renderer(component, props) { const state = { slots: [], cursor: 0, effects: [] }; return {
    render() { active = state; state.cursor = 0; const tree = component(props); const effects = state.effects.splice(0); effects.forEach(effect => effect()); return tree; },
    dispose() { state.slots.forEach(slot => slot?.cleanup?.()); },
  }; }
  const account = { tenant_id: ownId, subject: 'synthetic', roles: ['admin', 'site_admin'], permissions: { manage_members: true } };
  const root = renderer(Component, { mode: 'members', identity: 'owner-one', account, targets: [{ id: 'twitch', label: 'Twitch' }],
    onSessionRevoked: () => revoked.push(true), onOwnConnectionRemovalStarted: target => started.push(target), onOwnConnectionDeleted: target => deleted.push(target) });
  const membersNode = nodes(root.render()).find(node => node.type?.name === 'Members');
  const members = renderer(membersNode.type, membersNode.props);
  async function open() {
    members.render(); env.calls[0].resolve({ items: [member], total: 1, offset: 0, limit: 25 }); await flush();
    nodes(members.render()).find(node => node.type === 'button' && node.props['aria-pressed'] === false).props.onClick();
    const node = nodes(members.render()).find(node => node.type?.name === 'MemberDetails');
    const details = renderer(node.type, { ...node.props, onChanged: () => changed.push(true) });
    details.render(); env.calls[1].resolve(detail); await flush(); return details;
  }
  return { env, open, started, deleted, revoked, changed };
}
function nodes(node) { return Array.isArray(node) ? node.flatMap(nodes) : !node || typeof node !== 'object' ? [] : [node, ...nodes(node.props?.children)]; }
const flush = () => new Promise(resolve => setImmediate(resolve));
const nodeText = node => nodes(node).flatMap(item => [item.props?.children].flat()).filter(value => typeof value === 'string').join(' ');

test('deleting the administrators own connection immediately notifies the existing store without a key-use request', async () => {
  const harness = detailHarness(); const ui = await harness.open();
  nodes(ui.render()).find(node => node.props?.['aria-label'] === 'Twitch 회원 저장 연결 삭제').props.onClick();
  nodes(ui.render()).find(node => node.type?.name === 'ConfirmAction').props.onConfirm();
  assert.deepEqual(harness.started, ['twitch']); assert.deepEqual(harness.deleted, []);
  assert.equal(harness.env.calls[2].init.method, 'DELETE');
  harness.env.calls[2].resolve({ status: 204 }); await flush();
  assert.deepEqual(harness.deleted, ['twitch']); assert.equal(harness.changed.length, 1);
  assert.ok(harness.env.calls.every(call => !call.path.endsWith('/use'))); ui.dispose();
});

test('leaving a member detail before deletion returns suppresses callbacks and aborts its request', async () => {
  const harness = detailHarness(); const ui = await harness.open();
  nodes(ui.render()).find(node => node.props?.['aria-label'] === 'Twitch 회원 저장 연결 삭제').props.onClick();
  nodes(ui.render()).find(node => node.type?.name === 'ConfirmAction').props.onConfirm();
  ui.dispose(); assert.equal(harness.env.calls[2].init.signal.aborted, true);
  harness.env.calls[2].resolve({ status: 204 }); await flush();
  assert.deepEqual(harness.deleted, []); assert.deepEqual(harness.changed, []);
});

test('self session revocation resets local authentication immediately without another member request', async () => {
  const harness = detailHarness(); const ui = await harness.open();
  nodes(ui.render()).find(node => node.type === 'button' && nodeText(node).includes('모든 기기 로그아웃')).props.onClick();
  nodes(ui.render()).find(node => node.type?.name === 'ConfirmAction').props.onConfirm();
  harness.env.calls[2].resolve({ revoked: true }); await flush();
  assert.deepEqual(harness.revoked, [true]); assert.equal(harness.env.calls.length, 3); assert.deepEqual(harness.changed, []); ui.dispose();
});
