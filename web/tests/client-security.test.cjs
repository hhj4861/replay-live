/* CommonJS is intentional: the test executes transpiled modules in isolated VM contexts. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
/* Execute the real TypeScript modules with synthetic browser/OIDC/network adapters.
 * No application source rewriting, external requests, or real credentials.
 * Run: node --test web/tests/client-security.test.cjs
 */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const { isIP } = require('node:net');
const ts = require('../node_modules/typescript');

function environment() {
  const values = new Map();
  const events = [];
  const sessionStorage = { getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) };
  const window = { sessionStorage, location: { origin: 'https://studio.example', pathname: '/', search: '' },
    dispatchEvent: event => events.push(event), history: { replaceState: () => { window.location.search = ''; } } };
  class CustomEvent { constructor(type, options) { this.type = type; this.detail = options?.detail; } }
  return { values, events, sessionStorage, window, CustomEvent, crypto: webcrypto,
    URL, URLSearchParams, TextEncoder, Headers, Request, Response, AbortController, AbortSignal, Date,
    fetch: () => { throw new Error('Unmocked networking is forbidden'); } };
}

function load(relative, globals, imports) {
  const filename = path.join(__dirname, '..', relative);
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS }, fileName: filename,
  }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(code, { ...globals, exports: module.exports, module,
    require: name => { if (!(name in imports)) throw new Error(`Unexpected import: ${name}`); return imports[name]; } }, { filename });
  return module.exports;
}

function apiEnvironment({ commercial = false, cloud = true, accessToken = async () => 'synthetic-access-token', timeoutSignal } = {}) {
  const globals = environment();
  if (timeoutSignal) globals.AbortSignal = timeoutSignal;
  const calls = [];
  let fetcher = () => Response.json({ ok: true });
  globals.fetch = async (url, init) => { calls.push({ url, headers: new Headers(init.headers), body: init.body, credentials: init.credentials }); return fetcher(url, init); };
  globals.__REPLAY_API_BASE__ = commercial ? 'https://control.example/api' : '';
  globals.__REPLAY_CLOUD__ = cloud && !commercial;
  let identity = 'synthetic-account-a';
  const api = load('lib/api.ts', globals, { './auth': { COMMERCIAL: commercial, accessToken, authIdentity: () => identity } });
  function save(token = 'synthetic-session-a', base = 'https://sandbox-a.vercel.run/api') {
    api.saveSession({ token, expires_at: Date.now() / 1000 + 3600, api_base: base, sandbox_expires_at: Date.now() / 1000 + 600 });
  }
  return { ...globals, api, calls, save, setIdentity: value => { identity = value; }, useFetch: fn => { fetcher = fn; } };
}

test('cloud network interruption preserves token, base and idempotency for an explicit retry', async () => {
  const env = apiEnvironment(); env.save();
  const body = JSON.stringify({ media_id: 'synthetic-media', stream_key: 'synthetic-secret-key' });
  const key = await env.api.broadcastIdempotency(body);
  env.useFetch(() => { throw new TypeError('network unavailable'); });
  await assert.rejects(env.api.request('/broadcasts', { method: 'POST', body, headers: { 'Idempotency-Key': key } }), /접속 정보는 유지/);
  assert.equal(env.api.hasSession(), true);
  assert.equal(await env.api.broadcastIdempotency(body), key);
  assert.equal(env.calls.length, 1, 'ambiguous POST failure is never automatically replayed');
  assert.equal(env.events.length, 0);
  env.useFetch(() => Response.json({ id: 'synthetic-job' }));
  await env.api.request('/broadcasts', { method: 'POST', body, headers: { 'Idempotency-Key': key } });
  assert.equal(env.calls[1].url, 'https://sandbox-a.vercel.run/api/broadcasts');
  assert.equal(env.calls[1].headers.get('Authorization'), 'Bearer synthetic-session-a');
  assert.equal(env.calls[1].headers.get('Idempotency-Key'), key);
  assert.equal(env.calls[1].credentials, 'omit');
  assert.equal(JSON.stringify([...env.values]).includes('synthetic-secret-key'), false);
});

test('old-session 401 cannot remove the replacement session or expose the old response', async () => {
  const env = apiEnvironment(); env.save();
  let resolve;
  env.useFetch(() => new Promise(done => { resolve = done; }));
  const pending = env.api.request('/media');
  env.save('synthetic-session-b', 'https://sandbox-b.vercel.run/api');
  resolve(Response.json({ detail: 'expired' }, { status: 401 }));
  await assert.rejects(pending, /이전 테스트 세션/);
  assert.equal(env.sessionStorage.getItem(env.api.SESSION_KEY), 'synthetic-session-b');
  assert.equal(env.api.hasSession(), true);
});

test('cloud runtime expiry prevents API calls while retaining a resumable login token', async () => {
  const env = apiEnvironment(); env.save();
  env.sessionStorage.setItem('replay-test-sandbox-expires-at', String(Date.now() / 1000 - 1));
  await assert.rejects(env.api.request('/media'), /접속 코드로 다시 접속/);
  assert.equal(env.calls.length, 0);
  assert.equal(env.api.resumableSession(), 'synthetic-session-a');
});

