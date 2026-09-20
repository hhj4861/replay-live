import { api, assertSessionIdentity, sessionIdentity } from './api';

export const LOCAL_IMPORT_URL = 'http://127.0.0.1:17833';
export type LocalImportPhase = 'requesting' | 'downloading' | 'transferring' | 'uploading' | 'validating';
export class LocalImportError extends Error {
  constructor(message: string, readonly phase: LocalImportPhase) { super(message); this.name = 'LocalImportError'; }
}
type Pairing = { token: string; identity: string; direct: boolean };
type LocalJob = { id: string; state: string; error_code?: string; bytes?: number; sha256?: string; media_id?: string; phase?: LocalImportPhase };
const unavailable = '영상 가져오기 도우미를 실행하고 브라우저의 내 컴퓨터 연결 권한을 허용해 주세요.';

function pause(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    signal.throwIfAborted();
    const onAbort = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(); }, 1000);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

export function createLocalImporter() {
  // Separate capability, kept only in memory. Never send the cloud bearer here.
  let pairing: Pairing | undefined;
  let generation = 0;

  async function request(path: string, init: RequestInit = {}, owner?: Pairing, timeout = 12_000) {
    const headers = new Headers(init.headers);
    headers.set('X-Replay-Local', '1');
    if (owner) headers.set('Authorization', `Bearer ${owner.token}`);
    const options: RequestInit & { targetAddressSpace: string } = {
      ...init, headers, credentials: 'omit', redirect: 'error', cache: 'no-store',
      targetAddressSpace: 'loopback',
      signal: AbortSignal.any([AbortSignal.timeout(timeout), ...(init.signal ? [init.signal] : [])]),
    };
    let response: Response;
    try { response = await fetch(`${LOCAL_IMPORT_URL}${path}`, options); }
    catch (error) {
      if (init.signal?.aborted) throw error;
      if (owner && pairing === owner) pairing = undefined;
      throw new Error(unavailable);
    }
    if (!response.ok) {
      if (response.status === 401 && pairing === owner) pairing = undefined;
      const result = await response.json().catch(() => ({})) as { detail?: unknown };
      throw new Error(typeof result.detail === 'string' ? result.detail : `도우미 요청 실패 (${response.status})`);
    }
    return response;
  }

  function assertOwner(owner: Pairing) {
    assertSessionIdentity(owner.identity);
    if (pairing !== owner) throw new Error('내 컴퓨터 연결이 변경되어 요청을 취소했습니다.');
  }

  async function disconnect() {
    generation += 1;
    const old = pairing;
    pairing = undefined;
    if (old) await request('/session', { method: 'DELETE' }, old, 3000).catch(() => undefined);
  }

  return {
    isConnected: () => !!pairing && pairing.identity === sessionIdentity(),
    disconnect,
    async pair(code: string) {
      const identity = sessionIdentity();
      const cleanup = disconnect();
      const version = generation;
      await cleanup;
      assertSessionIdentity(identity);
      if (version !== generation) throw new Error('연결 요청이 취소됐습니다.');
      const result = await (await request('/pair', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: code.trim() }) })).json() as { token: string; version: number; features?: string[] };
      if (result.version !== 1 || !/^[A-Za-z0-9_-]{43}$/.test(result.token)) throw new Error('도우미를 최신 버전으로 실행해 주세요.');
      const candidate = { token: result.token, identity, direct: result.features?.includes('cloud-direct-upload') === true };
      try {
        assertSessionIdentity(identity);
        if (version !== generation) throw new Error('연결 요청이 취소됐습니다.');
        pairing = candidate;
      } catch (error) {
        await request('/session', { method: 'DELETE' }, candidate, 3000).catch(() => undefined);
        throw error;
      }
    },
    async runCloud<T>(input: { provider: string; url: string; name: string }, signal: AbortSignal,
      onPhase: (phase: LocalImportPhase) => void): Promise<T> {
      const owner = pairing;
      if (!owner) throw new Error('내 컴퓨터를 먼저 연결해 주세요.');
      assertOwner(owner);
      if (!owner.direct) throw new Error('도우미를 최신 버전으로 다시 실행한 뒤 연결해 주세요.');
      const bounded = AbortSignal.any([signal, AbortSignal.timeout(540_000)]);
      let id = '';
      let completed = false;
      let phase: LocalImportPhase = 'requesting';
      const reportPhase = (next: LocalImportPhase) => { phase = next; onPhase(next); };
      try {
        reportPhase('requesting');
        const ticket = await api<{ id: string; token: string }>('/device-imports', {
          method: 'POST', signal: bounded, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...input, name: input.name.trim() || `${input.provider}-recording.mp4` }),
        });
        id = ticket.id;
        if (!/^[a-f0-9]{32}$/.test(id) || typeof ticket.token !== 'string') throw new Error('가져오기 작업 응답을 확인할 수 없습니다.');
        assertOwner(owner); bounded.throwIfAborted();
        // Only an opaque, single-task capability crosses to the paired PC.
        let job = await (await request('/cloud-imports', { method: 'POST', signal: bounded,
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, token: ticket.token }) }, owner)).json() as LocalJob;
        const reportJobPhase = () => {
          if (job.phase === 'requesting' || job.phase === 'downloading' || job.phase === 'uploading' || job.phase === 'validating') reportPhase(job.phase);
          else reportPhase('downloading');
        };
        while (job.state === 'downloading' || job.state === 'cancelling') {
          reportJobPhase();
          await pause(bounded); assertOwner(owner);
          job = await (await request(`/imports/${id}`, { signal: bounded }, owner)).json() as LocalJob;
        }
        assertOwner(owner); bounded.throwIfAborted();
        reportJobPhase();
        if (job.state !== 'ready') throw new Error(job.error_code || 'SOURCE_UNAVAILABLE');
        if (job.id !== id || job.media_id !== id) throw new Error('가져오기 결과가 요청한 작업과 다릅니다.');
        reportPhase('validating');
        const result = await api<{ state: string; media: T }>(`/device-imports/${id}`, { signal: bounded });
        assertOwner(owner);
        if (result.state !== 'completed' || !result.media) throw new Error('클라우드 업로드 완료를 확인하지 못했습니다.');
        completed = true;
        return result.media;
      } catch (error) {
        if (signal.aborted) throw error;
        throw new LocalImportError(error instanceof Error ? error.message : 'DEVICE_IMPORT_TASK_FAILED', phase);
      } finally {
        if (id) {
          await request(`/imports/${id}`, { method: 'DELETE' }, owner, 3000).catch(() => undefined);
          if (!completed && owner.identity === sessionIdentity()) {
            await api(`/device-imports/${id}`, { method: 'DELETE', signal: AbortSignal.timeout(5000) }).catch(() => undefined);
          }
        }
      }
    },
    async run(input: { provider: string; url: string; name: string; maxBytes: number; maxDuration: number },
      signal: AbortSignal, onPhase: (phase: LocalImportPhase) => void) {
      const owner = pairing;
      if (!owner) throw new Error('내 컴퓨터를 먼저 연결해 주세요.');
      assertOwner(owner);
      const bounded = AbortSignal.any([signal, AbortSignal.timeout(180_000)]);
      let id = '';
      try {
        onPhase('downloading');
        const created = await (await request('/imports', { method: 'POST', signal: bounded,
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
            request_id: crypto.randomUUID(), provider: input.provider, url: input.url,
            max_bytes: Math.floor(Math.min(input.maxBytes, 50 * 1024 ** 2)),
            max_duration: Math.floor(Math.min(input.maxDuration, 120)),
          }) }, owner)).json() as LocalJob;
        if (!/^[a-f0-9]{32}$/.test(created.id)) throw new Error('도우미의 작업 응답을 확인할 수 없습니다.');
        id = created.id;
        assertOwner(owner);
        let job = created;
        while (job.state === 'downloading' || job.state === 'cancelling') {
          await pause(bounded);
          assertOwner(owner);
          job = await (await request(`/imports/${id}`, { signal: bounded }, owner)).json() as LocalJob;
          assertOwner(owner);
        }
        if (job.state !== 'ready') throw new Error(job.error_code || 'SOURCE_UNAVAILABLE');
        if (!Number.isSafeInteger(job.bytes) || !job.bytes || job.bytes > input.maxBytes
          || job.bytes > 50 * 1024 ** 2 || !/^[a-f0-9]{64}$/.test(job.sha256 || '')) throw new Error('영상 파일 정보를 확인할 수 없습니다.');
        onPhase('transferring');
        const response = await request(`/imports/${id}/file`, { signal: bounded }, owner, 60_000);
        if (!response.body) throw new Error('도우미에서 영상을 받지 못했습니다.');
        const reader = response.body.getReader();
        const chunks: ArrayBuffer[] = [];
        let bytes = 0;
        try {
          for (;;) {
            bounded.throwIfAborted(); assertOwner(owner);
            const { value, done } = await reader.read();
            if (done) break;
            bytes += value.byteLength;
            if (bytes > job.bytes) throw new Error('SOURCE_TOO_LARGE');
            chunks.push(new Uint8Array(value).buffer);
          }
        } finally { await reader.cancel().catch(() => undefined); }
        assertOwner(owner);
        if (bytes !== job.bytes) throw new Error('SOURCE_INCOMPLETE');
        const name = input.name.trim().replace(/\.mp4$/i, '').slice(0, 176) || `${input.provider}-recording`;
        return { file: new File(chunks, `${name}.mp4`, { type: 'video/mp4' }), sha256: job.sha256! };
      } finally {
        // This captured capability can only delete this operation's local file.
        if (id) await request(`/imports/${id}`, { method: 'DELETE' }, owner, 3000).catch(() => undefined);
      }
    },
  };
}
