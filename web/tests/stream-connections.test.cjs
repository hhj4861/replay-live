/* Synthetic storage/network only. Crypto uses Node's real WebCrypto implementation. */
/* oxlint-disable typescript/no-require-imports, next/no-assign-module-variable */
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const ts = require('../node_modules/typescript');
const watchFilename = path.join(__dirname, '../lib/watch-links.ts');
const watchModule = { exports: {} };
vm.runInNewContext(ts.transpileModule(fs.readFileSync(watchFilename, 'utf8'), { fileName: watchFilename,
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText,
{ module: watchModule, exports: watchModule.exports }, { filename: watchFilename });

// An asynchronous, transaction-serialized IndexedDB double. Values pass through
// structuredClone, including real nonextractable CryptoKeys, just as browser IDB.
function memoryIndexedDB() {
  const stores = new Map();
  const transactions = [];
  let running = false;
  let created = false;
  const factory = { opens: 0, records: name => [...(stores.get(name)?.values.values() || [])],
    open() {
      factory.opens++;
      const request = {};
      queueMicrotask(() => {
        request.result = database;
        if (!created) { created = true; request.onupgradeneeded?.(); }
        request.onsuccess?.();
      });
      return request;
    },
  };
  function nextTransaction() {
    if (running || !transactions.length) return;
    running = true;
    const current = transactions.shift();
    const original = stores.get(current.name);
    const values = new Map([...original.values].map(([key, value]) => [key, structuredClone(value)]));
    let ended = false;
    const finish = () => {
      if (ended) return;
      ended = true;
      if (current.aborted) current.tx.onabort?.();
      else { if (current.mode === 'readwrite') original.values = values; current.tx.oncomplete?.(); }
      running = false; queueMicrotask(nextTransaction);
    };
    const tick = () => {
      if (current.aborted) { finish(); return; }
      const operation = current.operations.shift();
      if (!operation) { finish(); return; }
      try { operation.request.result = structuredClone(operation.run(values)); operation.request.onsuccess?.(); }
      catch (error) { operation.request.error = error; current.aborted = true; operation.request.onerror?.(); }
      queueMicrotask(tick);
    };
    queueMicrotask(tick);
  }
  const database = {
    objectStoreNames: { contains: name => stores.has(name) },
    createObjectStore(name, { keyPath }) {
      stores.set(name, { keyPath, indexes: new Map(), values: new Map() });
      return { createIndex(index, key) { stores.get(name).indexes.set(index, key); } };
    },
    close() {},
    transaction(name, mode) {
      const current = { name, mode, operations: [], aborted: false };
      function enqueue(run) { const request = {}; current.operations.push({ request, run }); return request; }
      const tx = { abort() { current.aborted = true; }, objectStore() {
        return {
          get: key => enqueue(values => values.get(key)),
          put: value => enqueue(values => { if (mode !== 'readwrite') throw new Error('Readonly'); const key = value[stores.get(name).keyPath]; values.set(key, structuredClone(value)); return key; }),
          delete: key => enqueue(values => { if (mode !== 'readwrite') throw new Error('Readonly'); values.delete(key); }),
          index: index => ({ getAll: query => enqueue(values => [...values.values()].filter(value => value[stores.get(name).indexes.get(index)] === query)) }),
        };
      } };
      current.tx = tx; transactions.push(current); queueMicrotask(nextTransaction); return tx;
    },
  };
  return factory;
}

function environment({ hostname = 'localhost', indexedDB = memoryIndexedDB(), crypto = webcrypto } = {}) {
  let identity = 'account-a';
  let handler = () => { throw new Error('Unexpected network request'); };
  const calls = [];
  const storage = { getItem() { throw new Error('Plain storage forbidden'); }, setItem() { throw new Error('Plain storage forbidden'); } };
  const api = {
    sessionIdentity: () => identity,
    assertSessionIdentity: expected => { if (identity !== expected) throw new Error('계정이 변경되어 취소되었습니다.'); },
    api: async (url, init) => { calls.push({ url, init }); return handler(url, init); },
    request: async (url, init) => { calls.push({ url, init }); return handler(url, init); },
  };
  const filename = path.join(__dirname, '../lib/stream-connections.ts');
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(code, { exports: module.exports, module, require: name => {
    if (name === './watch-links') return watchModule.exports;
    assert.equal(name, './api'); return api;
  },
    crypto, indexedDB, TextEncoder, TextDecoder, Uint8Array, ArrayBuffer, URL, Date, AbortSignal,
    window: { location: { hostname }, localStorage: storage, sessionStorage: storage }, localStorage: storage, sessionStorage: storage,
  }, { filename });
  return { ...module.exports, indexedDB, calls, setIdentity: value => { identity = value; }, useNetwork: value => { handler = value; } };
}

const ownerA = { tenant_id: 'synthetic-tenant', subject: 'synthetic-user-a' };
const ownerB = { tenant_id: 'synthetic-tenant', subject: 'synthetic-user-b' };
const value = { server_url: 'rtmps://stream.example.com:443/app', stream_key: 'synthetic-stream-key-123', channel_url: '' };
const plain = value => JSON.parse(JSON.stringify(value));

test('only exact loopback page hosts select browser storage', () => {
  for (const hostname of ['localhost', '127.0.0.1', '[::1]', '::1']) assert.equal(environment({ hostname }).streamConnectionLocation(), 'browser');
  for (const hostname of ['studio.example.com', 'localhost.example.com', 'studio.localhost', '127.0.0.2']) assert.equal(environment({ hostname }).streamConnectionLocation(), 'account');
});

test('local save encrypts URL and key, persists a nonextractable AES key, and only explicit use returns the secret', async () => {
  const decryptedFields = [];
  const crypto = { getRandomValues: values => webcrypto.getRandomValues(values), subtle: {
    digest: (...args) => webcrypto.subtle.digest(...args), generateKey: (...args) => webcrypto.subtle.generateKey(...args), encrypt: (...args) => webcrypto.subtle.encrypt(...args),
    decrypt: (algorithm, ...args) => { decryptedFields.push(JSON.parse(new TextDecoder().decode(algorithm.additionalData)).at(-1)); return webcrypto.subtle.decrypt(algorithm, ...args); },
  } };
  const env = environment({ crypto });
  const store = await env.createStreamConnectionStore(ownerA);
  const saved = await store.save('youtube', value);
  assert.equal(saved.has_stream_key, true); assert.equal(saved.stream_key, undefined);
  const records = env.indexedDB.records('connections');
  assert.equal(records.length, 1);
  assert.equal(records[0].server_url, undefined); assert.equal(records[0].stream_key, undefined);
  const serialized = JSON.stringify(records);
  for (const secret of [value.stream_key, value.server_url, ownerA.subject, ownerA.tenant_id]) assert.equal(serialized.includes(secret), false);
  const key = env.indexedDB.records('keys')[0].key;
  assert.equal(key.extractable, false); assert.equal(key.algorithm.name, 'AES-GCM');
  await assert.rejects(webcrypto.subtle.exportKey('raw', key));
  const listed = await store.list();
  assert.equal(listed.length, 1); assert.equal(listed[0].server_url, value.server_url); assert.equal(listed[0].stream_key, undefined);
  assert.deepEqual(decryptedFields, ['server', 'channel'], 'metadata listing must not decrypt saved stream keys');
  assert.deepEqual(plain(await store.use('youtube')), { target: 'youtube', ...value });
  assert.equal(decryptedFields.includes('key'), true); assert.equal(env.calls.length, 0);
});

test('local saved connections survive a new module/session, remain owner scoped, and delete without changing another platform', async () => {
  const indexedDB = memoryIndexedDB();
  const first = environment({ indexedDB });
  const initial = await first.createStreamConnectionStore(ownerA);
  await initial.save('youtube', value); await initial.save('twitch', { ...value, stream_key: 'synthetic-twitch-key' });
  const second = environment({ indexedDB });
  const resumed = await second.createStreamConnectionStore(ownerA);
  assert.deepEqual(plain(await resumed.use('youtube')), { target: 'youtube', ...value });
  const otherUser = await second.createStreamConnectionStore(ownerB);
  const otherTenant = await second.createStreamConnectionStore({ ...ownerA, tenant_id: 'other-tenant' });
  assert.equal((await otherUser.list()).length, 0); assert.equal((await otherTenant.list()).length, 0);
  await assert.rejects(otherUser.use('youtube')); await assert.rejects(otherTenant.use('youtube'));
  await resumed.remove('youtube');
  assert.deepEqual(plain((await resumed.list()).map(record => record.target)), ['twitch']);
  await assert.rejects(resumed.use('youtube')); assert.equal(second.calls.length, 0);
});

test('concurrent first saves share one owner encryption key across tabs', async () => {
  const indexedDB = memoryIndexedDB();
  const first = await environment({ indexedDB }).createStreamConnectionStore(ownerA);
  const second = await environment({ indexedDB }).createStreamConnectionStore(ownerA);
  await Promise.all([first.save('youtube', value), second.save('twitch', { ...value, stream_key: 'second-platform-key' })]);
  assert.equal(indexedDB.records('keys').length, 1);
  assert.equal((await first.use('youtube')).stream_key, value.stream_key);
  assert.equal((await first.use('twitch')).stream_key, 'second-platform-key');
});

test('AES-GCM associated data prevents cross-platform or cross-owner ciphertext substitution', async () => {
  const env = environment(); const store = await env.createStreamConnectionStore(ownerA);
  await store.save('youtube', value); await store.save('twitch', { ...value, stream_key: 'second-platform-key' });
  const records = env.indexedDB.records('connections');
  const youtube = records.find(record => record.target === 'youtube');
  const twitch = records.find(record => record.target === 'twitch');
  youtube.secret = twitch.secret;
  await assert.rejects(store.use('youtube'), error => !error.message.includes('second-platform-key'));
  assert.equal((await store.use('twitch')).stream_key, 'second-platform-key');
  const other = await env.createStreamConnectionStore(ownerB);
  await other.save('youtube', { ...value, stream_key: 'other-owner-key' });
  const latest = env.indexedDB.records('connections');
  latest.find(record => record.owner === store.namespace && record.target === 'youtube').secret
    = latest.find(record => record.owner === other.namespace).secret;
  await assert.rejects(store.use('youtube'), error => !error.message.includes('other-owner-key'));
  assert.equal((await other.use('youtube')).stream_key, 'other-owner-key');
});

test('cloud storage uses only account API and no IndexedDB or plaintext browser persistence', async () => {
  const indexedDB = { open() { throw new Error('Cloud must not open browser storage'); } };
  const env = environment({ hostname: 'studio.example.com', indexedDB });
  env.useNetwork((url, init = {}) => {
    if (init.method === 'DELETE') return { status: 204 };
    if (url.endsWith('/use')) return { target: 'youtube', ...value };
    const result = { target: 'youtube', server_url: value.server_url, updated_at: 123, has_stream_key: true, stream_key: 'must-not-leak-in-metadata' };
    return init.method === 'PUT' ? result : [result];
  });
  const store = await env.createStreamConnectionStore(ownerA);
  assert.equal(store.location, 'account');
  assert.equal((await store.list())[0].stream_key, undefined);
  assert.equal((await store.save('youtube', value)).stream_key, undefined);
  assert.deepEqual(plain(await store.use('youtube')), { target: 'youtube', ...value });
  await store.remove('youtube');
  assert.deepEqual(env.calls.map(call => [call.url, call.init?.method || 'GET']), [
    ['/stream-connections', 'GET'], ['/stream-connections/youtube', 'PUT'],
    ['/stream-connections/youtube/use', 'POST'], ['/stream-connections/youtube', 'DELETE'],
  ]);
  assert.equal(env.calls.every(call => call.init.cache === 'no-store'), true);
  assert.equal(env.calls.every(call => call.init.signal instanceof AbortSignal), true, 'all account storage requests have an abort deadline');
  assert.deepEqual(JSON.parse(env.calls[1].init.body), value);
});

test('cloud late secret response cannot cross an account change and old stores cannot issue more requests', async () => {
  const env = environment({ hostname: 'studio.example.com' });
  const store = await env.createStreamConnectionStore(ownerA);
  let finish;
  env.useNetwork(() => new Promise(resolve => { finish = resolve; }));
  const pending = store.use('youtube');
  env.setIdentity('account-b'); finish({ target: 'youtube', ...value });
  await assert.rejects(pending, /계정이 변경/);
  await assert.rejects(store.list(), /계정이 변경/);
  assert.equal(env.calls.length, 1); assert.equal(env.indexedDB.opens, 0);
});

test('account switch during owner hashing never creates a usable old-owner store', async () => {
  let finish;
  const env = environment({ crypto: { subtle: { digest: () => new Promise(resolve => { finish = resolve; }) } } });
  const pending = env.createStreamConnectionStore(ownerA);
  env.setIdentity('account-b'); finish(new ArrayBuffer(32));
  await assert.rejects(pending, /계정이 변경/); assert.equal(env.indexedDB.opens, 0);
});

test('account switch during local encryption prevents the record write', async () => {
  let changed = false;
  const crypto = { getRandomValues: values => webcrypto.getRandomValues(values), subtle: {
    digest: (...args) => webcrypto.subtle.digest(...args), generateKey: (...args) => webcrypto.subtle.generateKey(...args),
    encrypt: async (...args) => { const result = await webcrypto.subtle.encrypt(...args); if (!changed) { changed = true; env.setIdentity('account-b'); } return result; },
  } };
  const env = environment({ crypto }); const store = await env.createStreamConnectionStore(ownerA);
  await assert.rejects(store.save('youtube', value), /계정이 변경/);
  assert.equal(env.indexedDB.records('connections').length, 0);
});

test('unavailable IndexedDB fails closed without localStorage or server fallback', async () => {
  const env = environment({ indexedDB: null });
  const store = await env.createStreamConnectionStore(ownerA);
  await assert.rejects(store.save('youtube', value), /저장하지 못했습니다/);
  assert.equal(env.calls.length, 0);
});

test('server errors and malformed connections never echo supplied URLs or keys', async () => {
  const env = environment({ hostname: 'studio.example.com' });
  const store = await env.createStreamConnectionStore(ownerA);
  env.useNetwork(() => { throw new Error(`${value.server_url} ${value.stream_key}`); });
  await assert.rejects(store.use('youtube'), error => !error.message.includes(value.stream_key) && !error.message.includes(value.server_url));
  assert.throws(() => store.save('youtube', { ...value, server_url: 'https://user:pass@example.com' }), /URL과 스트림 키/);
  assert.throws(() => store.save('local', value), /플랫폼/);
  assert.equal(env.calls.length, 1);
});

test('local connection syntax rejects the same unsafe keys, hosts, ports and paths before opening storage', async () => {
  const env = environment(); const store = await env.createStreamConnectionStore(ownerA);
  const invalidKeys = ['abc#def', 'rtmp://stream.example.com/app', 'abc%0adef', 'abc%23def', 'abc%zz', 'abc\\def', "abc'def", '-starts-invalid'];
  const invalidServers = ['rtmp://127.0.0.1:1935/app', 'rtmp://example.com:80/app', 'rtmps://example.com:8443/app',
    'rtmp://10.0.0.1/app', 'rtmp://169.254.169.254/app', 'rtmp://100.64.0.1/app', 'rtmp://192.0.2.1/app',
    'rtmp://198.18.0.1/app', 'rtmp://198.51.100.1/app', 'rtmp://203.0.113.1/app', 'rtmp://224.0.0.1/app',
    'rtmp://127.1/app', 'rtmp://0177.0.0.1/app', 'rtmp://0x7f000001/app', 'rtmp://[::1]/app',
    'rtmp://[2001:4860:4860::8888]/app', 'rtmp://localhost/app', 'rtmp://host.internal/app',
    'rtmp://@example.com/app', 'rtmp://example.com./app', 'rtmp://example.com/app?token=synthetic',
    'rtmp://example.com/app#synthetic', 'rtmp://example.com/live/../app', 'rtmp://example.com/live/%2e%2e/app',
    'rtmp://example.com/app%23key', 'rtmp://example.com/app%0akey'];
  for (const stream_key of invalidKeys) assert.throws(() => store.save('youtube', { ...value, stream_key }), /URL과 스트림 키/);
  for (const server_url of invalidServers) assert.throws(() => store.save('youtube', { ...value, server_url }), /URL과 스트림 키/);
  assert.equal(env.indexedDB.opens, 0); assert.equal(env.calls.length, 0);
  for (const server_url of ['rtmps://a.rtmps.youtube.com:443/live2', 'rtmp://live.twitch.tv:1935/app',
    'rtmps://stream.example.com:1935/app', 'rtmp://8.8.8.8/app', 'rtmps://192.0.0.9/app', 'rtmps://192.0.0.10/app']) {
    assert.doesNotThrow(() => env.validateStreamConnection({ ...value, server_url }));
  }
  assert.deepEqual(plain(env.validateStreamConnection({ server_url: 'RTMPS://Stream.Example.Com/app/', stream_key: 'abc%2Fdef?token=synthetic' })),
    { server_url: 'rtmps://stream.example.com:443/app', stream_key: 'abc%2Fdef?token=synthetic' });
});

test('platform readiness shares the saved-connection validator and accepts catalog default URLs', () => {
  const env = environment();
  const filename = path.join(__dirname, '../app/platform-picker.tsx');
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { fileName: filename,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const module = { exports: {} };
  const imports = { 'react': {}, 'react/jsx-runtime': {}, 'lucide-react': {}, '@/components/ui/input': {}, './platform-picker.css': {}, '@/lib/stream-connections': env, '@/lib/watch-links': watchModule.exports };
  vm.runInNewContext(code, { module, exports: module.exports, URL, require: name => { if (!(name in imports)) throw new Error('Unexpected import'); return imports[name]; } }, { filename });
  const ready = module.exports.destinationReady;
  const target = { id: 'youtube', requires_stream_key: true, requires_server_url: false, default_server_url: value.server_url };
  assert.equal(ready(target, { stream_key: value.stream_key }), true);
  for (const destination of [{ ...value, stream_key: 'abc#def' }, { ...value, stream_key: 'rtmp://example.com/app' },
    { ...value, server_url: 'rtmp://127.0.0.1:1935/app' }, { ...value, server_url: 'rtmp://example.com:80/app' }]) assert.equal(ready(target, destination), false);
  assert.equal(ready({ id: 'local', requires_stream_key: false, requires_server_url: false }), true);
});

test('channel URLs are encrypted separately, normalized on use/list, and never persist a broadcast URL', async () => {
  const env = environment(); const store = await env.createStreamConnectionStore(ownerA);
  const channel_url = 'https://youtube.com/@SyntheticChannel/live?si=share';
  const normalized = 'https://www.youtube.com/@SyntheticChannel';
  const saved = await store.save('youtube', { ...value, channel_url, broadcast_url: 'https://youtu.be/abcdefghijk' });
  assert.equal(saved.channel_url, normalized);
  assert.equal((await store.list())[0].channel_url, normalized);
  const used = await store.use('youtube');
  assert.equal(used.channel_url, normalized); assert.equal(used.stream_key, value.stream_key);
  assert.equal(used.broadcast_url, undefined);
  const record = env.indexedDB.records('connections')[0];
  assert.equal(record.channel_url, undefined); assert.equal(record.broadcast_url, undefined);
  assert.ok(record.channel.ciphertext instanceof ArrayBuffer);
  assert.equal(JSON.stringify(record).includes('SyntheticChannel'), false);
  assert.equal(JSON.stringify(record).includes('abcdefghijk'), false);
});

test('legacy v1 rows retain their key and ciphertexts; adding a channel never regenerates the owner key', async () => {
  const indexedDB = memoryIndexedDB(); const first = environment({ indexedDB });
  const oldStore = await first.createStreamConnectionStore(ownerA);
  await oldStore.save('youtube', value);
  const oldRecord = indexedDB.records('connections')[0];
  delete oldRecord.channel;
  const previousServer = structuredClone(oldRecord.server); const previousSecret = structuredClone(oldRecord.secret);
  const previousKey = indexedDB.records('keys')[0].key;
  const resumed = await environment({ indexedDB }).createStreamConnectionStore(ownerA);
  assert.equal((await resumed.list())[0].channel_url, '');
  assert.deepEqual(plain(await resumed.use('youtube')), { target: 'youtube', ...value });
  assert.deepEqual(indexedDB.records('connections')[0].server, previousServer);
  assert.deepEqual(indexedDB.records('connections')[0].secret, previousSecret);
  await resumed.save('youtube', { ...value, channel_url: 'https://www.youtube.com/@Synthetic' });
  assert.equal(indexedDB.records('keys').length, 1);
  const channel = indexedDB.records('connections')[0].channel;
  const decrypted = await webcrypto.subtle.decrypt({ name: 'AES-GCM', iv: channel.iv,
    additionalData: new TextEncoder().encode(JSON.stringify(['replay-stream-connections', 1, resumed.namespace, 'youtube', 'channel'])) }, previousKey, channel.ciphertext);
  assert.equal(new TextDecoder().decode(decrypted), 'https://www.youtube.com/@Synthetic');
});

test('channel ciphertext cannot be substituted across platforms, owners, or encrypted fields', async () => {
  const env = environment(); const store = await env.createStreamConnectionStore(ownerA);
  const other = await env.createStreamConnectionStore(ownerB);
  await store.save('youtube', { ...value, channel_url: 'https://www.youtube.com/@Synthetic' });
  await store.save('twitch', { ...value, channel_url: 'https://www.twitch.tv/synthetic' });
  await other.save('youtube', { ...value, channel_url: 'https://www.youtube.com/@Other' });
  const records = env.indexedDB.records('connections');
  const record = records.find(item => item.owner === store.namespace && item.target === 'youtube');
  const original = record.channel;
  const substitutions = [record.server, records.find(item => item.target === 'twitch').channel,
    records.find(item => item.owner === other.namespace).channel];
  for (const replacement of substitutions) {
    record.channel = replacement;
    await assert.rejects(store.use('youtube'));
    await assert.rejects(store.list());
  }
  record.channel = original;
  assert.equal((await store.use('youtube')).channel_url, 'https://www.youtube.com/@Synthetic');
});

test('account storage sends channel only, accepts legacy empty metadata, and rejects unsafe returned channel URLs', async () => {
  const env = environment({ hostname: 'studio.example.com' }); const store = await env.createStreamConnectionStore(ownerA);
  let channel_url = 'https://twitch.tv/synthetic?utm_source=share';
  env.useNetwork((url, init = {}) => {
    const result = { target: 'twitch', ...value, channel_url, updated_at: 123, has_stream_key: true, broadcast_url: 'must-not-forward' };
    return url.endsWith('/use') || init.method === 'PUT' ? result : [result];
  });
  assert.equal((await store.save('twitch', { ...value, channel_url, broadcast_url: 'must-not-store' })).channel_url, 'https://www.twitch.tv/synthetic');
  assert.deepEqual(JSON.parse(env.calls[0].init.body), { ...value, channel_url: 'https://www.twitch.tv/synthetic' });
  assert.equal((await store.use('twitch')).channel_url, 'https://www.twitch.tv/synthetic');
  assert.equal((await store.list())[0].broadcast_url, undefined);
  channel_url = undefined;
  assert.equal((await store.use('twitch')).channel_url, '');
  assert.equal((await store.list())[0].channel_url, '');
  channel_url = 'https://www.twitch.tv/synthetic?token=private-synthetic';
  await assert.rejects(store.use('twitch'), error => !error.message.includes('private-synthetic'));
  await assert.rejects(store.list(), error => !error.message.includes('private-synthetic'));
});

test('invalid or broadcast-only channel URLs fail before any storage write', async () => {
  const env = environment(); const store = await env.createStreamConnectionStore(ownerA);
  for (const channel_url of ['https://youtu.be/abcdefghijk', 'https://www.twitch.tv/synthetic', 'javascript:synthetic', 123]) {
    assert.throws(() => store.save('youtube', { ...value, channel_url }));
  }
  assert.equal(env.indexedDB.opens, 0); assert.equal(env.calls.length, 0);
});