test('untrusted session URL shapes are rejected before saving any token', () => {
  const env = apiEnvironment();
  for (const base of ['http://sandbox.vercel.run/api', 'https://user:pass@sandbox.vercel.run/api',
    'https://sandbox.vercel.run/api?token=private', 'https://sandbox.vercel.run/api#fragment', 'https://sandbox.vercel.run/private']) {
    assert.throws(() => env.save('synthetic-session', base), /세션 응답/);
    assert.equal(env.sessionStorage.getItem(env.api.SESSION_KEY), null);
  }
});

test('only a current-session 401 clears POC authentication; 503 preserves it', async () => {
  const env = apiEnvironment(); env.save();
  env.useFetch(() => Response.json({ detail: 'temporarily unavailable' }, { status: 503 }));
  await assert.rejects(env.api.request('/media'), /temporarily unavailable/);
  assert.equal(env.api.hasSession(), true);
  env.useFetch(() => Response.json({ detail: 'expired' }, { status: 401 }));
  await assert.rejects(env.api.request('/media'), /다시 접속/);
  assert.equal(env.api.hasSession(), false);
  assert.equal(env.events.at(-1).type, 'replay-session-expired');
});

test('commercial 401 refreshes once and preserves the exact mutation body and request ID', async () => {
  const tokenCalls = [];
  const env = apiEnvironment({ commercial: true, accessToken: async forced => { tokenCalls.push(Boolean(forced)); return forced ? 'synthetic-new-token' : 'synthetic-old-token'; } });
  env.useFetch(() => env.calls.length === 1 ? Response.json({}, { status: 401 }) : Response.json({ id: 'created' }));
  await env.api.request('/broadcasts', { method: 'POST', headers: { 'Idempotency-Key': 'synthetic-request-id' }, body: '{"title":"test"}' });
  assert.deepEqual(tokenCalls, [false, true]);
  assert.equal(env.calls.length, 2);
  assert.equal(env.calls[0].headers.get('Authorization'), 'Bearer synthetic-old-token');
  assert.equal(env.calls[1].headers.get('Authorization'), 'Bearer synthetic-new-token');
  assert.equal(env.calls[1].body, env.calls[0].body);
  assert.equal(env.calls[1].headers.get('Idempotency-Key'), env.calls[0].headers.get('Idempotency-Key'));
});

test('commercial denial after refresh is surfaced without an infinite refresh loop', async () => {
  let refreshed = 0;
  const env = apiEnvironment({ commercial: true, accessToken: async forced => { if (forced) refreshed++; return 'synthetic-token'; } });
  env.useFetch(() => Response.json({ detail: 'access revoked' }, { status: 401 }));
  await assert.rejects(env.api.request('/media'), /access revoked/);
  assert.equal(refreshed, 1);
  assert.equal(env.calls.length, 2);
});

test('commercial account switch while acquiring a token cancels before sending the old mutation', async () => {
  let finish;
  const env = apiEnvironment({ commercial: true, accessToken: () => new Promise(resolve => { finish = resolve; }) });
  const pending = env.api.request('/uploads', { method: 'POST', body: '{"name":"old-private.mp4"}' });
  env.setIdentity('synthetic-account-b');
  finish('synthetic-new-account-token');
  await assert.rejects(pending, /이전 요청이 취소/);
  assert.equal(env.calls.length, 0);
});

for (const status of [200, 401]) test(`commercial old-account ${status} never reaches the new account or triggers a replay`, async () => {
  let finish; const tokenCalls = [];
  const env = apiEnvironment({ commercial: true, accessToken: async forced => { tokenCalls.push(Boolean(forced)); return 'synthetic-old-token'; } });
  env.useFetch(() => new Promise(resolve => { finish = resolve; }));
  const pending = env.api.api('/media');
  await new Promise(resolve => setImmediate(resolve));
  env.setIdentity('synthetic-account-b');
  finish(Response.json([{ name: 'old-private-media' }], { status }));
  await assert.rejects(pending, /이전 요청이 취소/);
  assert.equal(env.calls.length, 1);
  assert.deepEqual(tokenCalls, [false]);
});

test('commercial account switch during forced refresh cannot resend the old mutation', async () => {
  let finish;
  const env = apiEnvironment({ commercial: true, accessToken: forced => forced
    ? new Promise(resolve => { finish = resolve; }) : Promise.resolve('old-token') });
  env.useFetch(() => Response.json({}, { status: 401 }));
  const pending = env.api.request('/broadcast-batches', { method: 'POST', body: '{"title":"old-account-task"}' });
  await new Promise(resolve => setImmediate(resolve));
  env.setIdentity('synthetic-account-b');
  finish('synthetic-new-account-token');
  await assert.rejects(pending, /이전 요청이 취소/);
  assert.equal(env.calls.length, 1);
});

test('commercial identity remains fenced until response JSON has finished parsing', async () => {
  const env = apiEnvironment({ commercial: true }); let finish;
  env.useFetch(() => ({ ok: true, status: 200, json: () => new Promise(resolve => { finish = resolve; }) }));
  const pending = env.api.api('/broadcasts');
  await new Promise(resolve => setImmediate(resolve));
  env.setIdentity('synthetic-account-b');
  finish([{ title: 'old-private-broadcast' }]);
  await assert.rejects(pending, /이전 요청이 취소/);
});

