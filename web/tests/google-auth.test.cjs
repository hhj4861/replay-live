/* CommonJS executes the real browser module in an isolated synthetic environment. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const source = ts.transpileModule(fs.readFileSync(require('node:path').join(__dirname, '../lib/google-auth.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const challenge = { challenge_id: 'c'.repeat(32), challenge_secret: 's'.repeat(43), nonce: 'n'.repeat(43), expires_at: Date.now() / 1000 + 300 };
const session = (letter = 'a', seconds = 3600) => ({ token: letter.repeat(43), expires_at: Date.now() / 1000 + seconds, absolute_expires_at: Date.now() / 1000 + 86400 });
function environment() {
  const values = new Map(); const events = []; const calls = [];
  let fetcher = () => Response.json(session());
  const module = { exports: {} };
  vm.runInNewContext(source, { exports: module.exports, module, Date, AbortSignal,
    __REPLAY_LOGIN__: { provider: 'google', google_client_id: '12345-synthetic.apps.googleusercontent.com' },
    __REPLAY_API_BASE__: 'https://api.synthetic.invalid/api',
    sessionStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) },
    window: { dispatchEvent: event => events.push(event.type) }, CustomEvent: class { constructor(type) { this.type = type; } },
    fetch: async (url, init) => { calls.push({ url, ...init }); return fetcher(url, init); },
  });
  return { auth: module.exports, values, events, calls, useFetch: fn => { fetcher = fn; } };
}
async function login(env, value = session()) {
  env.useFetch(() => Response.json(value));
  await env.auth.finishGoogleLogin('synthetic-google-id-credential', challenge, env.auth.beginGoogleLogin());
}

test('nonce exchange stores only the application token and sends bounded credential-free requests', async () => {
  const env = environment(); env.useFetch(() => Response.json(challenge));
  const value = await env.auth.googleChallenge();
  assert.equal(value.nonce, challenge.nonce);
  await login(env);
  assert.equal(await env.auth.googleAccessToken(), 'a'.repeat(43));
  const stored = JSON.stringify([...env.values]);
  for (const secret of ['synthetic-google-id-credential', challenge.nonce, challenge.challenge_secret]) assert.ok(!stored.includes(secret));
  assert.equal(env.calls[1].url, 'https://api.synthetic.invalid/api/auth/google/exchange');
  assert.equal(JSON.parse(env.calls[1].body).challenge_secret, challenge.challenge_secret);
  for (const call of env.calls) {
    assert.equal(call.credentials, 'omit'); assert.equal(call.redirect, 'error');
    assert.equal(call.headers['X-Replay-Client'], '1'); assert.ok(call.signal instanceof AbortSignal);
  }
});

test('parallel refresh has one request and all consumers receive the rotated token', async () => {
  const env = environment(); await login(env, session('a', 15));
  let resolve; env.useFetch(() => new Promise(done => { resolve = done; }));
  const first = env.auth.googleAccessToken(); const second = env.auth.googleAccessToken(true);
  assert.equal(env.calls.length, 2);
  resolve(Response.json(session('b')));
  assert.deepEqual(await Promise.all([first, second]), ['b'.repeat(43), 'b'.repeat(43)]);
  assert.equal(env.calls[1].headers.Authorization, 'Bearer ' + 'a'.repeat(43));
  assert.equal(await env.auth.googleAccessToken(), 'b'.repeat(43));
});

test('Google identity changes for login and logout but token rotation preserves the request identity', async () => {
  const env = environment();
  const initial = env.auth.googleAuthIdentity();
  await login(env, session('a', 15));
  const identity = env.auth.googleAuthIdentity();
  assert.notEqual(identity, initial);
  env.useFetch(() => Response.json(session('b')));
  await env.auth.googleAccessToken(true);
  assert.equal(env.auth.googleAuthIdentity(), identity);
  env.auth.clearGoogleSession();
  assert.notEqual(env.auth.googleAuthIdentity(), identity);
});

test('a delayed old-token 401 reuses the newer Google token without retiring it again', async () => {
  const env = environment();
  await login(env);
  env.useFetch(() => Response.json(session('b')));
  assert.equal(await env.auth.googleAccessToken(true, 'a'.repeat(43)), 'b'.repeat(43));
  const before = env.calls.length;
  assert.equal(await env.auth.googleAccessToken(true, 'a'.repeat(43)), 'b'.repeat(43));
  assert.equal(env.calls.length, before);
});

test('logout during exchange never resurrects a cancelled login', async () => {
  const env = environment(); let resolve;
  env.useFetch(() => new Promise(done => { resolve = done; }));
  const pending = env.auth.finishGoogleLogin('credential', challenge, env.auth.beginGoogleLogin());
  env.auth.clearGoogleSession(); resolve(Response.json(session()));
  await assert.rejects(pending, /취소/);
  assert.equal(env.auth.hasGoogleSession(), false); assert.equal(env.values.size, 0);
});

test('late refresh after logout or a newer login cannot replace the new identity', async () => {
  const env = environment(); await login(env, session('a', 15)); let resolve;
  env.useFetch(() => new Promise(done => { resolve = done; }));
  const pending = env.auth.googleAccessToken(); env.auth.clearGoogleSession();
  await login(env, session('c'));
  resolve(Response.json(session('b'))); await assert.rejects(pending, /취소/);
  assert.equal(await env.auth.googleAccessToken(), 'c'.repeat(43));
  assert.equal(env.events.length, 0);
});

test('refresh failure removes local authorization and returns to sign-in', async () => {
  const env = environment(); await login(env, session('a', 15));
  env.useFetch(() => Response.json({ detail: '다시 로그인하세요.' }, { status: 401 }));
  await assert.rejects(env.auth.googleAccessToken(), /다시 로그인/);
  assert.equal(env.auth.hasGoogleSession(), false); assert.equal(env.values.size, 0);
  assert.deepEqual(env.events, ['replay-signin-required']);
});

test('malformed and expired sessions and challenges cannot authenticate', async () => {
  const env = environment();
  for (const value of [session('a', -1), { ...session(), token: 'bad token' }, { ...session(), absolute_expires_at: Date.now() / 1000 + 90000 }, null]) {
    await assert.rejects(login(env, value), /로그인 응답/); assert.equal(env.auth.hasGoogleSession(), false);
  }
  for (const value of [{ ...challenge, nonce: '../unsafe' }, { ...challenge, expires_at: 1 }, null]) {
    env.useFetch(() => Response.json(value)); await assert.rejects(env.auth.googleChallenge(), /다시 시작/);
  }
  env.values.set('replay-google-session', '{bad json'); assert.equal(env.auth.hasGoogleSession(), false);
});

test('expired attempt rejects before credential transmission and non-JSON errors stay readable', async () => {
  const env = environment(); const attempt = env.auth.beginGoogleLogin(); env.auth.cancelGoogleLogin(attempt);
  await assert.rejects(env.auth.finishGoogleLogin('credential', challenge, attempt), /취소/); assert.equal(env.calls.length, 0);
  env.useFetch(() => new Response('<html>upstream unavailable</html>', { status: 502 }));
  await assert.rejects(env.auth.googleChallenge(), /Google 로그인 연결/);
});
