import { api, request, assertSessionIdentity } from './api';

export type MemberProfile = { id: string; email: string; enabled: boolean; created_at: number; updated_at: number };
export type Account = { tenant_id: string; subject: string; roles: string[]; profile?: MemberProfile | null;
  permissions?: { manage_members: boolean } };
export type Member = MemberProfile & { roles: string[]; stream_connection_count: number; active_session_count: number };
export type MemberConnection = { target: string; updated_at: number; has_stream_key: true };
export type MemberDetail = Member & { connections: MemberConnection[]; active_jobs_count: number };
export type MemberPage = { items: Member[]; total: number; offset: number; limit: number };
const invalid = '회원 정보를 확인할 수 없습니다. 다시 불러오세요.';

export function canManageMembers(account?: Account): boolean {
  return account?.permissions?.manage_members === true && account.roles.includes('site_admin');
}

function member(value: Member): Member {
  if (!value || typeof value.id !== 'string' || !value.id || typeof value.email !== 'string'
      || typeof value.enabled !== 'boolean' || !Array.isArray(value.roles) || !value.roles.every(role => typeof role === 'string')
      || ![value.created_at, value.updated_at, value.stream_connection_count, value.active_session_count].every(n => Number.isFinite(n) && n >= 0)) throw new Error(invalid);
  return { id: value.id, email: value.email, enabled: value.enabled, roles: [...value.roles], created_at: value.created_at,
    updated_at: value.updated_at, stream_connection_count: value.stream_connection_count, active_session_count: value.active_session_count };
}
function detail(value: MemberDetail): MemberDetail {
  const result = member(value);
  if (!Array.isArray(value.connections) || !Number.isFinite(value.active_jobs_count) || value.active_jobs_count < 0) throw new Error(invalid);
  const connections = value.connections.map(connection => {
    if (!connection || typeof connection.target !== 'string' || !connection.target || !Number.isFinite(connection.updated_at)
        || connection.updated_at < 0 || connection.has_stream_key !== true) throw new Error(invalid);
    // Admin responses never keep unexpected secrets, URLs, or ciphertext fields.
    return { target: connection.target, updated_at: connection.updated_at, has_stream_key: true as const };
  });
  return { ...result, connections, active_jobs_count: value.active_jobs_count };
}
function segment(value: string): string {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(value)) throw new Error(invalid);
  return encodeURIComponent(value);
}

// Bind a panel's requests to the login that opened it, including before token acquisition.
export function createMemberManagementClient(identity: string) {
  async function read<T>(path: string, init: RequestInit = {}): Promise<T> {
    assertSessionIdentity(identity);
    const timeout = AbortSignal.timeout(15_000);
    const value = await api<T>(path, { ...init, cache: 'no-store', signal: init.signal ? AbortSignal.any([init.signal, timeout]) : timeout });
    assertSessionIdentity(identity);
    return value;
  }
  const body = (method: string, value: unknown, signal?: AbortSignal): RequestInit => ({ method, signal,
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) });
  return {
    async list(query: string, offset = 0, signal?: AbortSignal): Promise<MemberPage> {
      if (query.length > 254 || !Number.isSafeInteger(offset) || offset < 0) throw new Error(invalid);
      const value = await read<MemberPage>(`/admin/members?q=${encodeURIComponent(query.trim())}&offset=${offset}&limit=25`, { signal });
      if (!value || !Array.isArray(value.items) || ![value.total, value.offset, value.limit].every(n => Number.isSafeInteger(n) && n >= 0)
          || value.limit < 1 || value.limit > 100) throw new Error(invalid);
      return { items: value.items.map(member), total: value.total, offset: value.offset, limit: value.limit };
    },
    async detail(id: string, signal?: AbortSignal) { return detail(await read<MemberDetail>(`/admin/members/${segment(id)}`, { signal })); },
    async setEnabled(id: string, enabled: boolean, signal?: AbortSignal) {
      return detail(await read<MemberDetail>(`/admin/members/${segment(id)}/status`, body('PUT', { enabled }, signal)));
    },
    async revokeSessions(id: string, signal?: AbortSignal) {
      const value = await read<{ revoked: boolean }>(`/admin/members/${segment(id)}/revoke-sessions`, body('POST', {}, signal));
      if (value?.revoked !== true) throw new Error(invalid);
    },
    async removeConnection(id: string, target: string, signal?: AbortSignal) {
      assertSessionIdentity(identity);
      const timeout = AbortSignal.timeout(15_000);
      await request(`/admin/members/${segment(id)}/stream-connections/${segment(target)}`, { method: 'DELETE',
        signal: signal ? AbortSignal.any([signal, timeout]) : timeout, cache: 'no-store' });
      assertSessionIdentity(identity);
    },
  };
}
export type MemberManagementClient = ReturnType<typeof createMemberManagementClient>;