test('late idempotency digest cannot overwrite the replacement account request key', async () => {
  const env = apiEnvironment({ commercial: true });
  const pending = env.api.broadcastIdempotency('{"media_id":"old-account-media"}');
  env.setIdentity('synthetic-account-b');
  env.sessionStorage.setItem('replay-create-id', 'new-account-pending-request');
  await assert.rejects(pending, /이전 요청이 취소/);
  assert.equal(env.sessionStorage.getItem('replay-create-id'), 'new-account-pending-request');
});

test('idempotency retains only a digest and UUID until acknowledgement', async () => {
  const env = apiEnvironment();
  const body = '{"stream_key":"synthetic-sensitive-key","media_id":"media-a"}';
  const [first, concurrent] = await Promise.all([env.api.broadcastIdempotency(body), env.api.broadcastIdempotency(body)]);
  assert.equal(first, concurrent);
  assert.equal(env.sessionStorage.getItem('replay-create-id').includes('synthetic-sensitive-key'), false);
  env.api.acknowledgeBroadcast();
  assert.notEqual(await env.api.broadcastIdempotency(body), first);
});

test('logout revokes only its captured old token without refreshing or touching a replacement session', async () => {
  let token = 'old-account-token'; let tokenReads = 0; let finish;
  const env = apiEnvironment({ commercial: true, accessToken: async () => { tokenReads++; return token; } });
  await env.api.request('/me');
  env.useFetch(() => new Promise(resolve => { finish = resolve; }));
  const pending = env.api.revokeCurrentSession();
  assert.equal(env.calls.at(-1).headers.get('Authorization'), 'Bearer old-account-token');
  assert.equal(env.calls.at(-1).url, 'https://control.example/api/logout');
  const previous = finish;
  env.setIdentity('account-b'); token = 'new-account-token';
  env.useFetch(() => Response.json({ ok: true }));
  await env.api.request('/me');
  previous(Response.json({}, { status: 401 }));
  assert.equal(await pending, false);
  assert.equal(tokenReads, 2, 'logout neither refreshes nor acquires a new token');
  assert.equal(await env.api.revokeCurrentSession(), true);
  assert.equal(env.calls.at(-1).headers.get('Authorization'), 'Bearer new-account-token');
  assert.equal(JSON.stringify([...env.values]).includes('account-token'), false);
});

test('a hung logout request is bounded and an uncaptured old identity cannot be revoked', async () => {
  const abort = new AbortController(); let timeout;
  const env = apiEnvironment({ commercial: true, timeoutSignal: { timeout: milliseconds => { timeout = milliseconds; return abort.signal; } } });
  await env.api.request('/me');
  env.useFetch((url, init) => new Promise((resolve, reject) => {
    assert.equal(init.redirect, 'error'); assert.equal(init.cache, 'no-store'); assert.equal(init.credentials, 'omit');
    init.signal.addEventListener('abort', () => reject(new Error('synthetic stalled network')), { once: true });
  }));
  const pending = env.api.revokeCurrentSession();
  assert.equal(timeout, 10_000);
  abort.abort(); assert.equal(await pending, false);
  const calls = env.calls.length;
  assert.equal(await env.api.revokeCurrentSession(), false);
  assert.equal(env.calls.length, calls);
});

function authEnvironment(overrides = {}, logoutOverrides = {}) {
  const globals = environment();
  const seen = { renewals: 0, callbacks: 0, removed: 0, settings: undefined, redirects: [], revocations: [] };
  const user = { access_token: 'synthetic-access', expired: false, refresh_token: 'synthetic-refresh' };
  class UserManager {
    constructor(settings) { seen.settings = this.settings = settings; this.events = { addAccessTokenExpiring: handler => { seen.expiring = handler; } }; }
    async getUser() { return user; }
    async signinSilent() { seen.renewals++; await new Promise(resolve => setImmediate(resolve)); return user; }
    async signinRedirectCallback() { seen.callbacks++; return user; }
    async signinRedirect() {}
    async signoutRedirect() { throw new Error('synthetic IdP outage'); }
    async removeUser() { seen.removed++; }
    stopSilentRenew() {}
  }
  class OidcClient {
    constructor(settings) { seen.logoutSettings = settings; }
    async revokeToken(token, type) { seen.revocations.push({ token, type }); }
    async createSignoutRequest() { throw new Error('synthetic IdP outage'); }
  }
  Object.assign(OidcClient.prototype, logoutOverrides);
  Object.assign(UserManager.prototype, overrides);
  globals.window.location.assign = url => { seen.redirects.push(url); };
  globals.__REPLAY_COMMERCIAL__ = true;
  globals.__REPLAY_OIDC__ = { authority: 'https://issuer.example', client_id: 'public-client', scope: 'openid profile offline_access', audience: 'replay-api' };
  const googleAuth = load('lib/google-auth.ts', globals, {});
  const auth = load('lib/auth.ts', globals, { './google-auth': googleAuth,
    'oidc-client-ts': { OidcClient, UserManager, WebStorageStateStore: class { constructor(settings) { this.settings = settings; } } } });
  return { ...globals, auth, seen, user };
}

test('OIDC authorization uses code flow, session storage and coalesces concurrent forced refreshes', async () => {
  const env = authEnvironment();
  assert.equal(await env.auth.completeSignIn(), true);
  assert.equal(env.seen.settings.response_type, 'code');
  assert.equal(env.seen.settings.automaticSilentRenew, false);
  env.seen.settings.userStore.settings.store.setItem('oidc.user:fixture', 'synthetic-user');
  assert.equal(env.sessionStorage.getItem('oidc.user:fixture'), 'synthetic-user');
  assert.equal(env.seen.settings.stateStore.settings.store, env.sessionStorage);
  const tokens = await Promise.all(Array.from({ length: 5 }, () => env.auth.accessToken(true)));
  assert.equal(tokens.every(token => token === 'synthetic-access'), true);
  assert.equal(env.seen.renewals, 1);
});

