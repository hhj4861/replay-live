const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const source = fs.readFileSync(require('node:path').join(__dirname, '../lib/proxy-alerts.ts'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
const loaded = { exports: {} };
vm.runInNewContext(code, { module: loaded, exports: loaded.exports, AbortSignal, fetch, Buffer, URL });
const { deliverProxyAlert, proxyAlertText, readProxyBalance, monitorProxyQuota } = loaded.exports;
const alert = { id: 'a'.repeat(32), lease_token: 'b'.repeat(32), remaining_bytes: 100_000_000, threshold_bytes: 250_000_000 };
const env = { REPLAY_TELEGRAM_BOT_TOKEN: '123456789:' + 'x'.repeat(35), REPLAY_TELEGRAM_CHAT_ID: '123456789' };
const proxyEnv = { ...env, REPLAY_SOURCE_PROXY_URL: 'http://synthetic:secret@gw.dataimpulse.com:823' };
const stats = { status: 'ok', login: 'synthetic', total_traffic: 5_000_000_000, traffic_used: 4_900_000_000, traffic_left: 100_000_000 };

test('personal plan API uses fixed HTTPS Basic auth and returns only balance and observation time', async () => {
  const result = await readProxyBalance(proxyEnv, async (url, options) => {
    assert.equal(url, 'https://gw.dataimpulse.com:777/api/stats');
    assert.equal(options.headers.Authorization, `Basic ${Buffer.from('synthetic:secret').toString('base64')}`);
    assert.equal(options.redirect, 'error');
    assert.equal(options.cache, 'no-store');
    return Response.json(stats);
  });
  assert.equal(result.remaining_bytes, 100_000_000);
  assert.ok(Math.abs(result.observed_at - Date.now() / 1000) < 1);
  assert.deepEqual(Object.keys(result).sort(), ['observed_at', 'remaining_bytes']);
});

test('invalid or failed provider responses never report zero balance or leak credentials', async () => {
  for (const body of [null, {}, { ...stats, status: 'error' }, { ...stats, login: 'other' },
    { ...stats, traffic_left: -1 }, { ...stats, traffic_left: '100' }, { ...stats, traffic_left: 6e9 },
    { ...stats, traffic_used: 1.5 }, { balance: 5 }]) {
    await assert.rejects(readProxyBalance(proxyEnv, async () => Response.json(body)), /^Error: Proxy balance unavailable$/);
  }
  for (const response of [new Response('secret', { status: 401 }), new Response('x'.repeat(16_385))]) {
    await assert.rejects(readProxyBalance(proxyEnv, async () => response), /^Error: Proxy balance unavailable$/);
  }
  await assert.rejects(readProxyBalance({ REPLAY_SOURCE_PROXY_URL: 'http://synthetic:secret@evil.example:823' }, () => assert.fail()), /Proxy balance unavailable/);
  await assert.rejects(readProxyBalance(proxyEnv, async () => { throw new Error('secret'); }), /^Error: Proxy balance unavailable$/);
});

test('monitor observes the real balance before delivery and does nothing when observation fails', async () => {
  const calls = [];
  await monitorProxyQuota(async (path, body) => {
    calls.push(path);
    if (path === 'proxy-balance') assert.equal(body.remaining_bytes, stats.traffic_left);
    return { alert: null };
  }, proxyEnv, async () => Response.json(stats));
  assert.deepEqual(calls, ['proxy-balance', 'proxy-alerts/claim']);
  calls.length = 0;
  await assert.rejects(monitorProxyQuota(async path => calls.push(path), proxyEnv,
    async () => new Response('unavailable', { status: 503 })), /Proxy balance unavailable/);
  assert.deepEqual(calls, []);
});

test('leased alert sends shared balance and purchase link, then acknowledges', async () => {
  const calls = [];
  const control = async (path, body) => { calls.push(path); return path.endsWith('/claim') ? { alert } : { acknowledged: true }; };
  let message;
  const request = async (url, options) => {
    assert.ok(url.startsWith('https://api.telegram.org/bot'));
    assert.equal(options.redirect, 'error'); message = JSON.parse(options.body);
    return Response.json({ ok: true });
  };
  assert.equal(await deliverProxyAlert(control, env, request), true);
  assert.deepEqual(calls, ['proxy-alerts/claim', 'proxy-alerts/ack']);
  assert.match(message.text, /100 MB/);
  assert.match(message.text, /모든 사용자/);
  assert.equal(message.reply_markup.inline_keyboard[0][0].url, 'https://app.dataimpulse.com/plans');
});

test('delivery failures do not acknowledge and do not expose provider error or token', async () => {
  const calls = [];
  const control = async path => { calls.push(path); return { alert }; };
  await assert.rejects(deliverProxyAlert(control, env, async () => { throw new Error(env.REPLAY_TELEGRAM_BOT_TOKEN); }), /^Error: Proxy alert delivery unavailable$/);
  assert.deepEqual(calls, ['proxy-alerts/claim']);
});

test('no pending alert does not contact Telegram and malformed balances are refused', async () => {
  assert.equal(await deliverProxyAlert(async () => ({ alert: null }), env, () => assert.fail()), false);
  assert.throws(() => proxyAlertText({ ...alert, remaining_bytes: NaN }));
  assert.throws(() => proxyAlertText({ ...alert, remaining_bytes: -1 }));
});
