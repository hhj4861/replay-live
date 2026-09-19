/* Test the real TypeScript with isolated queue/dispatcher/network adapters.
 * No queue SDK calls, cloud requests, worker creation, or real credentials.
 */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const ts = require('../node_modules/typescript');

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

function fixture(overrides = {}) {
  const env = { REPLAY_COMMERCIAL: '1', REPLAY_DISPATCH_MODE: 'queue',
    REPLAY_CONTROL_URL: 'https://control.example', REPLAY_VERSION: 'synthetic-release-v1',
    REPLAY_CONTROL_TOKEN: 'synthetic-control-token-00000000000001', CRON_SECRET: 'synthetic-cron-secret-000000000000001', ...overrides };
  const calls = [];
  const state = { at: null, health: { version: env.REPLAY_VERSION }, fetchError: null, sendError: null, sendGate: null,
    responses: {}, timeouts: [], timeoutController: null, nowMillis: 1_800_000_000_250,
    dispatchStatus: 200, dispatchError: null, callback: null, options: null };
  const plain = value => JSON.parse(JSON.stringify(value));
  const sdk = {
    send: async (topic, message, options) => {
      calls.push({ operation: 'send', topic, message: plain(message), options: plain(options) });
      if (state.sendError) throw state.sendError;
      if (state.sendGate) await state.sendGate;
      return { messageId: 'synthetic-message-id' };
    },
    handleCallback: (handler, options) => {
      state.callback = handler;
      state.options = plain(options);
      return async () => {
        await handler({ v: 1 }, { topicName: 'replay-dispatch', region: 'iad1' });
        return Response.json({ status: 'success' });
      };
    },
  };
  const dispatch = async request => {
    calls.push({ operation: 'dispatch', request });
    if (state.dispatchError) throw state.dispatchError;
    return Response.json({}, { status: state.dispatchStatus });
  };
  const globals = { process: { env }, URL, Request, Response, Headers, Buffer, TextEncoder,
    AbortSignal: { any: AbortSignal.any, timeout: milliseconds => {
      state.timeouts.push(milliseconds);
      return state.timeoutController?.signal ?? AbortSignal.timeout(milliseconds);
    } },
    Date: class extends Date { static now() { return state.nowMillis; } },
    fetch: async (url, init) => {
      const operation = url.endsWith('/api/live') ? 'live' : url.endsWith('/internal/next-wakeup') ? 'next' : 'unrecognized';
      calls.push({ operation, url, init });
      if (state.fetchError) throw state.fetchError;
      if (state.responses[operation]) return state.responses[operation]();
      if (operation === 'live') return Response.json(state.health);
      if (operation === 'next') return Response.json({ at: state.at });
      throw new Error('Unmocked networking is forbidden');
    } };
  const helper = load('lib/dispatch-wakeup.ts', globals, { '@vercel/queue': sdk });
  const wake = load('api/wake.ts', globals, { 'node:crypto': crypto, '../lib/dispatch-wakeup.js': helper }).default.fetch;
  const consumer = load('api/dispatch-queue.ts', globals, { '@vercel/queue': sdk,
    './dispatch.js': { default: { fetch: dispatch } }, '../lib/dispatch-wakeup.js': helper }).default.fetch;
  function request(method = 'POST', options = {}) {
    const { body, token = method === 'GET' ? env.CRON_SECRET : env.REPLAY_CONTROL_TOKEN, headers = {} } = options;
    return new Request('https://studio.example/api/wake', { method,
      headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...headers },
      ...(body === undefined ? {} : { body, ...(body instanceof ReadableStream ? { duplex: 'half' } : {}) }) });
  }
  return { env, state, calls, globals, helper, wake, consumer, dispatch, request };
}

test('POST sends only immediate v1 metadata with region and seven-day retention', async () => {
  const f = fixture();
  const response = await f.wake(f.request('POST', { body: '{"v":1}' }));
  assert.equal(response.status, 202);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await response.json(), { accepted: true });
  assert.deepEqual(f.calls, [{ operation: 'send', topic: 'replay-dispatch', message: { v: 1 },
    options: { region: 'iad1', retentionSeconds: 604800 } }]);
});

test('empty POST and cron GET both wake, without introducing a deduplication key', async () => {
  const f = fixture();
  assert.equal((await f.wake(f.request())).status, 202);
  assert.equal((await f.wake(f.request('GET'))).status, 202);
  assert.equal(f.calls.length, 2);
  for (const call of f.calls) assert.equal(Object.hasOwn(call.options, 'idempotencyKey'), false);
});