test('OIDC signout clears browser credentials even if identity-provider redirection fails', async () => {
  const env = authEnvironment();
  await assert.rejects(env.auth.signOut(), /synthetic IdP outage/);
  assert.equal(env.seen.removed, 1);
});

test('OIDC logout immediately clears local credentials while provider discovery is stalled', async () => {
  let finish;
  const env = authEnvironment({}, { async createSignoutRequest() { return new Promise(resolve => { finish = resolve; }); } });
  await env.auth.completeSignIn();
  const before = env.auth.authIdentity();
  env.seen.settings.userStore.settings.store.setItem('oidc.user:fixture', 'old-private-session');
  const pending = env.auth.signOut();
  assert.notEqual(env.auth.authIdentity(), before);
  assert.equal(env.sessionStorage.getItem('oidc.user:fixture'), null, 'removal is synchronous before any network await');
  await assert.rejects(env.auth.accessToken(), /다시 로그인/);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(env.seen.logoutSettings.requestTimeoutInSeconds, 8);
  finish({ url: 'https://issuer.example/end-session' }); await pending;
  assert.deepEqual(env.seen.redirects, ['https://issuer.example/end-session']);
  assert.deepEqual(env.seen.revocations, [{ token: 'synthetic-refresh', type: 'refresh_token' }, { token: 'synthetic-access', type: 'access_token' }]);
});

test('a late old logout and renewal cannot navigate away from or overwrite a new account', async () => {
  let finish;
  const env = authEnvironment({}, { async createSignoutRequest() { return new Promise(resolve => { finish = resolve; }); } });
  await env.auth.completeSignIn();
  const retiredStore = env.seen.settings.userStore.settings.store;
  retiredStore.setItem('oidc.user:fixture', 'old-session');
  const pending = env.auth.signOut();
  await new Promise(resolve => setImmediate(resolve));
  await env.auth.signIn();
  const currentStore = env.seen.settings.userStore.settings.store;
  currentStore.setItem('oidc.user:fixture', 'new-account-session');
  retiredStore.setItem('oidc.user:fixture', 'late-refresh-old-account');
  retiredStore.removeItem('oidc.user:fixture');
  assert.equal(retiredStore.getItem('oidc.user:fixture'), null);
  finish({ url: 'https://issuer.example/old-account-logout' }); await pending;
  assert.equal(env.sessionStorage.getItem('oidc.user:fixture'), 'new-account-session');
  assert.deepEqual(env.seen.redirects, []);
});

test('OIDC identity changes on sign-in and signout but remains stable during token refresh', async () => {
  const env = authEnvironment();
  const initial = env.auth.authIdentity();
  await env.auth.signIn();
  const signedIn = env.auth.authIdentity();
  assert.notEqual(signedIn, initial);
  await env.auth.accessToken(true);
  assert.equal(env.auth.authIdentity(), signedIn);
  await assert.rejects(env.auth.signOut(), /synthetic IdP outage/);
  assert.notEqual(env.auth.authIdentity(), signedIn);
});

test('OIDC callback completion is shared so duplicated effects consume state only once', async () => {
  let consumed = false;
  let callbacks = 0;
  const env = authEnvironment({ async signinRedirectCallback() {
    callbacks++;
    if (consumed) throw new Error('No matching state found in storage');
    consumed = true;
    await new Promise(resolve => setImmediate(resolve));
  } });
  env.window.location.search = '?state=synthetic-state&code=synthetic-code';
  const result = await Promise.all([env.auth.completeSignIn(), env.auth.completeSignIn()]);
  assert.deepEqual(result, [true, true]);
  assert.equal(callbacks, 1);
  assert.equal(env.window.location.search, '');
});

test('automatic expiry event and API refresh use the same renewal lock', async () => {
  const env = authEnvironment();
  await env.auth.completeSignIn();
  env.seen.expiring();
  await env.auth.accessToken(true);
  assert.equal(env.seen.renewals, 1);
});

test('late OIDC renewal cannot restore credentials after signout', async () => {
  let finish;
  const env = authEnvironment({ async signinSilent() { return new Promise(resolve => { finish = resolve; }); } });
  const pending = env.auth.accessToken(true);
  await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(env.auth.signOut(), /synthetic IdP outage/);
  finish({ access_token: 'synthetic-late-token', expired: false });
  await assert.rejects(pending, /이전 로그인 요청/);
  await assert.rejects(env.auth.accessToken(), /다시 로그인/);
  assert.equal(env.seen.removed, 1, 'retired renewal never removes a newer account from storage');
});

