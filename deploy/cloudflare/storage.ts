// The private R2 binding never leaves this Worker. Each transfer gets one
// short-lived, immutable object capability, including its verified checksum.
export type StorageEnv = {
  MEDIA: R2Bucket;
  REPLAY_CONTROL_TOKEN: string;
  REPLAY_OBJECT_KEY: string;
  REPLAY_PUBLIC_URL: string;
  REPLAY_ORIGINS: string;
};
type Grant = { v: 1; operation: 'GET' | 'PUT'; key: string; until: number;
  size?: number; sha256?: string; type?: string; filename?: string };
const encoder = new TextEncoder();
const maximum = 64 * 1024 ** 2;
const keyPattern = /^replay\/[a-f0-9]{64}\/(media|outputs)\/[A-Za-z0-9_-]{1,128}\.(mp4|flv)$/;
const hashPattern = /^[a-f0-9]{64}$/;
const mimeTypes = new Set(['video/mp4', 'video/x-flv', 'application/octet-stream']);
class Failure extends Error {
  constructor(readonly status: number, readonly code: string) { super(code); }
}
const reject = (status = 400, code = 'INVALID_REQUEST'): never => { throw new Failure(status, code); };
const hex = (value: ArrayBuffer) => [...new Uint8Array(value)].map(v => v.toString(16).padStart(2, '0')).join('');
const b64 = (value: Uint8Array) => btoa(String.fromCharCode(...value)).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
function decode(value: string) {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) return reject(403, 'INVALID_GRANT');
  return Uint8Array.from(atob(value.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0));
}
async function hmac(secret: string) {
  if (secret.length < 32) return reject(503, 'STORAGE_UNAVAILABLE');
  return crypto.subtle.importKey('raw', encoder.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
}
export async function authorized(request: Request, secret: string) {
  if (!secret || secret.length < 32) return false;
  const key = await hmac(secret);
  const expected = encoder.encode(`Bearer ${secret}`);
  const signature = await crypto.subtle.sign('HMAC', key, expected);
  return crypto.subtle.verify('HMAC', key, signature, encoder.encode(request.headers.get('authorization') || ''));
}
function integer(value: unknown, limit: number): number {
  if (!Number.isSafeInteger(value) || (value as number) < 1 || (value as number) > limit) return reject();
  return value as number;
}
function shape(size: unknown, sha256: unknown, type: unknown, key: string) {
  const bytes = integer(size, key.includes('/media/') ? 50 * 1024 ** 2 : maximum);
  if (typeof sha256 !== 'string' || !hashPattern.test(sha256) || typeof type !== 'string' || !mimeTypes.has(type)) return reject();
  return { size: bytes, sha256, type };
}
async function scopedKey(tenant: unknown, key: unknown) {
  if (typeof tenant !== 'string' || !tenant.trim() || tenant.length > 200 || /[\x00-\x1f]/.test(tenant)
      || typeof key !== 'string' || !keyPattern.test(key)) return reject();
  const digest = hex(await crypto.subtle.digest('SHA-256', encoder.encode(tenant)));
  if (!key.startsWith(`replay/${digest}/`)) return reject();
  return key;
}
function json(value: unknown, status = 200, additional: Record<string, string> = {}) {
  return Response.json(value, { status, headers: { 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', ...additional } });
}
async function body(request: Request): Promise<Record<string, unknown>> {
  if (request.headers.get('content-type')?.split(';')[0] !== 'application/json' || !request.body) return reject();
  const reader = request.body.getReader();
  const timer = setTimeout(() => { void reader.cancel(); }, 5000);
  let content = '', length = 0;
  const decoder = new TextDecoder();
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      length += next.value.byteLength;
      if (length > 16384) return reject();
      content += decoder.decode(next.value, { stream: true });
    }
    content += decoder.decode();
    const value = JSON.parse(content);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return reject();
    return value;
  } finally { clearTimeout(timer); await reader.cancel().catch(() => {}); }
}
async function signed(env: StorageEnv, value: Grant) {
  const payload = b64(encoder.encode(JSON.stringify(value)));
  const signature = b64(new Uint8Array(await crypto.subtle.sign('HMAC', await hmac(env.REPLAY_OBJECT_KEY), encoder.encode(payload))));
  return `${env.REPLAY_PUBLIC_URL}/objects/${value.key}?grant=${payload}.${signature}`;
}
async function grant(request: Request, env: StorageEnv): Promise<Grant> {
  const url = new URL(request.url);
  const tokens = url.searchParams.getAll('grant');
  if (tokens.length !== 1 || [...url.searchParams.keys()].some(k => k !== 'grant') || tokens[0].length > 2048) return reject(403, 'INVALID_GRANT');
  const [payload, signature, extra] = tokens[0].split('.');
  if (!payload || !signature || extra !== undefined
      || !await crypto.subtle.verify('HMAC', await hmac(env.REPLAY_OBJECT_KEY), decode(signature), encoder.encode(payload))) return reject(403, 'INVALID_GRANT');
  const value = JSON.parse(new TextDecoder().decode(decode(payload))) as Grant;
  if (value.v !== 1 || !keyPattern.test(value.key) || url.pathname !== `/objects/${value.key}`
      || request.method !== value.operation || !Number.isSafeInteger(value.until)
      || value.until <= Date.now() || value.until > Date.now() + 900_000) return reject(403, 'INVALID_GRANT');
  if (value.operation === 'PUT') shape(value.size, value.sha256, value.type, value.key);
  else if (value.operation !== 'GET') return reject(403, 'INVALID_GRANT');
  return value;
}
function metadata(item: R2Object, key: string) {
  if (!item.checksums.sha256 || !mimeTypes.has(item.httpMetadata?.contentType || '') || item.size < 1 || item.size > maximum) return reject(409, 'INTEGRITY_MISMATCH');
  return { bytes: item.size, size: item.size, sha256: hex(item.checksums.sha256), etag: item.etag,
    content_type: item.httpMetadata!.contentType!, object_key: key, version_id: null };
}
export async function storageControl(request: Request, env: StorageEnv): Promise<Response> {
  try {
    if (!await authorized(request, env.REPLAY_CONTROL_TOKEN)) return json({ code: 'UNAUTHORIZED' }, 401);
    if (request.method !== 'POST') return reject();
    const input = await body(request);
    if (input.operation === 'health') {
      // R2 binding access is checked without creating a canary or reading video.
      await env.MEDIA.head('replay/_health/probe');
      return json({ ready: true, access: 'private' });
    }
    const key = await scopedKey(input.tenant_id, input.object_key);
    if (input.operation === 'upload') {
      const constrained = shape(input.size, input.sha256, input.content_type ?? 'video/mp4', key);
      const expires = integer(input.expires ?? 900, 900);
      return json({ url: await signed(env, { v: 1, operation: 'PUT', key, until: Date.now() + expires * 1000, ...constrained }),
        method: 'PUT', headers: { 'Content-Type': constrained.type, 'Content-Length': String(constrained.size) },
        expires_in: expires, object_key: key });
    }
    if (input.operation === 'delete') { await env.MEDIA.delete(key); return json({ deleted: true }); }
    if (!['head', 'verify', 'download'].includes(String(input.operation))) return reject();
    const item = await env.MEDIA.head(key);
    if (!item) return reject(404, 'OBJECT_NOT_FOUND');
    const result = metadata(item, key);
    if (input.operation === 'verify') {
      const expected = shape(input.size, input.sha256, input.content_type ?? 'video/mp4', key);
      if (result.size !== expected.size || result.sha256 !== expected.sha256 || result.content_type !== expected.type) return reject(409, 'INTEGRITY_MISMATCH');
    }
    if (input.operation !== 'download') return json(result);
    const expires = integer(input.expires ?? 300, 900);
    const filename = input.filename === undefined ? undefined : String(input.filename).replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 180);
    return json({ url: await signed(env, { v: 1, operation: 'GET', key, until: Date.now() + expires * 1000, filename }),
      method: 'GET', headers: {}, expires_in: expires });
  } catch (error) {
    return json({ code: error instanceof Failure ? error.code : 'STORAGE_UNAVAILABLE' }, error instanceof Failure ? error.status : 503);
  }
}
export async function storageObject(request: Request, env: StorageEnv): Promise<Response> {
  const origin = request.headers.get('origin');
  const cors: Record<string, string> = { Vary: 'Origin' };
  if (origin) {
    if (!env.REPLAY_ORIGINS.split(',').map(s => s.trim()).includes(origin)) return json({ code: 'UNAUTHORIZED' }, 403);
    cors['Access-Control-Allow-Origin'] = origin;
    cors['Access-Control-Expose-Headers'] = 'Content-Length,Content-Range,ETag,Accept-Ranges';
  }
  if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: { ...cors,
    'Access-Control-Allow-Methods': 'GET,PUT,OPTIONS', 'Access-Control-Allow-Headers': 'Content-Type,If-Match,Range', 'Access-Control-Max-Age': '600' } });
  try {
    const value = await grant(request, env);
    if (value.operation === 'PUT') {
      if (request.headers.get('content-length') !== String(value.size) || request.headers.get('content-type') !== value.type || !request.body) return reject(409, 'INTEGRITY_MISMATCH');
      const stream = new FixedLengthStream(value.size!);
      const cancel = new AbortController();
      const copying = request.body.pipeTo(stream.writable, { signal: AbortSignal.any([cancel.signal, request.signal, AbortSignal.timeout(90_000)]) });
      void copying.catch(() => {});
      try {
        const item = await env.MEDIA.put(value.key, stream.readable, { onlyIf: { etagDoesNotMatch: '*' },
          sha256: value.sha256, httpMetadata: { contentType: value.type } });
        if (!item) return reject(409, 'INTEGRITY_MISMATCH');
        await copying;
        return json({ uploaded: true }, 200, cors);
      } finally { cancel.abort(); await copying.catch(() => {}); }
    }
    const ranged = request.headers.has('range');
    const item = await env.MEDIA.get(value.key, { onlyIf: request.headers, ...(ranged ? { range: request.headers } : {}) });
    if (!item) return reject(404, 'OBJECT_NOT_FOUND');
    if (!('body' in item)) return new Response(null, { status: 412, headers: cors });
    const range = item.range as { offset?: number; length?: number; suffix?: number } | undefined;
    const suffix = typeof range?.suffix === 'number';
    const length = ranged && range ? (suffix ? Math.min(range.suffix!, item.size) : range.length ?? item.size - (range.offset ?? 0)) : item.size;
    const offset = suffix ? item.size - length : range?.offset ?? 0;
    const headers = new Headers({ ...cors, 'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer',
      'X-Content-Type-Options': 'nosniff', 'Content-Type': item.httpMetadata?.contentType || 'application/octet-stream',
      ETag: item.httpEtag, 'Accept-Ranges': 'bytes', 'Content-Length': String(length) });
    if (value.filename) headers.set('Content-Disposition', `attachment; filename="${value.filename}"`);
    if (ranged && item.range) {
      headers.set('Content-Range', `bytes ${offset}-${offset + length - 1}/${item.size}`);
    }
    return new Response(item.body, { status: ranged && item.range ? 206 : 200, headers });
  } catch (error) {
    return json({ code: error instanceof Failure ? error.code : 'STORAGE_UNAVAILABLE' }, error instanceof Failure ? error.status : 503, cors);
  }
}