test('GET and POST reject the other credential and absent/short credentials before sending', async () => {
  for (const method of ['GET', 'POST']) {
    const f = fixture();
    const other = method === 'GET' ? f.env.REPLAY_CONTROL_TOKEN : f.env.CRON_SECRET;
    for (const token of [other, '', 'incorrect']) {
      const response = await f.wake(f.request(method, { token }));
      assert.equal(response.status, 401);
      assert.equal(response.headers.get('cache-control'), 'no-store');
    }
    f.env[method === 'GET' ? 'CRON_SECRET' : 'REPLAY_CONTROL_TOKEN'] = 'short';
    assert.equal((await f.wake(f.request(method))).status, 401);
    assert.deepEqual(f.calls, []);
  }
});

test('only GET and POST can wake the dispatcher', async () => {
  const f = fixture();
  const response = await f.wake(f.request('PUT'));
  assert.equal(response.status, 405);
  assert.equal(response.headers.get('allow'), 'GET, POST');
  assert.deepEqual(f.calls, []);
});

for (const overrides of [{ REPLAY_COMMERCIAL: '0' }, { REPLAY_DISPATCH_MODE: 'cron' }, { REPLAY_DISPATCH_MODE: undefined }]) {
  test(`disabled mode does not publish or dispatch (${JSON.stringify(overrides)})`, async () => {
    const f = fixture(overrides);
    const response = await f.wake(f.request());
    assert.deepEqual(await response.json(), { disabled: true });
    await f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch);
    assert.deepEqual(f.calls, []);
    await assert.rejects(f.helper.publishDispatchWakeup(), /Dispatch wakeup unavailable/);
  });
}

test('wake rejects malformed, oversized and non-JSON bodies without echoing them', async () => {
  const f = fixture();
  for (const body of ['{}', 'null', '[]', '{"v":true}', '{"v":2}', '{"v":1,"v":1}', '{"v":1,"token":"secret"}', '\u00a0{"v":1}', ' '.repeat(33)]) {
    const response = await f.wake(f.request('POST', { body }));
    assert.equal(response.status, 400, body);
    assert.deepEqual(await response.json(), { detail: 'Invalid wakeup request' });
  }
  assert.equal((await f.wake(f.request('POST', { body: '{"v":1}', headers: { 'Content-Type': 'text/plain' } }))).status, 400);
  assert.equal((await f.wake(f.request('GET', { headers: { 'Content-Length': '1' } }))).status, 400);
  assert.equal((await f.wake(f.request('POST', { headers: { 'Content-Length': '33' } }))).status, 400);
  assert.deepEqual(f.calls, []);
});

test('wake caps chunked bodies at 32 bytes and cancels the reader', async () => {
  const f = fixture();
  let cancelled = false;
  const body = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(33)); }, cancel() { cancelled = true; } });
  const response = await f.wake(f.request('POST', { body }));
  assert.equal(response.status, 400);
  assert.equal(cancelled, true);
  assert.deepEqual(f.calls, []);
});

test('a request aborted during a pending body read cannot publish and cancels the reader', async () => {
  const f = fixture();
  let cancelled = false;
  const body = new ReadableStream({ cancel() { cancelled = true; } });
  const controller = new AbortController();
  const request = new Request('https://studio.example/api/wake', { method: 'POST', body, duplex: 'half', signal: controller.signal,
    headers: { Authorization: `Bearer ${f.env.REPLAY_CONTROL_TOKEN}`, 'Content-Type': 'application/json' } });
  const pending = f.wake(request);
  controller.abort();
  assert.equal((await pending).status, 400);
  assert.equal(cancelled, true);
  assert.deepEqual(f.calls, []);
});

test('queue SDK send failures are sanitized and returned as retryable HTTP failures', async () => {
  const f = fixture();
  f.state.sendError = new Error(`synthetic-secret ${f.env.REPLAY_CONTROL_TOKEN}`);
  const response = await f.wake(f.request());
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { detail: 'Dispatch wakeup unavailable' });
});

test('an uncertain SDK send returns within its eight-second budget and permits an explicit retry', async () => {
  const f = fixture();
  f.state.timeoutController = new AbortController();
  let acceptLate;
  f.state.sendGate = new Promise(resolve => { acceptLate = resolve; });
  const pending = f.wake(f.request());
  await Promise.resolve();
  f.state.timeoutController.abort();
  assert.equal((await pending).status, 503);
  assert.ok(f.state.timeouts.includes(8000));
  acceptLate();
  f.state.sendGate = null;
  f.state.timeoutController = null;
  assert.equal((await f.wake(f.request())).status, 202);
  assert.equal(f.calls.filter(call => call.operation === 'send').length, 2);
});