function dispatchEnvironment({ target = 'local', runtimes = [], missingRuntime = false, releaseMismatch = false,
  alerts = [], webhookStatus = 204, monitoringFails = false, cleanupAdvanceMs = 0, streamDestination, pinSetupFails = false,
  setupFailure, setupAdvanceMs = {}, claimAdvanceMs = 0, verifyExitCode } = {}) {
  const calls = [], created = [], files = [], commands = [], stopped = [], logs = [], deliveries = [];
  const wall = Date.now(), signalTimeouts = new WeakMap();
  let elapsed = 0;
  class TestDate extends Date { static now() { return wall + elapsed; } }
  const job = { id: 'a'.repeat(32), lease_version: 1, lease_seconds: 180, deadline: wall / 1000 + 300,
    callback_base: 'https://control.example/internal/jobs/' + 'a'.repeat(32), callback_token: 'synthetic-job-callback',
    input: { url: 'https://private-bucket.s3.example/replay/media.mp4?signature=synthetic' },
    target, version: 'synthetic-release-1', mode: 'production', stream_key: target === 'youtube' ? 'synthetic-stream-key' : '' };
  if (!['local', 'validate'].includes(target)) job.stream_destination = streamDestination ?? {
    target, server_url: 'rtmps://ingest.example.com:443/live', stream_key: 'synthetic-platform-key?token=value',
    hostname: 'ingest.example.com', port: 443, protocol: 'rtmps', addresses: ['8.8.8.8'],
  };
  const env = { CRON_SECRET: 'synthetic-cron-secret-at-least-32-characters', REPLAY_COMMERCIAL: '1',
    REPLAY_CONTROL_URL: 'https://control.example', REPLAY_CONTROL_TOKEN: 'synthetic-global-control-secret',
    REPLAY_WORKER_SNAPSHOT_ID: 'synthetic-snapshot', REPLAY_VERSION: job.version };
  if (alerts.length) {
    env.REPLAY_ALERT_WEBHOOK_URL = 'https://alerts.example/replay';
    env.REPLAY_ALERT_WEBHOOK_TOKEN = 'synthetic-alert-destination-secret';
  }
  class APIError extends Error { constructor(status) { super('Synthetic API failure'); this.response = { status }; } }
  function failSetup(phase) {
    if (setupFailure?.phase !== phase) return;
    const error = setupFailure.category === 'APIError' ? new APIError(setupFailure.status) : new Error();
    if (['AbortError', 'TimeoutError'].includes(setupFailure.category)) error.name = setupFailure.category;
    else error.name = 'synthetic-sensitive-error-name';
    error.message = `synthetic-upstream-secret ${env.REPLAY_CONTROL_TOKEN} ${env.CRON_SECRET} ${job.callback_token} ${job.input.url}`;
    error.stack = `synthetic-sensitive-stack ${error.message}`;
    error.cause = { job, env };
    throw error;
  }
  const sandbox = { stop: async () => { stopped.push(true); },
    writeFiles: async value => { elapsed += setupAdvanceMs.write || 0; failSetup('write'); files.push(...value); },
    runCommand: async command => { commands.push(command); const phase = command.detached ? 'launch' : command.sudo ? 'pin' : 'verify';
      elapsed += setupAdvanceMs[phase] || 0; failSetup(phase);
      return { exitCode: phase === 'verify' && verifyExitCode !== undefined ? verifyExitCode : releaseMismatch || (pinSetupFails && command.sudo) ? 1 : 0 }; } };
  const Sandbox = { create: async options => {
    assert.ok(Number.isInteger(options.timeout), 'Sandbox requires an integer millisecond timeout');
    created.push(options); elapsed += setupAdvanceMs.create || 0; failSetup('create'); return sandbox;
  },
    get: async options => { calls.push({ sandboxGet: options }); elapsed += cleanupAdvanceMs; if (missingRuntime) throw new APIError(404); return sandbox; } };
  let claimed = false;
  const fetch = async (url, init) => {
    calls.push({ url, init });
    if (new URL(url).hostname === 'alerts.example') {
      deliveries.push({ url, init });
      assert.equal(new Headers(init.headers).get('Authorization'), 'Bearer ' + env.REPLAY_ALERT_WEBHOOK_TOKEN);
      return new Response(null, { status: webhookStatus });
    }
    assert.equal(new Headers(init.headers).get('Authorization'), 'Bearer ' + env.REPLAY_CONTROL_TOKEN);
    assert.equal(init.redirect, 'error');
    const endpoint = new URL(url).pathname.split('/').at(-1);
    if (endpoint === 'runtimes') return Response.json({ runtimes });
    if (endpoint === 'claim') { elapsed += claimAdvanceMs; const value = claimed ? null : job; claimed = true; return Response.json({ job: value }); }
    if (endpoint === 'monitor') return Response.json({ alerts_pending: alerts.length }, { status: monitoringFails ? 503 : 200 });
    if (endpoint === 'alerts') return Response.json({ alerts });
    return Response.json({ ok: true });
  };
  const dispatcher = load('api/dispatch.ts', { process: { env }, fetch, URL, Date: TestDate, Buffer, Response,
    performance: { now: () => elapsed }, AbortSignal: { timeout: milliseconds => {
      const signal = AbortSignal.timeout(milliseconds); signalTimeouts.set(signal, milliseconds); return signal;
    } },
    console: { info: value => logs.push(value), error: value => logs.push(value) } },
    { 'node:crypto': require('node:crypto'), 'node:net': require('node:net'), '@vercel/sandbox': { Sandbox, APIError },
      '../lib/worker-network-policy.js': load('lib/worker-network-policy.ts', {}, {}) });
  const run = (authorization = 'Bearer ' + env.CRON_SECRET) => dispatcher.default.fetch(
    new Request('https://studio.example/api/dispatch', { headers: { Authorization: authorization } }));
  return { run, env, job, calls, created, files, commands, stopped, logs, deliveries, signalTimeouts, now: () => TestDate.now() };
}

