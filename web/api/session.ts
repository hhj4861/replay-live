import { createHash, timingSafeEqual } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';
import { Sandbox } from '@vercel/sandbox';

const DEFAULT_ORIGIN = 'https://replay-live-poc.vercel.app';
const MINIMUM_REMAINING_MS = 3 * 60 * 1000;
const STARTUP_TIMEOUT_MS = 100_000;

class LoginError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

function reply(body: unknown, status: number, origin?: string) {
  const headers = new Headers({
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    Vary: 'Origin',
  });
  if (origin) headers.set('Access-Control-Allow-Origin', origin);
  return Response.json(body, { status, headers });
}

async function readCode(request: Request) {
  if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') {
    throw new LoginError(415, '접속 코드를 JSON 형식으로 전송하세요.');
  }
  const reader = request.body?.getReader();
  if (!reader) throw new LoginError(400, '테스트 접속 코드를 입력하세요.');
  const chunks: Uint8Array[] = [];
  let size = 0;
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    void reader.cancel().catch(() => undefined);
  }, 5_000);
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (timedOut) throw new LoginError(408, '접속 요청 시간이 초과되었습니다. 다시 시도하세요.');
      if (done) break;
      size += value.byteLength;
      if (size > 4096) {
        void reader.cancel().catch(() => undefined);
        throw new LoginError(413, '접속 요청이 너무 큽니다.');
      }
      chunks.push(value);
    }
  } finally {
    clearTimeout(timer);
    reader.releaseLock();
  }
  let payload: unknown;
  try {
    payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new LoginError(400, '접속 요청 형식이 올바르지 않습니다.');
  }
  const code = payload && typeof payload === 'object' && 'code' in payload ? payload.code : undefined;
  if (typeof code !== 'string' || code.length < 1 || code.length > 200) {
    throw new LoginError(400, '테스트 접속 코드를 확인하세요.');
  }
  const resume = payload && typeof payload === 'object' && 'resume_token' in payload ? payload.resume_token : undefined;
  if (resume !== undefined && resume !== null && (typeof resume !== 'string' || resume.length > 200)) {
    throw new LoginError(400, '이전 접속 정보를 확인하세요.');
  }
  return { code, resume_token: typeof resume === 'string' ? resume : undefined };
}

function expiresAt(sandbox: Sandbox) {
  const expires = sandbox.expiresAt?.getTime();
  if (!expires || !Number.isFinite(expires)) {
    throw new LoginError(503, '테스트 서버의 이용 시간을 확인할 수 없습니다. 잠시 후 다시 접속하세요.');
  }
  if (expires - Date.now() < MINIMUM_REMAINING_MS) {
    throw new LoginError(503, '현재 테스트 서버의 이용 시간이 곧 끝납니다. 약 3분 뒤 다시 접속하면 테스트 서버를 재개합니다.');
  }
  return Math.floor(expires / 1000);
}

async function backendReady(apiBase: string, origin: string, signal: AbortSignal) {
  try {
    const response = await fetch(`${apiBase}/health`, {
      headers: { Origin: origin },
      signal: AbortSignal.any([signal, AbortSignal.timeout(3_000)]),
      redirect: 'error',
    });
    // The public health endpoint deliberately requires authentication.
    const body: unknown = await response.json();
    return response.status === 401 && typeof body === 'object' && body !== null
      && 'detail' in body && body.detail === '테스트 접속 코드로 먼저 접속하세요.';
  } catch {
    return false;
  }
}

