declare const __REPLAY_LOGIN__: { provider: string; google_client_id: string } | undefined;
declare const __REPLAY_API_BASE__: string | undefined;

const login = typeof __REPLAY_LOGIN__ === 'object' ? __REPLAY_LOGIN__ : undefined;
export const LOGIN_PROVIDER = login?.provider || 'oidc';
export const GOOGLE_CLIENT_ID = login?.google_client_id || '';
const base = typeof __REPLAY_API_BASE__ === 'string' && __REPLAY_API_BASE__ ? __REPLAY_API_BASE__ : '/api';
const storageKey = 'replay-google-session';
type Session = { token: string; expires_at: number; absolute_expires_at: number };
export type GoogleChallenge = { challenge_id: string; challenge_secret: string; nonce: string; expires_at: number };
let generation = 0;
let signedOut = false;
let renewing: Promise<string> | undefined;

function validSession(value: unknown): value is Session {
  if (!value || typeof value !== 'object') return false;
  const session = value as Session;
  return typeof session.token === 'string' && /^[A-Za-z0-9_-]{32,128}$/.test(session.token)
    && Number.isFinite(session.expires_at) && Number.isFinite(session.absolute_expires_at)
    && session.expires_at <= session.absolute_expires_at && session.expires_at > Date.now() / 1000
    && session.expires_at <= Date.now() / 1000 + 3660
    && session.absolute_expires_at <= Date.now() / 1000 + 86460;
}
function stored(): Session | undefined {
  try {
    const session: unknown = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
    return validSession(session) ? session : undefined;
  } catch { return undefined; }
}
async function call<T>(path: string, body?: unknown, token?: string): Promise<T> {
  const response = await fetch(`${base}/auth/google/${path}`, { method: 'POST', credentials: 'omit',
    redirect: 'error', signal: AbortSignal.timeout(12000), headers: { 'Content-Type': 'application/json', 'X-Replay-Client': '1',
      ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: JSON.stringify(body ?? {}) });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = result && typeof result === 'object' && 'detail' in result ? result.detail : undefined;
    throw new Error(typeof detail === 'string' ? detail : 'Google 로그인 연결을 확인하세요.');
  }
  return result as T;
}
export function hasGoogleSession() { return !signedOut && Boolean(stored()); }
export function googleAuthIdentity(): string { return `google:${generation}`; }
export function beginGoogleLogin() { signedOut = false; return ++generation; }
export function cancelGoogleLogin(attempt: number) { if (generation === attempt) generation += 1; }
export async function googleChallenge(): Promise<GoogleChallenge> {
  const value = await call<GoogleChallenge>('challenge');
  if (!value || ![value.challenge_id, value.challenge_secret, value.nonce].every(item => typeof item === 'string' && /^[A-Za-z0-9_-]{16,128}$/.test(item))
    || !Number.isFinite(value.expires_at) || value.expires_at <= Date.now() / 1000) throw new Error('로그인 요청을 다시 시작하세요.');
  return value;
}
export async function finishGoogleLogin(credential: string, challenge: GoogleChallenge, attempt: number) {
  if (attempt !== generation || signedOut) throw new Error('이전 로그인 요청이 취소되었습니다.');
  const session = await call<Session>('exchange', { credential, challenge_id: challenge.challenge_id,
    challenge_secret: challenge.challenge_secret });
  if (attempt !== generation || signedOut) throw new Error('이전 로그인 요청이 취소되었습니다.');
  if (!validSession(session)) throw new Error('로그인 응답을 확인할 수 없습니다. 다시 시도하세요.');
  // Store only the application session. Google credentials never enter storage.
  sessionStorage.setItem(storageKey, JSON.stringify(session));
}
export function clearGoogleSession() { generation += 1; signedOut = true; sessionStorage.removeItem(storageKey); }
export function requireGoogleSignIn() {
  clearGoogleSession();
  window.dispatchEvent(new CustomEvent('replay-signin-required'));
}
export async function googleAccessToken(forceRefresh = false, rejectedToken?: string): Promise<string> {
  const session = stored();
  if (signedOut || !session) { requireGoogleSignIn(); throw new Error('Google 계정으로 다시 로그인하세요.'); }
  if (renewing) return renewing;
  // A delayed 401 for a retired token must not rotate the newer token again.
  if ((!forceRefresh || (rejectedToken !== undefined && session.token !== rejectedToken))
    && session.expires_at > Date.now() / 1000 + 30) return session.token;
  const current = generation;
  if (!renewing) renewing = call<Session>('refresh', undefined, session.token).then(value => {
    if (current !== generation || signedOut) throw new Error('이전 로그인 요청이 취소되었습니다.');
    if (!validSession(value)) throw new Error('로그인 세션을 갱신하지 못했습니다.');
    sessionStorage.setItem(storageKey, JSON.stringify(value));
    return value.token;
  }).catch(error => { if (current === generation) requireGoogleSignIn(); throw error; })
    .finally(() => { renewing = undefined; });
  return renewing;
}