test('dispatcher rejects missing cron authentication before control or Sandbox access', async () => {
  const env = dispatchEnvironment();
  assert.equal((await env.run('Bearer incorrect-synthetic-value')).status, 401);
  assert.equal(env.calls.length, 0);
  assert.equal(env.created.length, 0);
});

test('private jobs receive isolated VMs, pinned HTTP hosts and no YouTube or global credentials', async () => {
  for (const target of ['local', 'validate']) {
    const env = dispatchEnvironment({ target });
    const response = await env.run();
    assert.equal(response.status, 200);
    assert.equal((await response.json()).started, 1);
    assert.equal(env.created.length, 1);
    const options = env.created[0];
    assert.equal(options.persistent, false);
    assert.equal(options.ports.length, 0);
    assert.equal(options.networkPolicy.allow['a.rtmps.youtube.com'], undefined);
    for (const [host, rules] of Object.entries(options.networkPolicy.allow)) {
      assert.equal(rules[0].transform[0].headers.Host, host);
      assert.equal(rules[0].match, undefined, 'Host pinning must apply unconditionally');
    }
    const content = env.files[0].content.toString();
    assert.equal(content.includes(env.env.REPLAY_CONTROL_TOKEN), false);
    assert.equal(content.includes(env.env.CRON_SECRET), false);
    assert.equal(content.includes('synthetic-job-callback'), true);
    assert.equal(env.logs.join('').includes('synthetic-job-callback'), false);
    assert.equal(env.commands[0].args.includes(env.job.version), true, 'snapshot version checked before job write');
  }
});

test('fractional database deadlines reach Sandbox as integer milliseconds', async () => {
  const env = dispatchEnvironment({ target: 'validate' });
  env.job.deadline = (env.now() + 180_123.4560546875) / 1000;
  assert.equal(Number.isInteger(env.job.deadline * 1000), false, 'fixture retains a fractional millisecond deadline');
  assert.equal((await (await env.run()).json()).started, 1);
  assert.equal(Number.isInteger(env.created[0].timeout), true);
  assert.ok(env.created[0].timeout >= 179_000 && env.created[0].timeout <= 180_123);
});

test('each live platform receives exact public IP egress and a VM hosts pin before its credentials', async () => {
  for (const target of ['youtube', 'twitch', 'facebook', 'instagram', 'tiktok', 'naver', 'chzzk', 'kick', 'custom']) {
    const env = dispatchEnvironment({ target });
    assert.equal((await (await env.run()).json()).started, 1);
    const policy = env.created[0].networkPolicy;
    assert.equal(policy.allow['ingest.example.com'], undefined, 'stream domains must not bypass the IP pin');
    assert.deepEqual(Array.from(policy.subnets.allow), ['8.8.8.8/32']);
    assert.equal(policy.subnets.deny, undefined);
    assert.equal(policy.subnets.allow.some(cidr => cidr.startsWith('169.254.')), false);
    assert.equal(env.commands[1].sudo, true);
    assert.equal(env.commands[1].args[1].includes('install_host_pin'), true);
    assert.equal(JSON.stringify(env.commands[1]).includes('synthetic-platform-key'), false);
    assert.equal(JSON.parse(env.commands[1].args[2]).hostname, 'ingest.example.com');
    assert.equal(JSON.parse(env.files[0].content.toString()).stream_destination.target, target);
    assert.equal(env.logs.join('').includes('synthetic-platform-key'), false);
  }
});

test('recording imports use public IPv4 CIDRs without domain DNS restrictions or private access', async () => {
  const env = dispatchEnvironment({ target: 'import' });
  env.job.input = null;
  delete env.job.stream_destination;
  env.job.source = { provider: 'direct', url: 'https://media.example/own.mp4?signature=synthetic-source' };
  assert.equal((await (await env.run()).json()).started, 1);
  const policy = env.created[0].networkPolicy;
  const expected = load('lib/worker-network-policy.ts', {}, {});
  assert.equal(policy.allow, undefined, 'Domain allow rules would block redirected CDN DNS');
  assert.equal(policy.subnets.deny, undefined, 'Provider rejects our private deny ranges');
  assert.deepEqual(Array.from(policy.subnets.allow), Array.from(expected.PUBLIC_IPV4_CIDRS));
  assert.equal(policy.subnets.allow.some(value => value.includes(':') || value.endsWith('/0')), false);
  const delivered = JSON.parse(env.files[0].content.toString());
  assert.equal(delivered.input, null);
  assert.equal(delivered.source.provider, 'direct');
  assert.equal(env.logs.join('').includes('synthetic-source'), false);
  assert.equal(env.commands.some(command => command.sudo), false);
});

test('live policy grants raw TCP only to exact public pins and preserves control Host transforms', async () => {
  const env = dispatchEnvironment({ target: 'custom' });
  assert.equal((await (await env.run()).json()).started, 1);
  const policy = env.created[0].networkPolicy;
  assert.equal(policy.allow['*'], undefined);
  assert.equal(policy.subnets.deny, undefined);
  assert.deepEqual(Array.from(policy.subnets.allow), Array.from(new Set(env.job.stream_destination.addresses), address => `${address}/32`));
  for (const cidr of policy.subnets.allow) {
    const [address, prefix, extra] = cidr.split('/');
    assert.equal(isIP(address), 4);
    assert.equal(prefix, '32');
    assert.equal(extra, undefined);
  }
  for (const [host, rules] of Object.entries(policy.allow)) {
    assert.equal(rules[0].transform[0].headers.Host, host);
    assert.equal(rules[0].match, undefined);
  }
});

