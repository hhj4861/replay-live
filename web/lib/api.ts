import { COMMERCIAL, accessToken, authIdentity } from './auth';
declare const __REPLAY_API_BASE__: string | undefined;
declare const __REPLAY_CLOUD__: boolean | undefined;
const configured = typeof __REPLAY_API_BASE__ === 'string' ? __REPLAY_API_BASE__ : '';
export const CLOUD_TEST = typeof __REPLAY_CLOUD__ === 'boolean' && __REPLAY_CLOUD__;
export const API_BASE = CLOUD_TEST ? '/api' : configured || '/api';
export const PUBLIC_TEST = !COMMERCIAL && (CLOUD_TEST || Boolean(configured));
export const SESSION_KEY = 'replay-test-session';
const SESSION_BASE_KEY = 'replay-test-api-base';
const SESSION_EXPIRY_KEY = 'replay-test-expires-at';
const SANDBOX_EXPIRY_KEY = 'replay-test-sandbox-expires-at';
const cloudExpired = '클라우드 송출 엔진에 연결할 수 없거나 테스트 시간이 끝났습니다. 접속 코드로 다시 접속하세요.';
let sessionVersion = 0;
let lastCommercialToken: { identity: string; token: string } | undefined;

export type LoginSession = { token: string; expires_at: number; api_base?: string; sandbox_expires_at?: number };

function validBase(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/api'
      ? url.href.replace(/\/$/, '') : null;
  } catch { return null; }
}

export function sandboxExpiresAt(): number {
  return typeof window === 'undefined' ? 0 : Number(sessionStorage.getItem(SANDBOX_EXPIRY_KEY));
}

export function clearSession(message?: string): void {
  sessionVersion += 1;
  for (const key of [SESSION_KEY, SESSION_BASE_KEY, SESSION_EXPIRY_KEY, SANDBOX_EXPIRY_KEY]) sessionStorage.removeItem(key);
  if (message) window.dispatchEvent(new CustomEvent('replay-session-expired', { detail: message }));
}

export function hasSession(): boolean {
  if (!PUBLIC_TEST) return true;
  if (typeof window === 'undefined') return false;
  const token = sessionStorage.getItem(SESSION_KEY);
  const expires = Number(sessionStorage.getItem(SESSION_EXPIRY_KEY));
  const sandboxExpires = sandboxExpiresAt();
  const now = Date.now() / 1000;
  if (!token || (expires && expires <= now) || (CLOUD_TEST && (!validBase(sessionStorage.getItem(SESSION_BASE_KEY)) || !Number.isFinite(expires) || expires <= now))) {
    clearSession();
    return false;
  }
  return !CLOUD_TEST || Number.isFinite(sandboxExpires) && sandboxExpires > now;
}

export function saveSession(result: LoginSession): void {
  const now = Date.now() / 1000;
  const base = validBase(result.api_base ?? null);
  if (typeof result.token !== 'string' || !result.token || !Number.isFinite(result.expires_at) || result.expires_at <= now || (CLOUD_TEST && (!base || !Number.isFinite(result.sandbox_expires_at) || (result.sandbox_expires_at ?? 0) <= now))) {
    throw new Error('테스트 세션 응답을 확인할 수 없습니다. 다시 접속하세요.');
  }
  clearSession();
  sessionStorage.setItem(SESSION_KEY, result.token);
  sessionStorage.setItem(SESSION_EXPIRY_KEY, String(result.expires_at));
  if (CLOUD_TEST && base) {
    sessionStorage.setItem(SESSION_BASE_KEY, base);
    sessionStorage.setItem(SANDBOX_EXPIRY_KEY, String(result.sandbox_expires_at));
  }
}

export function sessionIdentity(): string {
  return `${sessionVersion}:${COMMERCIAL ? authIdentity() : PUBLIC_TEST ? sessionStorage.getItem(SESSION_KEY) || '' : 'local'}`;
}

export function assertSessionIdentity(expected: string): void {
  if (expected !== sessionIdentity()) throw new Error(COMMERCIAL
    ? '로그인 계정이 변경되어 이전 요청이 취소되었습니다.' : '이전 테스트 세션의 요청이 취소되었습니다.');
}

