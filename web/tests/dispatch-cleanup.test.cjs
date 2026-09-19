/* Exercise the real dispatcher with synthetic control responses and Sandbox
 * adapters. No cloud SDK calls, worker launches, or real credentials. */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const net = require('node:net');
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

function fixture(mode = 'queue') {
  const env = { REPLAY_COMMERCIAL: '1', REPLAY_DISPATCH_MODE: mode,
    REPLAY_CONTROL_URL: 'https://control.example', REPLAY_VERSION: 'synthetic-release',
    REPLAY_WORKER_SNAPSHOT_ID: 'synthetic-snapshot',
    REPLAY_CONTROL_TOKEN: 'synthetic-control-token-00000000000001', CRON_SECRET: 'synthetic-cron-secret-000000000000001' };
  const calls = [];
  const logs = [];
  const state = { now: 0, wall: 1_800_000_000_000, getError: null, stopError: null, statuses: {},
    advanceAfterRuntimeList: 0,
    runtimes: [{ id: 'synthetic-job', lease_version: 3, state: 'completed', deadline: 1_800_000_060 }] };
  class APIError extends Error {
    constructor(status) { super('synthetic-upstream-sensitive-text'); this.response = { status }; }
  }
  const sdk = { APIError, Sandbox: {
    get: async options => {
      calls.push({ operation: 'sandbox-get', name: options.name, resume: options.resume });
      assert.ok(options.signal instanceof AbortSignal);
      if (state.getError) throw state.getError;
      return { stop: async options => {
        calls.push({ operation: 'sandbox-stop' });
        assert.ok(options.signal instanceof AbortSignal);
        if (state.stopError) throw state.stopError;
      } };
    },
    create: async () => { calls.push({ operation: 'forbidden-create' }); throw new Error('Worker creation is forbidden'); },
  } };
  const globals = { process: { env }, URL, Request, Response, Headers, Buffer, AbortSignal,
    performance: { now: () => state.now }, Date: class extends Date { static now() { return state.wall; } },
    console: { info: value => logs.push(value), error: value => logs.push(value) },
    fetch: async (url, init) => {
      const parsed = new URL(url);
      assert.equal(parsed.origin, env.REPLAY_CONTROL_URL);
      const operation = parsed.pathname.replace(/^\/internal\//, '');
      calls.push({ operation, body: init.body ? JSON.parse(init.body) : undefined });
      if (operation === '/api/live') return Response.json({ version: env.REPLAY_VERSION });
      assert.equal(init.method, 'POST');
      assert.equal(init.redirect, 'error');
      assert.equal(init.headers.Authorization, `Bearer ${env.REPLAY_CONTROL_TOKEN}`);
      assert.ok(init.signal instanceof AbortSignal);
      if (state.statuses[operation]) return new Response('synthetic-upstream-sensitive-text', { status: state.statuses[operation] });
      if (operation === 'runtimes') {
        state.now += state.advanceAfterRuntimeList;
        return Response.json({ runtimes: state.runtimes });
      }
      if (operation === 'runtime-cleaned' || operation === 'maintenance') return Response.json({});
      if (operation === 'claim') return Response.json({ job: null });
      if (operation === 'monitor') return Response.json({ alerts_pending: 0 });
      if (operation === 'next-wakeup') return Response.json({ at: null });
      throw new Error('Unmocked networking is forbidden');
    } };
  const dispatch = load('api/dispatch.ts', globals, {
    'node:crypto': crypto, 'node:net': net, '@vercel/sandbox': sdk,
    '../lib/worker-network-policy.js': load('lib/worker-network-policy.ts', {}, {}),
  }).default.fetch;
  const wake = load('lib/dispatch-wakeup.ts', globals, { '@vercel/queue': {
    send: async () => { calls.push({ operation: 'queue-send' }); },
  } });
  const run = () => dispatch(new Request('https://studio.example/api/dispatch', {
    method: 'GET', headers: { Authorization: `Bearer ${env.CRON_SECRET}` },
  }));
  return { state, calls, logs, APIError, run, dispatch, wake };
}

test('successful terminal Sandbox stop is acknowledged and returns 200', async () => {
  const f = fixture();
  const response = await f.run();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await response.json(), { started: 0, stopped: 1, version: 'synthetic-release' });
  assert.deepEqual(f.calls.map(call => call.operation), ['runtimes', 'sandbox-get', 'sandbox-stop', 'runtime-cleaned', 'maintenance', 'claim', 'monitor']);
  assert.deepEqual(f.calls[1], { operation: 'sandbox-get', name: 'replay-job-synthetic-job-3', resume: false });
  assert.deepEqual(f.calls[3].body, { job_id: 'synthetic-job', lease_version: 3 });
});