test('dispatcher rejects private/malformed stream pins and failed host setup without starting a worker', async () => {
  for (const addresses of [['127.0.0.1'], ['169.254.169.254'], ['8.8.8.8/0'], ['::1'], [], ['8.8.8.8', '10.0.0.1']]) {
    const env = dispatchEnvironment({ target: 'custom', streamDestination: {
      target: 'custom', server_url: 'rtmp://ingest.example.com:1935/live', stream_key: 'synthetic-key',
      hostname: 'ingest.example.com', port: 1935, protocol: 'rtmp', addresses,
    } });
    assert.equal((await (await env.run()).json()).started, 0);
    assert.equal(env.created.length, 0);
    assert.equal(env.files.length, 0);
  }
  const env = dispatchEnvironment({ target: 'youtube', pinSetupFails: true });
  assert.equal((await (await env.run()).json()).started, 0);
  assert.equal(env.stopped.length, 1);
  assert.equal(env.files.length, 0, 'pin failure must precede writing secrets');
});

test('dispatcher fences mismatched snapshot releases before writing worker credentials', async () => {
  const env = dispatchEnvironment({ releaseMismatch: true });
  const result = await (await env.run()).json();
  assert.equal(result.started, 0);
  assert.equal(env.files.length, 0);
  assert.equal(env.stopped.length, 1);
  assert.equal(env.logs.map(value => JSON.parse(value)).find(value => value.event === 'worker_setup_failed').phase, 'verify');
});

for (const phase of ['create', 'verify', 'pin', 'write', 'launch']) {
  test(`worker setup ${phase} failure logs only safe fields without retrying the lease`, async () => {
    const env = dispatchEnvironment({ target: 'youtube', setupFailure: { phase, category: 'Error' } });
    const response = await env.run();
    assert.equal(response.status, 200, 'uncertain setup keeps the existing expiry/reconciliation policy');
    assert.equal((await response.json()).started, 0);
    const records = env.logs.map(value => JSON.parse(value));
    const failures = records.filter(value => value.event === 'worker_setup_failed');
    assert.deepEqual(failures, [{ event: 'worker_setup_failed', version: env.job.version,
      job_id: env.job.id, lease_version: 1, phase, error_category: 'Error', phase_elapsed_ms: 0,
      ...(['pin', 'write', 'launch'].includes(phase) ? { verify_exit_code: 0 } : {}) }]);
    assert.deepEqual(records.find(value => value.event === 'dispatch_tick').deferred, ['worker_setup']);
    assert.equal(env.created.length, 1, 'never create a replacement for the claimed lease');
    assert.equal(env.stopped.length, phase === 'create' ? 0 : 1);
    assert.equal(env.commands.filter(value => value.detached).length, phase === 'launch' ? 1 : 0);
    for (const sensitive of ['synthetic-upstream-secret', 'synthetic-sensitive-stack', 'synthetic-sensitive-error-name',
      env.env.REPLAY_CONTROL_TOKEN, env.env.CRON_SECRET, env.job.callback_token, env.job.input.url,
      env.job.stream_destination.stream_key, env.job.stream_destination.server_url]) {
      assert.equal(env.logs.join('').includes(sensitive), false, `log excludes ${sensitive.split(' ')[0]}`);
    }
  });
}

test('worker setup diagnostics use fixed error categories and bounded API status only', async () => {
  for (const [category, status, expected] of [['APIError', 403, { error_category: 'APIError', http_status: 403 }],
    ['APIError', 'synthetic-secret-status', { error_category: 'APIError' }],
    ['APIError', 999, { error_category: 'APIError' }], ['AbortError', undefined, { error_category: 'AbortError' }],
    ['TimeoutError', undefined, { error_category: 'TimeoutError' }]]) {
    const env = dispatchEnvironment({ setupFailure: { phase: 'create', category, status } });
    await env.run();
    const failure = env.logs.map(value => JSON.parse(value)).find(value => value.event === 'worker_setup_failed');
    assert.deepEqual(failure, { event: 'worker_setup_failed', version: env.job.version, job_id: env.job.id,
      lease_version: 1, phase: 'create', phase_elapsed_ms: 0, ...expected });
    assert.equal(env.logs.join('').includes('synthetic-secret-status'), false);
    assert.equal(env.logs.join('').includes('synthetic-upstream-secret'), false);
    assert.equal(env.logs.join('').includes('synthetic-sensitive-stack'), false);
  }
});

test('startup budgets accommodate bounded cold verification and reserve cleanup without acquiring another lease', async () => {
  const env = dispatchEnvironment({ target: 'youtube', claimAdvanceMs: 10_000,
    setupAdvanceMs: { create: 30_000, verify: 15_000, pin: 5_000, write: 7_000, launch: 7_000 } });
  assert.equal((await (await env.run()).json()).started, 1);
  assert.equal(env.commands[0].timeoutMs, 15_000);
  assert.equal(env.signalTimeouts.get(env.commands[0].signal), 20_000);
  assert.equal(env.calls.filter(call => call.url?.endsWith('/claim')).length, 1,
    'less than 100 seconds remaining must not acquire another lease');
  assert.equal(env.created.length, 1);
  assert.equal(env.stopped.length, 0);
});

