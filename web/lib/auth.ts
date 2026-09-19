import { OidcClient, UserManager, WebStorageStateStore } from 'oidc-client-ts';
import { LOGIN_PROVIDER, clearGoogleSession, googleAccessToken, googleAuthIdentity, hasGoogleSession, requireGoogleSignIn } from './google-auth';
export const AUTH_PROVIDER = LOGIN_PROVIDER;

declare const __REPLAY_COMMERCIAL__: boolean | undefined;
declare const __REPLAY_OIDC__: { authority: string; client_id: string; scope: string; audience: string } | undefined;
export const COMMERCIAL = typeof __REPLAY_COMMERCIAL__ === 'boolean' && __REPLAY_COMMERCIAL__;
let manager: UserManager | undefined;
let renewing: Promise<string> | undefined;
let completing: Promise<boolean> | undefined;
let generation = 0;
let signedOut = false;
const userStorageKeys = new Set<string>();

function fencedUserStorage(version: number): Storage {
  const current = () => version === generation && !signedOut;
  const storage = window.sessionStorage;
  // The library can finish a token refresh after a newer login. Fence the
  // underlying synchronous write/removal, not only the eventual promise result.
  return {
    get length() { return current() ? storage.length : 0; },
    key: index => current() ? storage.key(index) : null,
    getItem: key => { userStorageKeys.add(key); return current() ? storage.getItem(key) : null; },
    setItem: (key, value) => { userStorageKeys.add(key); if (current()) storage.setItem(key, value); },
    removeItem: key => { if (current()) storage.removeItem(key); },
    clear: () => { if (current()) for (const key of userStorageKeys) storage.removeItem(key); },
  };
}

export function authIdentity(): string {
  return AUTH_PROVIDER === 'google' ? googleAuthIdentity() : `oidc:${generation}`;
}

function client() {
  if (manager) return manager;
  const config = typeof __REPLAY_OIDC__ === 'object' ? __REPLAY_OIDC__ : undefined;
  if (!config?.authority || !config.client_id || !config.authority.startsWith('https://')) {
    throw new Error('로그인 연결이 아직 준비되지 않았습니다. 관리자에게 문의하세요.');
  }
  manager = new UserManager({ authority: config.authority, client_id: config.client_id,
    redirect_uri: window.location.origin + '/', post_logout_redirect_uri: window.location.origin + '/',
    response_type: 'code', scope: config.scope || 'openid profile offline_access',
    extraQueryParams: config.audience ? { audience: config.audience } : undefined,
    automaticSilentRenew: false, monitorSession: false, revokeTokensOnSignout: true,
    userStore: new WebStorageStateStore({ store: fencedUserStorage(generation) }),
    stateStore: new WebStorageStateStore({ store: window.sessionStorage }),
  });
  manager.events.addAccessTokenExpiring(() => { if (!signedOut) void accessToken(true).catch(() => undefined); });
  return manager;
}

async function finishSignIn() {
  const version = generation;
  const query = new URLSearchParams(window.location.search);
  if (query.has('state') && (query.has('code') || query.has('error'))) {
    try { await client().signinRedirectCallback(); }
    finally { window.history.replaceState(null, '', window.location.pathname); }
  }
  const user = await client().getUser();
  if (version !== generation || signedOut) return false;
  return Boolean(user && !user.expired);
}

export function completeSignIn() {
  if (AUTH_PROVIDER === 'google') return Promise.resolve(hasGoogleSession());
  if (!completing) completing = finishSignIn().finally(() => { completing = undefined; });
  return completing;
}
export function signIn() {
  if (AUTH_PROVIDER === 'google') { requireGoogleSignIn(); return Promise.resolve(); }
  generation += 1; signedOut = false; manager?.stopSilentRenew(); manager = undefined; renewing = undefined;
  return client().signinRedirect();
}
export async function signOut() {
  if (AUTH_PROVIDER === 'google') { clearGoogleSession(); return; }
  const auth = client();
  const previousUser = auth.getUser();
  const version = ++generation;
  signedOut = true;
  auth.stopSilentRenew();
  manager = undefined; renewing = undefined;
  for (const key of userStorageKeys) window.sessionStorage.removeItem(key);
  userStorageKeys.clear();
  // removeUser only unloads this retired manager's events: its storage facade
  // is already fenced and cannot remove a later account's credentials.
  const removed = auth.removeUser();
  const [user] = await Promise.all([previousUser, removed]);
  if (version !== generation || !signedOut) return;
  const values = new Map<string, string>();
  const logout = new OidcClient({ authority: auth.settings.authority, client_id: auth.settings.client_id,
    redirect_uri: window.location.origin + '/', response_type: 'code', requestTimeoutInSeconds: 8,
    stateStore: { set: async (key, value) => { values.set(key, value); },
      get: async key => values.get(key) ?? null,
      remove: async key => { const value = values.get(key) ?? null; values.delete(key); return value; },
      getAllKeys: async () => [...values.keys()] } });
  // Preserve provider token revocation using the captured old user only.
  await Promise.allSettled([
    ...(user?.refresh_token ? [logout.revokeToken(user.refresh_token, 'refresh_token')] : []),
    ...(user?.access_token ? [logout.revokeToken(user.access_token, 'access_token')] : []),
  ]);
  if (version !== generation || !signedOut) return;
  const request = await logout.createSignoutRequest({ id_token_hint: user?.id_token,
    post_logout_redirect_uri: window.location.origin + '/' });
  if (version === generation && signedOut) window.location.assign(request.url);
}
export async function accessToken(forceRefresh = false, rejectedToken?: string): Promise<string> {
  if (AUTH_PROVIDER === 'google') return googleAccessToken(forceRefresh, rejectedToken);
  if (signedOut) throw new Error('다시 로그인하세요.');
  const version = generation;
  const auth = client();
  const user = await auth.getUser();
  if (version !== generation) throw new Error('이전 로그인 요청이 취소되었습니다.');
  if (user && !user.expired && (!forceRefresh || (rejectedToken !== undefined && user.access_token !== rejectedToken))) return user.access_token;
  if (!renewing) {
    const pending = auth.signinSilent().then(value => {
      if (version !== generation || signedOut) throw new Error('이전 로그인 요청이 취소되었습니다.');
      if (!value || value.expired) throw new Error('다시 로그인하세요.');
      return value.access_token;
    }).finally(() => { if (renewing === pending) renewing = undefined; });
    renewing = pending;
  }
  return renewing;
}
