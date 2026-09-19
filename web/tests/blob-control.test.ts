import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createHash } from 'node:crypto';
import * as blob from '@vercel/blob';
// Node's test runner executes TypeScript directly without a build artifact.
// @ts-expect-error TypeScript preserves this extension for Node's type stripping.
import { createBlobControl } from '../lib/blob-control.ts';

const tenant = 'synthetic-tenant';
const key = `replay/${createHash('sha256').update(tenant).digest('hex')}/media/example.mp4`;
const data = Buffer.from('synthetic-video-bytes');
const sha256 = createHash('sha256').update(data).digest('hex');
const host = 'synthetic.private.blob.vercel-storage.com';
const token = 'synthetic-control-token-'.repeat(3);
const canary = 'replay/_health/private-store-v1.txt';
const canaryBody = 'replay-live-private-store-v1';
type Dep = NonNullable<Parameters<typeof createBlobControl>[1]>;

function fixture(options: { bytes?: Buffer; etag?: string; unsignedStatus?: number; missingCanary?: boolean;
  getStatus?: number; headSize?: number; fail?: boolean; missing?: boolean; noLength?: boolean; alternateHost?: string } = {}) {
  const calls: { kind: string; value: unknown }[] = [];
  let canaryExists = !options.missingCanary;
  const sdk = {
    async head(url: string) {
      calls.push({ kind: 'head', value: url });
      if (options.fail) throw new Error('sensitive signed URL and token must not escape');
      const pathname = new URL(url).pathname.slice(1);
      if ((pathname === canary && !canaryExists) || options.missing) throw new blob.BlobNotFoundError();
      return { url: `https://${options.alternateHost || host}/${pathname}`, pathname,
        size: pathname === canary ? Buffer.byteLength(canaryBody) : options.headSize ?? data.length,
        contentType: pathname === canary ? 'text/plain' : 'video/mp4', etag: 'synthetic-version' };
    },
    async issueSignedToken(value: unknown) { calls.push({ kind: 'issue', value }); return { delegationToken: 'synthetic', clientSigningToken: 'synthetic' }; },
    async presignUrl(_token: unknown, value: { operation: string; pathname: string }) {
      calls.push({ kind: 'presign', value });
      return { presignedUrl: value.operation === 'put'
        ? `https://vercel.com/api/blob/?pathname=${encodeURIComponent(value.pathname)}&vercel-blob-signature=synthetic`
        : `https://${host}/${value.pathname}?vercel-blob-signature=synthetic&cache=0` };
    },
    async put(pathname: string, body: string, value: unknown) {
      calls.push({ kind: 'put', value: { pathname, body, options: value } }); canaryExists = true;
    },
    async del(value: unknown) { calls.push({ kind: 'delete', value }); },
  } as unknown as NonNullable<Dep['sdk']>;
  const fakeFetch: typeof fetch = async (url, init) => {
    calls.push({ kind: 'fetch', value: { url, init } });
    if ((typeof url === 'string' ? url : url instanceof URL ? url.href : url.url).includes(canary)) return new Response(null, { status: options.unsignedStatus ?? 403 });
    return new Response(options.bytes ?? data, { status: options.getStatus ?? 200, headers: {
      ...(options.noLength ? {} : { 'Content-Length': String(options.headSize ?? data.length) }),
      'Content-Type': 'video/mp4', ETag: options.etag ?? '"synthetic-version"',
    } });
  };
  const handle = createBlobControl({ REPLAY_CONTROL_TOKEN: token, BLOB_STORE_ID: 'store_synthetic' }, { sdk, fetch: fakeFetch, now: () => 100000 });
  const request = (body: unknown, authorization = `Bearer ${token}`) => handle(new Request('https://app.example/api/blob-control', {
    method: 'POST', headers: { Authorization: authorization, 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  }));
  const input = (operation: string, extra: Record<string, unknown> = {}) => ({ operation, tenant_id: tenant, object_key: key, ...extra });
  return { calls, request, input };
}

void test('control authentication rejects before any provider call and never reflects secrets', async () => {
  const f = fixture();
  const response = await f.request(f.input('head'), 'Bearer wrong-secret');
  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { code: 'UNAUTHORIZED' });
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(f.calls, []);
});