test('post-claim lease budget counts claim latency and keeps 15 seconds for the first heartbeat', async () => {
  const env = dispatchEnvironment({ claimAdvanceMs: 9_000, setupAdvanceMs: { create: 1_000 } });
  env.job.lease_seconds = 30;
  assert.equal((await (await env.run()).json()).started, 1);
  assert.equal(env.signalTimeouts.get(env.created[0].signal), 6_000);
  assert.equal(env.signalTimeouts.get(env.commands[0].signal), 5_000);
});

test('an exhausted short lease or hard job deadline never creates a VM or replays the claimed job', async () => {
  for (const limit of ['lease', 'deadline']) {
    const env = dispatchEnvironment({ claimAdvanceMs: 9_000 });
    if (limit === 'lease') env.job.lease_seconds = 20;
    else env.job.deadline = env.now() / 1000 + 20;
    assert.equal((await (await env.run()).json()).started, 0);
    assert.equal(env.created.length, 0);
    assert.equal(env.files.length, 0);
    assert.equal(env.logs.map(JSON.parse).filter(value => value.event === 'worker_setup_failed').length, 1);
  }
});

test('setup deadline exhaustion stops the acquired VM without writing credentials or retrying it', async () => {
  const env = dispatchEnvironment({ setupAdvanceMs: { create: 80_000 } });
  env.job.lease_seconds = 90;
  assert.equal((await (await env.run()).json()).started, 0);
  assert.equal(env.created.length, 1);
  assert.equal(env.stopped.length, 1);
  assert.equal(env.files.length, 0);
  assert.equal(env.commands.length, 0);
  assert.equal(env.calls.filter(call => call.url?.endsWith('/claim')).length, 1);
});

test('verify diagnostics include only a safe integer exit code and elapsed milliseconds', async () => {
  for (const exitCode of [137, 'synthetic-secret-exit', 1.5, Number.NaN]) {
    const env = dispatchEnvironment({ verifyExitCode: exitCode, setupAdvanceMs: { verify: 12.75 } });
    await env.run();
    const failure = env.logs.map(JSON.parse).find(value => value.event === 'worker_setup_failed');
    assert.equal(failure.phase, 'verify');
    assert.equal(failure.phase_elapsed_ms, 12);
    assert.equal(failure.verify_exit_code, Number.isSafeInteger(exitCode) ? exitCode : undefined);
    assert.equal(env.logs.join('').includes('synthetic-secret-exit'), false);
  }
});

test('definitively missing old VMs are acknowledged without resuming or replacing them', async () => {
  const id = 'b'.repeat(32);
  const env = dispatchEnvironment({ missingRuntime: true, runtimes: [{ id, lease_version: 2, state: 'completed', deadline: 1 }] });
  await env.run();
  const lookup = env.calls.find(value => value.sandboxGet).sandboxGet;
  assert.equal(lookup.name, `replay-job-${id}-2`);
  assert.equal(lookup.resume, false);
  const ack = env.calls.find(value => value.url?.endsWith('/runtime-cleaned'));
  assert.deepEqual(JSON.parse(ack.init.body), { job_id: id, lease_version: 2 });
  assert.equal(env.created.length, 1, 'only the independently claimed new job receives a VM');
});

test('alerts acknowledge only successful authenticated delivery with a stable idempotency key', async () => {
  const alert = { id: 'c'.repeat(32), code: 'QUEUE_DELAY', count: 180, created: 1800000000 };
  for (const webhookStatus of [204, 503]) {
    const env = dispatchEnvironment({ alerts: [alert], webhookStatus });
    assert.equal((await env.run()).status, 200);
    assert.equal(env.deliveries.length, 1);
    const delivery = env.deliveries[0];
    assert.equal(new Headers(delivery.init.headers).get('Idempotency-Key'), alert.id);
    assert.equal(delivery.init.redirect, 'error');
    assert.deepEqual(JSON.parse(delivery.init.body), { service: 'replay-live', ...alert });
    assert.equal(env.calls.filter(value => String(value.url).endsWith(`/alerts/${alert.id}/ack`)).length,
      webhookStatus === 204 ? 1 : 0, 'failed delivery remains pending for the next cron tick');
    assert.equal(env.logs.join('').includes(env.env.REPLAY_ALERT_WEBHOOK_TOKEN), false);
    assert.equal(env.created.length, 1);
  }
});

test('monitoring outage does not undo successful dispatch or prevent later cron work', async () => {
  const env = dispatchEnvironment({ monitoringFails: true });
  const response = await env.run();
  assert.equal(response.status, 200);
  assert.equal((await response.json()).started, 1);
});

test('slow cleanup is bounded so a backlog cannot starve new jobs', async () => {
  const runtimes = Array.from({ length: 20 }, (_, index) => ({ id: index.toString(16).padStart(32, '0'),
    lease_version: 1, state: 'completed', deadline: 1 }));
  const env = dispatchEnvironment({ runtimes, cleanupAdvanceMs: 16_000 });
  assert.equal((await env.run()).status, 200);
  assert.ok(env.calls.filter(value => value.sandboxGet).length < runtimes.length);
  assert.equal(env.created.length, 1);
});
