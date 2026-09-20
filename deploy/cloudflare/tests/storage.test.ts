import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createHash, createHmac } from 'node:crypto';
import { build } from 'esbuild';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

const token = 'synthetic-control-'.repeat(4), signing = 'synthetic-object-'.repeat(4);
const tenant = 'cloudflare-test';
const base = 'https://storage.example';
const key = `replay/${createHash('sha256').update(tenant).digest('hex')}/media/test.mp4`;
const bytes = Buffer.from('test video content');
const sha256 = createHash('sha256').update(bytes).digest('hex');
let runtime: Miniflare;
before(async () => {
  const bundled = await build({ entryPoints: ['tests/storage-worker.ts'], bundle: true, write: false, format: 'esm', target: 'es2022' });
  runtime = new Miniflare(convertV4MiniflareOptions({ name: 'storage', modules: true, script: bundled.outputFiles[0].text,
    compatibilityDate: '2026-08-18', r2Buckets: ['MEDIA'], bindings: {
      REPLAY_CONTROL_TOKEN: token, REPLAY_OBJECT_KEY: signing, REPLAY_PUBLIC_URL: base,
      REPLAY_ORIGINS: 'https://replay-live.pages.dev',
    } }));
});
after(async () => { await runtime?.dispose(); });
async function control(operation: string, extra: Record<string, unknown> = {}, authorization = token) {
  return runtime.dispatchFetch(base + '/api/blob-control', { method: 'POST', headers: {
    Authorization: `Bearer ${authorization}`, 'Content-Type': 'application/json',
  }, body: JSON.stringify({ operation, tenant_id: tenant, object_key: key, ...extra }) });
}
async function uploadGrant(extra: Record<string, unknown> = {}) {
  const result = await control('upload', { size: bytes.length, sha256, ...extra });
  assert.equal(result.status, 200);
  return await result.json() as { url: string; headers: Record<string, string> };
}
test('auth and tenant checks precede storage; bounded grants carry no control secret', async () => {
  assert.equal((await control('health', {}, 'invalid')).status, 401);
  assert.equal((await control('upload', { tenant_id: 'another', size: bytes.length, sha256 })).status, 400);
  assert.equal((await control('upload', { size: 50 * 1024 ** 2 + 1, sha256 })).status, 400);
  const signed = await uploadGrant();
  assert.equal(signed.url.includes(token), false);
  assert.equal(new URL(signed.url).origin, base);
});
test('real R2 binding validates upload checksum, refuses overwrite, and serves range playback', async () => {
  const signed = await uploadGrant();
  const put = await runtime.dispatchFetch(signed.url, { method: 'PUT', headers: signed.headers, body: bytes });
  assert.equal(put.status, 200, await put.text());
  const verified = await control('verify', { size: bytes.length, sha256 });
  assert.equal(verified.status, 200);
  const meta = await verified.json() as { sha256: string; bytes: number; etag: string };
  assert.equal(meta.sha256, sha256);
  assert.equal(meta.bytes, bytes.length);
  const duplicate = await runtime.dispatchFetch(signed.url, { method: 'PUT', headers: signed.headers, body: bytes });
  assert.equal(duplicate.status, 409);
  const get = await control('download');
  const download = await get.json() as { url: string };
  const full = await runtime.dispatchFetch(download.url, { headers: { 'If-Match': `"${meta.etag}"` } });
  assert.equal(full.status, 200);
  assert.equal(Buffer.from(await full.arrayBuffer()).equals(bytes), true);
  const ranged = await runtime.dispatchFetch(download.url, { headers: { Range: 'bytes=2-5', Origin: 'https://replay-live.pages.dev' } });
  assert.equal(ranged.status, 206);
  assert.equal(ranged.headers.get('content-range'), `bytes 2-5/${bytes.length}`);
  assert.equal(await ranged.text(), bytes.subarray(2, 6).toString());
  assert.equal((await runtime.dispatchFetch(download.url, { headers: { 'If-Match': '"wrong"' } })).status, 412);
});
test('tampered, expired, other-path, wrong-method and cross-origin capabilities fail closed', async () => {
  const signed = await uploadGrant();
  assert.equal((await runtime.dispatchFetch(signed.url)).status, 403);
  assert.equal((await runtime.dispatchFetch(signed.url.replace('/test.mp4', '/other.mp4'), { method: 'PUT' })).status, 403);
  assert.equal((await runtime.dispatchFetch(signed.url + 'x', { method: 'PUT' })).status, 403);
  assert.equal((await runtime.dispatchFetch(signed.url, { headers: { Origin: 'https://evil.example' } })).status, 403);
  const payload = Buffer.from(JSON.stringify({ v: 1, operation: 'GET', key, until: Date.now() - 1 })).toString('base64url');
  const signature = createHmac('sha256', signing).update(payload).digest('base64url');
  assert.equal((await runtime.dispatchFetch(`${base}/objects/${key}?grant=${payload}.${signature}`)).status, 403);
});
test('wrong bytes never become an admitted object; deletion is scoped and idempotent', async () => {
  const other = key.replace('test.mp4', 'wrong.mp4');
  const signed = await uploadGrant({ object_key: other });
  const result = await runtime.dispatchFetch(signed.url, { method: 'PUT', headers: signed.headers, body: Buffer.alloc(bytes.length) });
  assert.notEqual(result.status, 200);
  assert.equal(await (await runtime.getR2Bucket('MEDIA')).head(other), null);
  assert.equal((await control('verify', { size: bytes.length, sha256: '0'.repeat(64) })).status, 409);
  assert.equal((await control('delete')).status, 200);
  assert.equal((await control('head')).status, 404);
});