export async function request(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set('X-Replay-Client', '1');
  const sessionRequest = PUBLIC_TEST && path !== '/session';
  if (sessionRequest && !hasSession()) {
    const message = CLOUD_TEST ? cloudExpired : '테스트 세션이 만료되었습니다. 다시 접속하세요.';
    window.dispatchEvent(new CustomEvent('replay-session-expired', { detail: message }));
    throw new Error(message);
  }
  const identity = sessionIdentity();
  let sentToken: string | undefined;
  if (COMMERCIAL) {
    assertSessionIdentity(identity);
    sentToken = await accessToken();
    assertSessionIdentity(identity);
    lastCommercialToken = { identity, token: sentToken };
    headers.set('Authorization', `Bearer ${sentToken}`);
  }
  if (sessionRequest) headers.set('Authorization', `Bearer ${sessionStorage.getItem(SESSION_KEY)}`);
  const base = CLOUD_TEST && sessionRequest ? validBase(sessionStorage.getItem(SESSION_BASE_KEY))! : API_BASE;
  let response: Response;
  try {
    assertSessionIdentity(identity);
    response = await fetch(`${base}${path}`, { ...init, headers, credentials: 'omit' });
    assertSessionIdentity(identity);
  } catch (error) {
    if (sessionRequest && !init?.signal?.aborted && identity === sessionIdentity()) {
      throw new Error('서버와 연결이 끊겼습니다. 접속 정보는 유지됩니다. 잠시 후 다시 시도하세요.');
    }
    throw error;
  }
  assertSessionIdentity(identity);
  if (COMMERCIAL && response.status === 401) {
    assertSessionIdentity(identity);
    const refreshed = await accessToken(true, sentToken);
    assertSessionIdentity(identity);
    lastCommercialToken = { identity, token: refreshed };
    headers.set('Authorization', `Bearer ${refreshed}`);
    response = await fetch(`${base}${path}`, { ...init, headers, credentials: 'omit' });
    assertSessionIdentity(identity);
  }
  if (!response.ok) {
    if (sessionRequest && (response.status === 401)) {
      const message = CLOUD_TEST ? cloudExpired : '테스트 세션이 만료되었습니다. 다시 접속하세요.';
      clearSession(message);
      throw new Error(message);
    }
    const body = await response.json().catch(() => ({})) as { detail?: unknown };
    assertSessionIdentity(identity);
    throw new Error(typeof body.detail === 'string' ? body.detail : `요청 실패 (${response.status})`);
  }
  return response;
}
export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const identity = sessionIdentity();
  const result: T = await (await request(path, init)).json();
  assertSessionIdentity(identity);
  return result;
}

// Capture before signOut fences the identity. This one-shot revocation cannot
// acquire/refresh a replacement account's token and never persists the bearer.
export async function revokeCurrentSession(): Promise<boolean> {
  const current = lastCommercialToken;
  lastCommercialToken = undefined;
  if (!COMMERCIAL || !current || current.identity !== sessionIdentity()) return false;
  try {
    const response = await fetch(`${API_BASE}/logout`, { method: 'POST', credentials: 'omit', redirect: 'error',
      cache: 'no-store', signal: AbortSignal.timeout(10_000), headers: { Authorization: `Bearer ${current.token}`,
        'Content-Type': 'application/json', 'X-Replay-Client': '1' }, body: '{}' });
    return response.ok;
  } catch { return false; }
  finally { current.token = ''; }
}

// Persist only a digest and request ID, never the stream key or request body.
export async function broadcastIdempotency(body: string): Promise<string> {
  const identity = sessionIdentity();
  const bytes = new TextEncoder().encode(body);
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), n => n.toString(16).padStart(2, '0')).join('');
  assertSessionIdentity(identity);
  const previous = sessionStorage.getItem('replay-create-id');
  if (previous?.startsWith(digest + ':')) return previous.slice(65);
  const key = crypto.randomUUID();
  sessionStorage.setItem('replay-create-id', digest + ':' + key);
  return key;
}
export function acknowledgeBroadcast() { sessionStorage.removeItem('replay-create-id'); }
export function resumableSession() {
  return Number(sessionStorage.getItem(SESSION_EXPIRY_KEY)) > Date.now() / 1000 ? sessionStorage.getItem(SESSION_KEY) : null;
}