void test('tenant, traversal, URL keys, invalid sizes, hashes and expiration are rejected before provider calls', async () => {
  for (const extra of [{ tenant_id: 'another-tenant' }, { object_key: key + '/../other.mp4' }, { object_key: 'https://evil.example/file' },
    { size: 50 * 1024 ** 2 + 1 }, { size: true }, { size: 0 }, { sha256: 'fake' }, { expires: 901 }, { content_type: 'text/html' }]) {
    const f = fixture();
    const response = await f.request(f.input('upload', { size: data.length, sha256, ...extra }));
    assert.equal(response.status, 400);
    assert.deepEqual(f.calls, []);
  }
});

void test('upload grant is private, single-path, create-only, byte/MIME limited and at most 15 minutes', async () => {
  const f = fixture();
  const response = await f.request(f.input('upload', { size: data.length, sha256, expires: 900 }));
  assert.equal(response.status, 200);
  const value = await response.json() as { object_key: string; method: string; headers: Record<string, string> };
  assert.equal(value.object_key, key);
  assert.equal(value.method, 'PUT');
  assert.deepEqual(value.headers, { 'Content-Type': 'video/mp4', 'Content-Length': String(data.length) });
  const issue = f.calls.find(x => x.kind === 'issue')!.value as Record<string, unknown>;
  assert.deepEqual(issue.operations, ['put']);
  assert.equal(issue.pathname, key);
  assert.equal(issue.maximumSizeInBytes, data.length);
  assert.equal(issue.validUntil, 1000000);
  const signed = f.calls.find(x => x.kind === 'presign')!.value as Record<string, unknown>;
  assert.equal(signed.allowOverwrite, false);
  assert.equal(signed.addRandomSuffix, false);
  assert.equal(signed.access, 'private');
});

void test('head and verify calculate actual SHA-256 with immutable ETag; no trusted checksum cache', async () => {
  const f = fixture();
  for (const operation of ['head', 'verify']) {
    const response = await f.request(f.input(operation, { size: data.length, sha256 }));
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { bytes: data.length, size: data.length, sha256,
      etag: 'synthetic-version', content_type: 'video/mp4', version_id: null, object_key: key });
  }
  const reads = f.calls.filter(x => x.kind === 'fetch');
  assert.equal(reads.length, 2);
  const init = (reads[0].value as { init: RequestInit }).init;
  assert.equal((init.headers as Record<string, string>)['If-Match'], '"synthetic-version"');
  assert.equal(init.redirect, 'error');
});

void test('short, long, wrong-hash, changed-version and missing-length media cannot verify', async () => {
  for (const options of [{ bytes: Buffer.from('short') }, { bytes: Buffer.concat([data, data]) },
    { bytes: Buffer.alloc(data.length) }, { etag: '"changed"' }, { noLength: true }, { getStatus: 412 }]) {
    const f = fixture(options);
    const response = await f.request(f.input('verify', { size: data.length, sha256 }));
    assert.equal(response.status, 409);
    assert.deepEqual(await response.json(), { code: 'INTEGRITY_MISMATCH' });
  }
});

void test('download grants preserve existing GET descriptor without store credentials or file proxying', async () => {
  const f = fixture();
  const response = await f.request(f.input('download', { expires: 300, filename: 'video.mp4' }));
  assert.equal(response.status, 200);
  const result = await response.json() as { url: string; method: string; headers: Record<string, string>; expires_in: number };
  assert.equal(result.method, 'GET');
  assert.deepEqual(result.headers, {});
  assert.equal(result.expires_in, 300);
  assert.equal(new URL(result.url).hostname, host);
  assert.equal(f.calls.filter(x => x.kind === 'fetch').length, 0);
  assert.equal(JSON.stringify(result).includes(token), false);
});