test('private callback delegates to SDK with visibility long enough for the dispatcher', async () => {
  const f = fixture();
  assert.deepEqual(f.state.options, { visibilityTimeoutSeconds: 300 });
  const response = await f.consumer(new Request('https://private.invalid/queue', { method: 'POST' }));
  assert.equal(response.status, 200);
  assert.deepEqual(f.calls.map(call => call.operation), ['live', 'dispatch', 'next']);
  const [live, dispatch, next] = f.calls;
  assert.equal(live.url, 'https://control.example/api/live');
  assert.equal(live.init.method, 'GET');
  assert.equal(live.init.headers, undefined, 'public version check receives no control token');
  for (const call of [live, next]) {
    assert.equal(call.init.redirect, 'error');
    assert.equal(call.init.cache, 'no-store');
    assert.ok(call.init.signal instanceof AbortSignal);
  }
  assert.equal(dispatch.request.url, 'https://internal.invalid/api/dispatch');
  assert.equal(dispatch.request.headers.get('authorization'), `Bearer ${f.env.CRON_SECRET}`);
  assert.equal(next.url, 'https://control.example/internal/next-wakeup');
  assert.equal(next.init.method, 'POST');
  assert.equal(next.init.headers.Authorization, `Bearer ${f.env.REPLAY_CONTROL_TOKEN}`);
  assert.equal(next.init.body, '{}');
});

test('stale deployment acknowledges the wakeup without dispatch, control claim or followup', async () => {
  const f = fixture();
  f.state.health = { version: 'synthetic-release-v2' };
  await f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch);
  assert.deepEqual(f.calls.map(call => call.operation), ['live']);
});

test('malformed message cannot reach the API or dispatcher', async () => {
  const f = fixture();
  for (const message of [null, [], {}, true, { v: 2 }, { v: '1' }, { v: 1, job: 'synthetic-job' }]) {
    await assert.rejects(f.helper.consumeDispatchWakeup(message, f.dispatch), /Dispatch wakeup unavailable/);
  }
  assert.deepEqual(f.calls, []);
});

test('invalid control origins and incomplete credentials fail before networking', async () => {
  for (const overrides of [
    { REPLAY_CONTROL_URL: 'http://control.example' }, { REPLAY_CONTROL_URL: 'https://user:secret@control.example' },
    { REPLAY_CONTROL_URL: 'https://control.example/api' }, { REPLAY_CONTROL_URL: 'https://control.example/?token=secret' },
    { REPLAY_CONTROL_URL: 'https://control.example/#secret' }, { REPLAY_CONTROL_URL: undefined },
    { REPLAY_CONTROL_TOKEN: 'short' }, { CRON_SECRET: '' }, { REPLAY_VERSION: '' },
  ]) {
    const f = fixture(overrides);
    await assert.rejects(f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch), /Dispatch wakeup unavailable/);
    assert.deepEqual(f.calls, []);
  }
});

test('invalid live response retries instead of treating missing version as an obsolete release', async () => {
  for (const health of [null, [], {}, { version: null }, { version: 1 }, { version: '' }, { version: '\ninvalid' }]) {
    const f = fixture(); f.state.health = health;
    await assert.rejects(f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch), /Dispatch wakeup unavailable/);
    assert.deepEqual(f.calls.map(call => call.operation), ['live']);
  }
});

for (const [at, delaySeconds, targetSeconds] of [[1_800_000_000, 60, 1_800_000_060], [1_800_000_030.4, 60, 1_800_000_060],
  [1_800_000_900.25, 930, 1_800_000_930], [1_800_000_000 + 9 * 86400, 5 * 86400, 1_800_432_000]]) {
  test(`next wakeup ${at} produces bounded delay ${delaySeconds} seconds`, async () => {
    const f = fixture(); f.state.at = at;
    await f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch);
    assert.deepEqual(f.calls.map(call => call.operation), ['live', 'dispatch', 'next', 'send']);
    assert.deepEqual(f.calls.at(-1), { operation: 'send', topic: 'replay-dispatch', message: { v: 1 },
      options: { region: 'iad1', retentionSeconds: 604800, delaySeconds, idempotencyKey: `wake-${f.env.REPLAY_VERSION}-${targetSeconds}` } });
  });
}

test('invalid schedule data retries without scheduling a guessed time', async () => {
  for (const at of [undefined, '1800000001', false, -1, {}, [], Number.MAX_VALUE]) {
    const f = fixture(); f.state.at = at;
    await assert.rejects(f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch), /Dispatch wakeup unavailable/);
    assert.deepEqual(f.calls.map(call => call.operation), ['live', 'dispatch', 'next']);
  }
});

