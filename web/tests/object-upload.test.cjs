const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('../node_modules/typescript');
const code = ts.transpileModule(fs.readFileSync(require.resolve('../lib/object-upload.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
function fixture({ failPart = 0, stalePart = 0 } = {}) {
  const requests = [], control = [], active = [], progress = [];
  let stale = false;
  class XHR {
    upload = {}; headers = {};
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(key, value) { this.headers[key] = value; }
    send(data) {
      requests.push({ method: this.method, url: this.url, headers: this.headers, size: data.size });
      const number = Number(this.headers['X-Replay-Part'] || 1);
      queueMicrotask(() => {
        this.upload.onprogress?.({ lengthComputable: true, loaded: data.size });
        this.status = number === failPart ? 503 : 200;
        this.responseText = JSON.stringify({ partNumber: number, etag: 'opaque-'.repeat(30) });
        if (number === stalePart) stale = true;
        this.onload(); this.onloadend();
      });
    }
    abort() { this.onabort(); this.onloadend(); }
  }
  const exports = {};
  vm.runInNewContext(code, { exports, XMLHttpRequest: XHR, AbortSignal, DOMException,
    fetch: async (url, options) => { control.push({ url, ...options }); return { ok: true }; } });
  const file = { size: 65 * 1024 ** 2 + 17, slice: (start, end) => ({ size: Math.min(end, file.size) - start }) };
  const signed = { url: 'https://storage.example/objects/synthetic?grant=synthetic', method: 'PUT',
    headers: { 'Content-Type': 'video/mp4', 'Content-Length': String(file.size) }, multipart: { part_size: 32 * 1024 ** 2 } };
  return { requests, control, active, progress, run: () => exports.uploadObject(file, signed, {
    check: () => { if (stale) throw Error('session changed'); }, active: value => active.push(value), progress: value => progress.push(value),
  }) };
}
test('large browser uploads send bounded parts, preserve opaque ETags and complete without credentials', async () => {
  const f = fixture(); await f.run();
  assert.deepEqual(f.requests.map(r => r.size), [32 * 1024 ** 2, 32 * 1024 ** 2, 1024 ** 2 + 17]);
  assert.ok(f.requests.every(r => !Object.keys(r.headers).some(k => k.toLowerCase() === 'content-length')));
  assert.equal(f.control.length, 1); assert.equal(f.control[0].method, 'POST');
  assert.equal(f.control[0].credentials, 'omit'); assert.equal(f.control[0].redirect, 'error');
  assert.equal(JSON.parse(f.control[0].body).parts.length, 3);
  assert.equal(f.progress.at(-1), 100); assert.equal(f.active.at(-1), null);
});
for (const options of [{ failPart: 2 }, { stalePart: 1 }]) test(`failed or stale upload aborts only its capability: ${JSON.stringify(options)}`, async () => {
  const f = fixture(options); await assert.rejects(f.run());
  assert.equal(f.control.length, 1); assert.equal(f.control[0].method, 'DELETE');
  assert.equal(f.control[0].credentials, 'omit'); assert.equal(f.active.at(-1), null);
});
