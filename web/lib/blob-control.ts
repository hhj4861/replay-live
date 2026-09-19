import { createHash, timingSafeEqual } from 'node:crypto';
import * as blob from '@vercel/blob';

const MiB = 1024 * 1024;
const canaryPath = 'replay/_health/private-store-v1.txt';
const canaryBody = 'replay-live-private-store-v1';
type Environment = Record<string, string | undefined>;
type Dependencies = { sdk: Pick<typeof blob, 'issueSignedToken' | 'presignUrl' | 'head' | 'put' | 'del'>; fetch: typeof fetch; now: () => number };
const defaults: Dependencies = { sdk: blob, fetch: (...args) => fetch(...args), now: Date.now };

class ControlError extends Error {
  constructor(readonly code: string, readonly status: number) { super(code); }
}
const invalid = () => new ControlError('INVALID_REQUEST', 400);
const unavailable = () => new ControlError('STORAGE_UNAVAILABLE', 502);
const integrity = () => new ControlError('INTEGRITY_MISMATCH', 409);

function integer(value: unknown, maximum: number): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1 || value > maximum) throw invalid();
  return value;
}
function configuredLimit(value: string | undefined, maximum: number) {
  if (value === undefined) return maximum;
  if (!/^[1-9][0-9]*$/.test(value)) throw unavailable();
  const limit = Number(value);
  if (!Number.isSafeInteger(limit) || limit > maximum) throw unavailable();
  return limit;
}
function tenantKey(tenant: unknown, key: unknown): { key: string; kind: string } {
  if (typeof tenant !== 'string' || !tenant.trim() || tenant.length > 200) throw invalid();
  for (const character of tenant) if (character.charCodeAt(0) < 32) throw invalid();
  const prefix = `replay/${createHash('sha256').update(tenant).digest('hex')}/`;
  if (typeof key !== 'string' || !key.startsWith(prefix)) throw invalid();
  const match = /^(media|outputs)\/[A-Za-z0-9_-]{1,128}\.(mp4|flv)$/.exec(key.slice(prefix.length));
  if (!match) throw invalid();
  return { key, kind: match[1] };
}
function checksum(value: unknown): string {
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) throw invalid();
  return value;
}
function contentType(value: unknown): string {
  if (typeof value !== 'string' || !['video/mp4', 'video/x-flv', 'application/octet-stream'].includes(value)) throw invalid();
  return value;
}
function same(left: string, right: string) {
  const digest = (value: string) => createHash('sha256').update(value).digest();
  return timingSafeEqual(digest(left), digest(right));
}
function etag(value: unknown): string {
  if (typeof value !== 'string') throw integrity();
  const plain = value.replace(/^"|"$/g, '');
  if (!plain || plain.length > 200 || !/^[\x21\x23-\x7e]+$/.test(plain) || plain.startsWith('W/')) throw integrity();
  return plain;
}
async function boundedRead(reader: ReadableStreamDefaultReader<Uint8Array>, signal: AbortSignal) {
  signal.throwIfAborted();
  let onAbort: (() => void) | undefined;
  try {
    return await Promise.race([reader.read(), new Promise<never>((_, reject) => {
      onAbort = () => { void reader.cancel().catch(() => {}); reject(unavailable()); };
      signal.addEventListener('abort', onAbort, { once: true });
    })]);
  } finally { if (onAbort) signal.removeEventListener('abort', onAbort); }
}
async function requestBody(request: Request, signal: AbortSignal): Promise<Record<string, unknown>> {
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json') || !request.body) throw invalid();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const next = await boundedRead(reader, signal);
      if (next.done) break;
      size += next.value.byteLength;
      if (size > 16384) throw invalid();
      chunks.push(next.value);
    }
    const value: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw invalid();
    return value as Record<string, unknown>;
  } catch (error) { if (error instanceof ControlError) throw error; throw invalid(); }
  finally { await reader.cancel().catch(() => {}); }
}