async function handle(request: Request) {
  // The commercial deployment uses the authenticated production API. Retire
  // the old invite-code gateway before reading a request or touching Sandbox.
  if (process.env.REPLAY_COMMERCIAL === '1') return reply({ detail: 'Not found' }, 404);
  const origins = (process.env.REPLAY_PUBLIC_ORIGINS || DEFAULT_ORIGIN)
    .split(',').map(value => value.trim()).filter(Boolean);
  const origin = request.headers.get('origin') || '';
  if (!origins.includes(origin)) {
    return reply({ detail: '허용된 테스트 화면에서 접속하세요.' }, 403);
  }
  if (request.method === 'OPTIONS') {
    return new Response(null, {
      status: 204,
      headers: {
        'Access-Control-Allow-Origin': origin,
        'Access-Control-Allow-Methods': 'POST, OPTIONS',
        'Access-Control-Allow-Headers': 'Content-Type, X-Replay-Client',
        'Cache-Control': 'no-store',
        Vary: 'Origin',
      },
    });
  }
  if (request.method !== 'POST') {
    return reply({ detail: 'POST 요청으로 접속하세요.' }, 405, origin);
  }
  if (request.headers.get('x-replay-client') !== '1') {
    return reply({ detail: '테스트 관리 화면에서 접속하세요.' }, 403, origin);
  }
  try {
    const { code, resume_token } = await readCode(request);
    const expectedCode = process.env.REPLAY_INVITE_CODE;
    const name = process.env.REPLAY_SANDBOX_NAME;
    if (!expectedCode || !name) {
      throw new LoginError(503, '테스트 서버 설정이 완료되지 않았습니다. 운영자에게 문의하세요.');
    }
    const digest = (value: string) => createHash('sha256').update(value).digest();
    if (!timingSafeEqual(digest(code), digest(expectedCode))) {
      throw new LoginError(401, '테스트 접속 코드를 확인하세요.');
    }

    // Authentication precedes every cloud operation. Never create or replace a sandbox here.
    const signal = AbortSignal.timeout(STARTUP_TIMEOUT_MS);
    const sandbox = await Sandbox.get({ name, resume: true, signal });
    let sandboxExpires = expiresAt(sandbox);
    const domain = new URL(sandbox.domain(8081));
    if (domain.protocol !== 'https:' || domain.username || domain.password) {
      throw new LoginError(503, '테스트 서버 주소를 확인할 수 없습니다. 운영자에게 문의하세요.');
    }
    const apiBase = `${domain.origin}/api`;
    if (!await backendReady(apiBase, origin, signal)) {
      const startup = await sandbox.runCommand({
        cmd: 'bash',
        args: ['/vercel/sandbox/replay/start-api.sh', domain.hostname, String(sandboxExpires)],
        signal,
        timeoutMs: 15_000,
      });
      if (startup.exitCode !== 0) {
        throw new LoginError(503, '테스트 서버를 시작하지 못했습니다. 잠시 후 다시 접속하세요.');
      }
      let ready = false;
      while (!signal.aborted) {
        if (await backendReady(apiBase, origin, signal)) {
          ready = true;
          break;
        }
        await delay(1_000, undefined, { signal });
      }
      if (!ready) throw new LoginError(503, '테스트 서버를 준비하는 데 시간이 걸립니다. 잠시 후 다시 접속하세요.');
    }
    sandboxExpires = expiresAt(sandbox);
    const response = await fetch(`${apiBase}/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Replay-Client': '1', Origin: origin },
      body: JSON.stringify({ code, resume_token }),
      signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]),
      redirect: 'error',
    });
    if (!response.ok) {
      if (response.status === 429) {
        throw new LoginError(429, '현재 테스트 접속 한도가 찼습니다. 잠시 후 다시 시도하거나 운영자에게 문의하세요.');
      }
      throw new LoginError(503, '테스트 서버에 접속하지 못했습니다. 잠시 후 다시 시도하세요.');
    }
    const result: unknown = await response.json();
    if (!result || typeof result !== 'object' || !('token' in result) || typeof result.token !== 'string'
      || !('expires_at' in result) || typeof result.expires_at !== 'number' || !Number.isFinite(result.expires_at)) {
      throw new LoginError(502, '테스트 서버 응답을 확인할 수 없습니다. 잠시 후 다시 접속하세요.');
    }
    return reply({ token: result.token, expires_at: result.expires_at, api_base: apiBase, sandbox_expires_at: sandboxExpires }, 200, origin);
  } catch (error) {
    if (error instanceof LoginError) return reply({ detail: error.message }, error.status, origin);
    return reply({ detail: '클라우드 테스트 서버를 준비하지 못했습니다. 잠시 후 다시 접속하세요.' }, 503, origin);
  }
}

const sessionHandler = { fetch: handle };
export default sessionHandler;
