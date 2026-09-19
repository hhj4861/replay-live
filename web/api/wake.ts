import { createHash, timingSafeEqual } from 'node:crypto';
import { dispatchQueueEnabled, publishDispatchWakeup } from '../lib/dispatch-wakeup.js';

const invalid = () => new Error('Invalid wakeup request');

async function validateBody(request: Request) {
  const length = request.headers.get('content-length');
  if (length !== null && (!/^(0|[1-9][0-9]*)$/.test(length) || Number(length) > 32)) throw invalid();
  if (!request.body) return;
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(5000)]);
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      signal.throwIfAborted();
      let onAbort: (() => void) | undefined;
      const next = await Promise.race([reader.read(), new Promise<never>((_, reject) => {
        onAbort = () => reject(invalid());
        signal.addEventListener('abort', onAbort, { once: true });
      })]).finally(() => { if (onAbort) signal.removeEventListener('abort', onAbort); });
      if (next.done) break;
      size += next.value.byteLength;
      if (size > 32) throw invalid();
      chunks.push(next.value);
    }
    if (!size) return;
    if (request.headers.get('content-type')?.split(';', 1)[0].trim().toLowerCase() !== 'application/json'
        || !/^[ \t\r\n]*\{[ \t\r\n]*"v"[ \t\r\n]*:[ \t\r\n]*1[ \t\r\n]*\}[ \t\r\n]*$/.test(Buffer.concat(chunks).toString('utf8'))) throw invalid();
  } finally { void reader.cancel().catch(() => {}); }
}

async function wake(request: Request): Promise<Response> {
  const response = (body: unknown, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });
  if (!['GET', 'POST'].includes(request.method)) return new Response(null, { status: 405, headers: { Allow: 'GET, POST', 'Cache-Control': 'no-store' } });
  // Cron recovery and API event notifications have separate credentials.
  const expected = request.method === 'GET' ? process.env.CRON_SECRET : process.env.REPLAY_CONTROL_TOKEN;
  const digest = (value: string) => createHash('sha256').update(value).digest();
  if (!expected || expected.length < 32 || !timingSafeEqual(digest(request.headers.get('authorization') || ''), digest(`Bearer ${expected}`))) {
    return response({ detail: 'Unauthorized' }, 401);
  }
  if (!dispatchQueueEnabled()) return response({ disabled: true });
  try {
    if (request.method === 'GET') {
      if (request.body || (request.headers.has('content-length') && request.headers.get('content-length') !== '0')) throw invalid();
    } else await validateBody(request);
  } catch { return response({ detail: 'Invalid wakeup request' }, 400); }
  try {
    await publishDispatchWakeup();
    return response({ accepted: true }, 202);
  } catch { return response({ detail: 'Dispatch wakeup unavailable' }, 503); }
}

const wakeHandler = { fetch: wake };
export default wakeHandler;