export function createBlobControl(env: Environment = process.env, dependencies: Partial<Dependencies> = {}) {
  const dep = { ...defaults, ...dependencies };
  return async (request: Request): Promise<Response> => {
    const response = (body: unknown, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer' } });
    if (!env.REPLAY_CONTROL_TOKEN || env.REPLAY_CONTROL_TOKEN.length < 32
        || !same(request.headers.get('authorization') || '', `Bearer ${env.REPLAY_CONTROL_TOKEN}`)) return response({ code: 'UNAUTHORIZED' }, 401);
    if (request.method !== 'POST') return response({ code: 'INVALID_REQUEST' }, 400);
    const signal = AbortSignal.any([request.signal, AbortSignal.timeout(80_000)]);
    try {
      const input = await requestBody(request, AbortSignal.any([signal, AbortSignal.timeout(5000)]));
      const storeId = env.BLOB_STORE_ID;
      if (!storeId || !/^store_[A-Za-z0-9]+$/.test(storeId)) throw unavailable();
      const host = `${storeId.slice(6).toLowerCase()}.private.blob.vercel-storage.com`;
      const objectUrl = (key: string) => `https://${host}/${key}`;
      const options = { storeId, abortSignal: signal };
      const checkedHead = async (key: string, maximum: number) => {
        const item = await dep.sdk.head(objectUrl(key), options);
        if (item.url !== objectUrl(key) || item.pathname !== key || !Number.isSafeInteger(item.size) || item.size < 1 || item.size > maximum) throw integrity();
        return { ...item, etag: etag(item.etag) };
      };
      const signed = async (key: string, operation: 'get' | 'put', expires: number, shape: { size: number; type: string } | null = null) => {
        const validUntil = dep.now() + expires * 1000;
        const token = await dep.sdk.issueSignedToken({ ...options, pathname: key, operations: [operation], validUntil,
          ...(shape ? { maximumSizeInBytes: shape.size, allowedContentTypes: [shape.type] } : {}) });
        const result = await dep.sdk.presignUrl(token, operation === 'put'
          ? { operation, pathname: key, access: 'private', validUntil, maximumSizeInBytes: shape!.size,
            allowedContentTypes: [shape!.type], addRandomSuffix: false, allowOverwrite: false }
          : { operation, pathname: key, access: 'private', validUntil, useCache: false });
        const url = new URL(result.presignedUrl);
        if (url.username || url.password || url.hash || url.protocol !== 'https:'
            || (operation === 'get' ? url.origin !== `https://${host}` || url.pathname !== `/${key}`
              : url.origin !== 'https://vercel.com' || url.pathname !== '/api/blob/' || url.searchParams.get('pathname') !== key)) throw unavailable();
        return result.presignedUrl;
      };

      const requirePrivateStore = async () => {
        let item;
        try { item = await checkedHead(canaryPath, 128); }
        catch (error) {
          if (!(error instanceof blob.BlobNotFoundError)) throw error;
          try { await dep.sdk.put(canaryPath, canaryBody, { ...options, access: 'private', contentType: 'text/plain', addRandomSuffix: false, allowOverwrite: false }); }
          catch (putError) { if (!(putError instanceof blob.BlobPreconditionFailedError)) throw putError; }
          item = await checkedHead(canaryPath, 128);
        }
        if (item.size !== Buffer.byteLength(canaryBody)) throw integrity();
        const denied = await dep.fetch(objectUrl(canaryPath), { method: 'GET', redirect: 'error', signal, cache: 'no-store' });
        await denied.body?.cancel();
        if (![401, 403].includes(denied.status)) throw unavailable();
      };
      if (input.operation === 'health') {
        await requirePrivateStore();
        return response({ ready: true, access: 'private' });
      }
      const { key, kind } = tenantKey(input.tenant_id, input.object_key);
      const maximum = kind === 'media' ? configuredLimit(env.REPLAY_MAX_UPLOAD_BYTES, 50 * MiB) : configuredLimit(env.REPLAY_MAX_OUTPUT_BYTES, 128 * MiB);
      if (input.operation === 'upload') {
        const size = integer(input.size, maximum), type = contentType(input.content_type ?? 'video/mp4');
        checksum(input.sha256);
        const expires = integer(input.expires ?? 900, 900);
        await requirePrivateStore();
        return response({ url: await signed(key, 'put', expires, { size, type }), method: 'PUT',
          headers: { 'Content-Type': type, 'Content-Length': String(size) }, expires_in: expires, object_key: key });
      }
      if (input.operation === 'delete') {
        await dep.sdk.del(objectUrl(key), options);
        return response({ deleted: true });
      }
      if (!['download', 'head', 'verify'].includes(String(input.operation))) throw invalid();
      const expectedSize = input.operation === 'verify' ? integer(input.size, maximum) : null;
      const expectedHash = input.operation === 'verify' ? checksum(input.sha256) : null;
      const expectedType = input.operation === 'verify' ? contentType(input.content_type ?? 'video/mp4') : null;
      const item = await checkedHead(key, maximum);
      contentType(item.contentType);
      if (input.operation === 'download') {
        const expires = integer(input.expires ?? 300, 900);
        // Blob's signed GET has no response-content-disposition override; keep the stored filename.
        if (input.filename !== undefined && (typeof input.filename !== 'string' || input.filename.length > 200)) throw invalid();
        return response({ url: await signed(key, 'get', expires), method: 'GET', headers: {}, expires_in: expires });
      }
      if (expectedSize !== null && (item.size !== expectedSize || item.contentType !== expectedType)) throw integrity();
      const result = await dep.fetch(await signed(key, 'get', 300), { method: 'GET', headers: { 'If-Match': `"${item.etag}"`, 'Accept-Encoding': 'identity' }, redirect: 'error', signal, cache: 'no-store' });
      if (result.status === 404) { await result.body?.cancel(); throw new ControlError('OBJECT_NOT_FOUND', 404); }
      if (result.status !== 200 || !result.body || result.headers.get('content-length') !== String(item.size)
          || result.headers.get('content-type') !== item.contentType || etag(result.headers.get('etag')) !== item.etag
          || (result.headers.get('content-encoding') && result.headers.get('content-encoding') !== 'identity')) {
        await result.body?.cancel(); throw integrity();
      }
      const hash = createHash('sha256');
      const reader = result.body.getReader();
      let actual = 0;
      try {
        while (true) {
          const next = await boundedRead(reader, signal);
          if (next.done) break;
          actual += next.value.byteLength;
          if (actual > item.size) throw integrity();
          hash.update(next.value);
        }
      } finally { await reader.cancel().catch(() => {}); }
      const sha256 = hash.digest('hex');
      if (actual !== item.size || (expectedHash !== null && !same(sha256, expectedHash))) throw integrity();
      return response({ bytes: actual, size: actual, sha256, etag: item.etag, content_type: item.contentType, version_id: null, object_key: key });
    } catch (error) {
      if (error instanceof ControlError) return response({ code: error.code }, error.status);
      if (error instanceof blob.BlobNotFoundError) return response({ code: 'OBJECT_NOT_FOUND' }, 404);
      if (error instanceof blob.BlobPreconditionFailedError) return response({ code: 'INTEGRITY_MISMATCH' }, 409);
      return response({ code: 'STORAGE_UNAVAILABLE' }, 502);
    }
  };
}