test('failed Sandbox stop returns 503 without acknowledging or exposing upstream text', async () => {
  const f = fixture(); f.state.stopError = new Error('synthetic-upstream-sensitive-text');
  const response = await f.run();
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { started: 0, stopped: 0, version: 'synthetic-release' });
  assert.equal(f.calls.some(call => call.operation === 'runtime-cleaned'), false);
  assert.equal(f.logs.join('').includes('synthetic-upstream-sensitive-text'), false);
  assert.ok(JSON.parse(f.logs.at(-1)).deferred.includes('cleanup'));
});

for (const failure of ['lookup', 'runtime-list', 'acknowledgement']) {
  test(`queue cleanup ${failure} failure uses existing-message retry response`, async () => {
    const f = fixture();
    if (failure === 'lookup') f.state.getError = new f.APIError(403);
    if (failure === 'runtime-list') f.state.statuses.runtimes = 502;
    if (failure === 'acknowledgement') f.state.statuses['runtime-cleaned'] = 502;
    const response = await f.run();
    assert.equal(response.status, 503);
    assert.equal((await response.json()).stopped, 0);
    assert.equal(f.calls.some(call => call.operation === 'forbidden-create'), false);
  });
}

test('already absent Sandbox is acknowledged without attempting a stop', async () => {
  const f = fixture(); f.state.getError = new f.APIError(404);
  assert.equal((await f.run()).status, 200);
  assert.equal(f.calls.some(call => call.operation === 'sandbox-stop'), false);
  assert.deepEqual(f.calls.find(call => call.operation === 'runtime-cleaned').body, { job_id: 'synthetic-job', lease_version: 3 });
});

test('failed acknowledgement of an absent Sandbox also returns 503', async () => {
  const f = fixture(); f.state.getError = new f.APIError(404); f.state.statuses['runtime-cleaned'] = 502;
  assert.equal((await f.run()).status, 503);
});

test('cron mode preserves its existing successful tick response on cleanup failure', async () => {
  const f = fixture('cron'); f.state.stopError = new Error('synthetic-upstream-sensitive-text');
  const response = await f.run();
  assert.equal(response.status, 200);
  assert.equal((await response.json()).stopped, 0);
  assert.ok(JSON.parse(f.logs.at(-1)).deferred.includes('cleanup'));
});

test('ordinary cleanup phase budget exhaustion defers remaining work without failing the queue message', async () => {
  const f = fixture(); f.state.advanceAfterRuntimeList = 40_000;
  assert.equal((await f.run()).status, 200);
  assert.equal(f.calls.some(call => call.operation === 'sandbox-get'), false);
  assert.ok(JSON.parse(f.logs.at(-1)).deferred.includes('cleanup'));
});

test('an active runtime before its deadline is neither stopped nor marked failed', async () => {
  const f = fixture(); f.state.runtimes[0].state = 'streaming';
  assert.equal((await f.run()).status, 200);
  assert.equal(f.calls.some(call => call.operation === 'sandbox-get'), false);
  assert.deepEqual(JSON.parse(f.logs.at(-1)).deferred, []);
});

test('real consumer rejects the failed cleanup tick without creating a fresh successor message', async () => {
  const f = fixture(); f.state.stopError = new f.APIError(403);
  await assert.rejects(f.wake.consumeDispatchWakeup({ v: 1 }, f.dispatch), /Dispatch wakeup unavailable/);
  assert.equal(f.calls.some(call => call.operation === 'next-wakeup' || call.operation === 'queue-send'), false);
  assert.equal(f.calls.filter(call => call.operation === 'sandbox-stop').length, 1);
});