test('publisher rejects invalid followup times and never publishes user-supplied payloads', async () => {
  const f = fixture();
  for (const at of ['1800000001', false, {}, -1, NaN, Infinity, Number.MAX_VALUE]) {
    await assert.rejects(f.helper.publishDispatchWakeup(at), /Dispatch wakeup unavailable/);
  }
  assert.deepEqual(f.calls, []);
});

test('duplicate future followups share a key, while consumption at that target produces a new key', async () => {
  const f = fixture(); f.state.at = 1_800_000_100;
  await f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch);
  await f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch);
  const first = f.calls.filter(call => call.operation === 'send');
  assert.equal(first[0].options.idempotencyKey, first[1].options.idempotencyKey);
  assert.equal(first[0].options.idempotencyKey, `wake-${f.env.REPLAY_VERSION}-1800000120`);
  f.state.nowMillis = 1_800_000_120_250;
  await f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch);
  assert.equal(f.calls.at(-1).options.idempotencyKey, `wake-${f.env.REPLAY_VERSION}-1800000180`);
  assert.equal(f.calls.at(-1).options.delaySeconds, 60);
});

test('future followup keys are release-specific and the minimum delay is 30 seconds on a boundary', async () => {
  const f = fixture(); f.state.nowMillis = 1_800_000_000_000;
  await f.helper.publishDispatchWakeup(1_800_000_000);
  f.env.REPLAY_VERSION = 'synthetic-release-v2';
  await f.helper.publishDispatchWakeup(1_800_000_000);
  assert.equal(f.calls[0].options.delaySeconds, 30);
  assert.notEqual(f.calls[0].options.idempotencyKey, f.calls[1].options.idempotencyKey);
});

test('dispatch failure retries without scheduling or leaking downstream error text', async () => {
  const f = fixture(); f.state.dispatchStatus = 503;
  await assert.rejects(f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch), /Dispatch wakeup unavailable/);
  assert.deepEqual(f.calls.map(call => call.operation), ['live', 'dispatch']);
  f.state.dispatchError = new Error(`synthetic-secret ${f.env.CRON_SECRET}`);
  await assert.rejects(f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch), error => {
    assert.equal(error.message, 'Dispatch wakeup unavailable');
    assert.equal(error.cause, undefined);
    return true;
  });
});

test('fetch failure is sanitized; no dispatcher or followup is attempted', async () => {
  const f = fixture(); f.state.fetchError = new Error('https://control.example?secret=synthetic-secret');
  await assert.rejects(f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch), error => {
    assert.equal(error.message, 'Dispatch wakeup unavailable'); assert.equal(error.cause, undefined); return true;
  });
  assert.deepEqual(f.calls.map(call => call.operation), ['live']);
});

test('live and next-wakeup HTTP/JSON failures retry without following redirects or publishing', async () => {
  for (const operation of ['live', 'next']) {
    for (const result of [() => new Response('synthetic-secret', { status: 503 }),
      () => new Response(null, { status: 307, headers: { Location: 'https://other.example' } }),
      () => new Response('{"invalid":"synthetic-secret"'),
      () => { throw new Error('synthetic-secret fetch failure'); }]) {
      const f = fixture(); f.state.responses[operation] = result;
      await assert.rejects(f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch), error => {
        assert.equal(error.message, 'Dispatch wakeup unavailable'); assert.equal(error.cause, undefined); return true;
      });
      assert.deepEqual(f.calls.map(call => call.operation), operation === 'live' ? ['live'] : ['live', 'dispatch', 'next']);
    }
  }
});

test('failed followup publish retries with the same future key and no credentials in queue arguments', async () => {
  const f = fixture(); f.state.at = 1_800_000_100; f.state.sendError = new Error('synthetic-sdk-secret');
  await assert.rejects(f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch), /Dispatch wakeup unavailable/);
  f.state.sendError = null;
  await f.helper.consumeDispatchWakeup({ v: 1 }, f.dispatch);
  assert.equal(f.calls.filter(call => call.operation === 'dispatch').length, 2);
  assert.equal(f.calls.filter(call => call.operation === 'send').length, 2);
  const sent = f.calls.filter(call => call.operation === 'send');
  assert.equal(sent[0].options.idempotencyKey, sent[1].options.idempotencyKey);
  assert.equal(JSON.stringify(f.calls.filter(call => call.operation === 'send')).includes('synthetic-control'), false);
});