void test('health initializes private canary once and refuses publicly readable or missing reads', async () => {
  const f = fixture({ missingCanary: true });
  for (let count = 0; count < 2; count++) {
    const response = await f.request({ operation: 'health' });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ready: true, access: 'private' });
  }
  const puts = f.calls.filter(x => x.kind === 'put');
  assert.equal(puts.length, 1);
  const value = puts[0].value as { body: string; options: Record<string, unknown> };
  assert.equal(value.body, canaryBody);
  assert.equal(value.options.access, 'private');
  assert.equal(value.options.allowOverwrite, false);
  for (const status of [200, 404, 500]) {
    const blocked = fixture({ unsignedStatus: status });
    assert.equal((await blocked.request({ operation: 'health' })).status, 502);
    assert.equal((await blocked.request(blocked.input('upload', { size: data.length, sha256 }))).status, 502);
    assert.equal(blocked.calls.some(x => x.kind === 'issue'), false);
  }
});

void test('metadata outside private store and object size ceiling is rejected', async () => {
  for (const options of [{ alternateHost: 'synthetic.public.blob.vercel-storage.com' }, { headSize: 50 * 1024 ** 2 + 1 }]) {
    const f = fixture(options);
    assert.equal((await f.request(f.input('head'))).status, 409);
    assert.equal(f.calls.some(x => x.kind === 'fetch'), false);
  }
});

void test('provider errors and missing objects map to fixed codes without upstream text', async () => {
  for (const [options, status, code] of [[{ fail: true }, 502, 'STORAGE_UNAVAILABLE'], [{ missing: true }, 404, 'OBJECT_NOT_FOUND']] as const) {
    const f = fixture(options);
    const response = await f.request(f.input('head'));
    assert.equal(response.status, status);
    assert.deepEqual(await response.json(), { code });
  }
});

void test('delete validates tenant scope and returns only completion metadata', async () => {
  const f = fixture();
  assert.deepEqual(await (await f.request(f.input('delete'))).json(), { deleted: true });
  assert.deepEqual(f.calls, [{ kind: 'delete', value: `https://${host}/${key}` }]);
});

void test('50 MiB input and 128 MiB output grant boundaries preserve exact signed limits', async () => {
  for (const [kind, limit] of [['media', 50 * 1024 ** 2], ['outputs', 128 * 1024 ** 2]] as const) {
    const f = fixture();
    const object_key = key.replace('/media/', `/${kind}/`);
    assert.equal((await f.request(f.input('upload', { object_key, size: limit, sha256 }))).status, 200);
    const issue = f.calls.find(x => x.kind === 'issue')!.value as Record<string, unknown>;
    assert.equal(issue.maximumSizeInBytes, limit);
    assert.equal((await f.request(f.input('upload', { object_key, size: limit + 1, sha256 }))).status, 400);
  }
});

void test('official SDK generates the exact private GET and control-plane PUT URL forms without network', async () => {
  const validUntil = Date.now() + 60_000;
  const delegationToken = Buffer.from(JSON.stringify({ storeId: 'store_synthetic', ownerId: 'synthetic',
    pathname: key, operations: ['get', 'put'], validUntil })).toString('base64url') + '.synthetic';
  const issued = { delegationToken, clientSigningToken: 'synthetic-signing-material' };
  const upload = new URL((await blob.presignUrl(issued, { operation: 'put', pathname: key, access: 'private',
    validUntil, maximumSizeInBytes: data.length, allowedContentTypes: ['video/mp4'], allowOverwrite: false, addRandomSuffix: false })).presignedUrl);
  assert.equal(upload.origin, 'https://vercel.com');
  assert.equal(upload.pathname, '/api/blob/');
  assert.equal(upload.searchParams.get('pathname'), key);
  for (const parameter of upload.searchParams.keys()) assert.ok(parameter === 'pathname' || parameter.startsWith('vercel-blob-'));
  const download = new URL((await blob.presignUrl(issued, { operation: 'get', pathname: key, access: 'private', validUntil, useCache: false })).presignedUrl);
  assert.equal(download.hostname, host);
  assert.equal(download.pathname, `/${key}`);
  assert.equal(download.searchParams.get('cache'), '0');
});
