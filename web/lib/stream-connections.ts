import { api, request, sessionIdentity, assertSessionIdentity } from './api';
import { normalizeWatchUrl } from './watch-links';

export type StreamConnectionOwner = { tenant_id: string; subject: string };
export type StreamConnectionValue = { server_url: string; stream_key: string; channel_url?: string };
export type StreamConnectionMetadata = { target: string; server_url: string; channel_url?: string; updated_at: number; has_stream_key: true };
export type StreamConnection = StreamConnectionValue & { target: string };
export type StreamConnectionLocation = 'browser' | 'account';
export type StreamConnectionStore = {
  readonly namespace: string;
  readonly identity: string;
  readonly location: StreamConnectionLocation;
  list(): Promise<StreamConnectionMetadata[]>;
  save(target: string, value: StreamConnectionValue): Promise<StreamConnectionMetadata>;
  use(target: string): Promise<StreamConnection>;
  remove(target: string): Promise<void>;
};

type Sealed = { iv: ArrayBuffer; ciphertext: ArrayBuffer };
type StoredConnection = { id: string; owner: string; target: string; updated_at: number; server: Sealed; secret: Sealed; channel?: Sealed };
const databaseName = 'replay-stream-connections-v1';
const platforms = new Set(['youtube', 'twitch', 'facebook', 'instagram', 'tiktok', 'naver', 'chzzk', 'kick', 'custom']);
const storageUnavailable = '이 브라우저에서 암호화 저장소를 사용할 수 없습니다. 브라우저 설정을 확인하거나 이번 방송에만 키를 입력하세요.';
const invalidConnection = '송출 서버 URL과 스트림 키를 확인하세요.';

export function streamConnectionLocation(): StreamConnectionLocation {
  return typeof window !== 'undefined' && ['localhost', '127.0.0.1', '[::1]', '::1'].includes(window.location.hostname)
    ? 'browser' : 'account';
}

function targetId(target: string): string {
  if (!platforms.has(target)) throw new Error('저장할 송출 플랫폼을 확인하세요.');
  return target;
}

function isVisibleAscii(value: string): boolean {
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    if (code < 33 || code > 126) return false;
  }
  return true;
}

function safeEncoding(value: string): string {
  if (/%(?![a-fA-F0-9]{2})/.test(value)) throw new Error(invalidConnection);
  let decoded: string;
  try { decoded = decodeURIComponent(value); } catch { throw new Error(invalidConnection); }
  if (!isVisibleAscii(decoded) || /[\\#"']/.test(decoded)) throw new Error(invalidConnection);
  return decoded;
}

function publicIPv4(host: string): boolean {
  const pieces = host.split('.');
  if (pieces.length !== 4 || pieces.some(piece => !/^(0|[1-9][0-9]{0,2})$/.test(piece) || Number(piece) > 255)) return false;
  const [a, b, c, d] = pieces.map(Number);
  // Match server.stream_targets.public_ipv4: global IPv4 only, excluding shared,
  // multicast, reserved, benchmark and documentation ranges. DNS stays server-side.
  return !(a === 0 || a === 10 || a === 127 || a >= 224
    || (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254)
    || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && (b === 168 || (b === 0 && (c === 2 || (c === 0 && d !== 9 && d !== 10)))))
    || (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100)))
    || (a === 203 && b === 0 && c === 113));
}

export function validateStreamConnection(value: StreamConnectionValue): StreamConnectionValue {
  if (typeof value?.server_url !== 'string' || typeof value?.stream_key !== 'string'
      || (value.channel_url !== undefined && typeof value.channel_url !== 'string')
      || !/^[A-Za-z0-9][A-Za-z0-9._~!$&()*+,;=:@?%/-]{0,1023}$/.test(value.stream_key)
      || !isVisibleAscii(value.stream_key) || value.stream_key.includes('://')
      || !value.server_url || value.server_url.length > 2048 || value.server_url.includes('\\')
      || !isVisibleAscii(value.server_url)) {
    throw new Error(invalidConnection);
  }
  try {
    safeEncoding(value.stream_key);
    // Retain the original path for dot-segment checks; URL() can normalize them
    // before validation, unlike the server's urlsplit().
    const original = /^rtmps?:\/\/([^/?#]*)([^?#]*)(?:\?[^#]*)?(?:#.*)?$/i.exec(value.server_url);
    if (!original || original[1].includes('@')) throw new Error();
    const url = new URL(value.server_url);
    const host = url.hostname.toLowerCase();
    const port = Number(url.port) || (url.protocol === 'rtmps:' ? 443 : 1935);
    if (!['rtmp:', 'rtmps:'].includes(url.protocol) || !host || url.username || url.password || url.search || url.hash
        || host.includes('%') || host.endsWith('.') || !(url.protocol === 'rtmp:' ? port === 1935 : port === 443 || port === 1935)) throw new Error();
    if (/^[0-9.]+$/.test(host)) {
      if (!publicIPv4(host)) throw new Error();
    } else if (host.length > 253 || !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(host)
        || /\.(localhost|local|internal|lan|home)$/.test(host) || host.split('.').some(label => label.startsWith('0x'))) {
      throw new Error();
    }
    const path = original[2].replace(/\/+$/, '') || '/';
    if (!/^\/[A-Za-z0-9._~!$&()*+,;=:@%/-]*$/.test(path) || safeEncoding(path).split('/').some(part => part === '.' || part === '..')) throw new Error();
    const serverUrl = `${url.protocol}//${host}:${port}${path.replace(/\/+$/, '')}`;
    if (serverUrl.length >= 1024) throw new Error();
    // Keep the optional channel field through autofill's RTMP validation. The
    // storage boundary below validates it against its platform before use/save.
    return { server_url: serverUrl, stream_key: value.stream_key,
      ...(value.channel_url === undefined ? {} : { channel_url: value.channel_url }) };
  } catch { throw new Error(invalidConnection); }
}

function metadata(value: StreamConnectionMetadata): StreamConnectionMetadata {
  if (!value || !platforms.has(value.target) || typeof value.server_url !== 'string'
      || (value.channel_url !== undefined && typeof value.channel_url !== 'string')
      || !Number.isFinite(value.updated_at) || value.has_stream_key !== true) throw new Error('저장된 연결 정보를 확인할 수 없습니다.');
  // Construct a fresh metadata object; never forward an unexpected server secret.
  return { target: value.target, server_url: value.server_url, channel_url: normalizeWatchUrl(value.target, value.channel_url ?? '', 'channel'),
    updated_at: value.updated_at, has_stream_key: true };
}

function openDatabase(): Promise<IDBDatabase> {
  if (typeof indexedDB === 'undefined' || !globalThis.crypto?.subtle) return Promise.reject(new Error(storageUnavailable));
  return new Promise((resolve, reject) => {
    let settled = false;
    const opening = indexedDB.open(databaseName, 1);
    const fail = () => { settled = true; reject(new Error(storageUnavailable)); };
    opening.onupgradeneeded = () => {
      const db = opening.result;
      if (!db.objectStoreNames.contains('keys')) db.createObjectStore('keys', { keyPath: 'owner' });
      if (!db.objectStoreNames.contains('connections')) db.createObjectStore('connections', { keyPath: 'id' }).createIndex('owner', 'owner');
    };
    opening.onerror = opening.onblocked = fail;
    opening.onsuccess = () => {
      if (settled) { opening.result.close(); return; }
      const db = opening.result;
      db.onversionchange = () => db.close();
      settled = true; resolve(db);
    };
  });
}

function transaction<T>(db: IDBDatabase, store: string, mode: IDBTransactionMode, identity: string,
  run: (values: IDBObjectStore, result: (value: T) => void, fail: () => void) => void): Promise<T> {
  assertSessionIdentity(identity);
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, mode);
    let result: T;
    const fail = () => { try { tx.abort(); } catch { /* Transaction already ended. */ } reject(new Error(storageUnavailable)); };
    tx.onabort = tx.onerror = () => reject(new Error(storageUnavailable));
    tx.oncomplete = () => {
      try { assertSessionIdentity(identity); resolve(result); } catch (error) { reject(error); }
    };
    try { run(tx.objectStore(store), value => { result = value; }, fail); } catch { fail(); }
  });
}

function validKey(key: CryptoKey): CryptoKey {
  if (!key || key.type !== 'secret' || key.extractable || key.algorithm.name !== 'AES-GCM'
      || !key.usages.includes('encrypt') || !key.usages.includes('decrypt')) throw new Error(storageUnavailable);
  return key;
}

async function ownerKey(db: IDBDatabase, owner: string, identity: string, create: boolean): Promise<CryptoKey | undefined> {
  const existing = await transaction<{ owner: string; key: CryptoKey } | undefined>(db, 'keys', 'readonly', identity, (values, done) => {
    const read = values.get(owner); read.onsuccess = () => done(read.result);
  });
  if (existing) return validKey(existing.key);
  if (!create) return undefined;
  const generated = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
  assertSessionIdentity(identity);
  // Recheck inside the exclusive write transaction: two tabs must never replace
  // one another's key while encrypting their first saved connection.
  return transaction<CryptoKey>(db, 'keys', 'readwrite', identity, (values, done, fail) => {
    const read = values.get(owner);
    read.onsuccess = () => {
      try {
        assertSessionIdentity(identity);
        if (read.result) { done(validKey(read.result.key)); return; }
        values.put({ owner, key: generated }); done(generated);
      } catch { fail(); }
    };
  });
}

function associatedData(owner: string, target: string, field: string): Uint8Array<ArrayBuffer> {
  return new TextEncoder().encode(JSON.stringify(['replay-stream-connections', 1, owner, target, field]));
}

async function seal(key: CryptoKey, owner: string, target: string, field: string, value: string): Promise<Sealed> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const bytes = new TextEncoder().encode(value);
  try {
    return { iv: iv.buffer, ciphertext: await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: associatedData(owner, target, field) }, key, bytes) };
  } finally { bytes.fill(0); }
}

async function unseal(key: CryptoKey, owner: string, target: string, field: string, value: Sealed): Promise<string> {
  const bytes = new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: value.iv, additionalData: associatedData(owner, target, field) }, key, value.ciphertext));
  try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); } finally { bytes.fill(0); }
}

export async function createStreamConnectionStore(owner: StreamConnectionOwner, identity = sessionIdentity()): Promise<StreamConnectionStore> {
  assertSessionIdentity(identity);
  if (!owner || typeof owner.tenant_id !== 'string' || !owner.tenant_id || owner.tenant_id.length > 1024
      || typeof owner.subject !== 'string' || !owner.subject || owner.subject.length > 1024) {
    throw new Error('로그인 계정 정보를 확인한 뒤 연결을 저장하세요.');
  }
  if (!globalThis.crypto?.subtle) throw new Error(storageUnavailable);
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(['replay-connection-owner-v1', owner.tenant_id, owner.subject])));
  assertSessionIdentity(identity);
  const namespace = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
  const location = streamConnectionLocation();
  const guard = () => { assertSessionIdentity(identity); if (streamConnectionLocation() !== location) throw new Error('저장 위치가 변경되었습니다. 화면을 새로 열어주세요.'); };
  async function safely<T>(message: string, task: () => Promise<T>): Promise<T> {
    guard();
    try { const value = await task(); guard(); return value; }
    catch { guard(); throw new Error(message); }
  }
  async function local<T>(task: (db: IDBDatabase) => Promise<T>): Promise<T> {
    const db = await openDatabase();
    try { guard(); return await task(db); } finally { db.close(); }
  }
  const id = (target: string) => `${namespace}:${targetId(target)}`;
  return Object.freeze<StreamConnectionStore>({ namespace, identity, location,
    list: () => safely('저장한 연결 목록을 불러오지 못했습니다. 저장소 설정이나 서버 연결을 확인하세요.', async () => {
      if (location === 'account') {
        const result = await api<StreamConnectionMetadata[]>('/stream-connections', { cache: 'no-store', signal: AbortSignal.timeout(12_000) });
        return result.map(metadata);
      }
      return local(async db => {
        const records = await transaction<StoredConnection[]>(db, 'connections', 'readonly', identity, (values, done) => {
          const read = values.index('owner').getAll(namespace); read.onsuccess = () => done(read.result);
        });
        if (!records.length) return [];
        const key = await ownerKey(db, namespace, identity, false);
        if (!key) throw new Error(storageUnavailable);
        return Promise.all(records.map(async record => {
          if (record.owner !== namespace || record.id !== id(record.target)) throw new Error(storageUnavailable);
          // Metadata URLs are separate ciphertexts; stream keys stay sealed until
          // use(). Existing v1 rows have no channel field and remain readable.
          const [serverUrl, channelUrl] = await Promise.all([
            unseal(key, namespace, record.target, 'server', record.server),
            record.channel ? unseal(key, namespace, record.target, 'channel', record.channel) : '',
          ]);
          return metadata({ target: record.target, server_url: serverUrl, channel_url: channelUrl, updated_at: record.updated_at, has_stream_key: true });
        }));
      });
    }),
    save: (target, value) => {
      targetId(target); const current = { ...validateStreamConnection(value), channel_url: normalizeWatchUrl(target, value.channel_url ?? '', 'channel') };
      return safely('연결을 저장하지 못했습니다. 저장소 설정이나 서버 연결을 확인하세요.', async () => {
        if (location === 'account') {
          const result = await api<StreamConnectionMetadata>(`/stream-connections/${target}`, {
            method: 'PUT', cache: 'no-store', signal: AbortSignal.timeout(12_000), headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(current),
          });
          if (result.target !== target) throw new Error(invalidConnection);
          return metadata(result);
        }
        return local(async db => {
          const key = (await ownerKey(db, namespace, identity, true))!;
          const [server, secret, channel] = await Promise.all([
            seal(key, namespace, target, 'server', current.server_url), seal(key, namespace, target, 'key', current.stream_key),
            seal(key, namespace, target, 'channel', current.channel_url),
          ]);
          guard();
          const updatedAt = Date.now() / 1000;
          await transaction<void>(db, 'connections', 'readwrite', identity, values => {
            values.put({ id: id(target), owner: namespace, target, updated_at: updatedAt, server, secret, channel } satisfies StoredConnection);
          });
          return metadata({ target, server_url: current.server_url, channel_url: current.channel_url, updated_at: updatedAt, has_stream_key: true });
        });
      });
    },
    use: target => {
      targetId(target);
      return safely('저장한 연결을 불러오지 못했습니다. 최신 URL과 키를 입력해 다시 저장하세요.', async () => {
        if (location === 'account') {
          const value = await api<StreamConnection>(`/stream-connections/${target}/use`, { method: 'POST', cache: 'no-store', signal: AbortSignal.timeout(12_000) });
          if (value.target !== target) throw new Error(invalidConnection);
          return { target, ...validateStreamConnection(value), channel_url: normalizeWatchUrl(target, value.channel_url ?? '', 'channel') };
        }
        return local(async db => {
          const record = await transaction<StoredConnection | undefined>(db, 'connections', 'readonly', identity, (values, done) => {
            const read = values.get(id(target)); read.onsuccess = () => done(read.result);
          });
          const key = await ownerKey(db, namespace, identity, false);
          if (!record || record.owner !== namespace || record.target !== target || !key) throw new Error(storageUnavailable);
          const [server_url, stream_key, channelUrl] = await Promise.all([
            unseal(key, namespace, target, 'server', record.server), unseal(key, namespace, target, 'key', record.secret),
            record.channel ? unseal(key, namespace, target, 'channel', record.channel) : '',
          ]);
          return { target, ...validateStreamConnection({ server_url, stream_key }), channel_url: normalizeWatchUrl(target, channelUrl, 'channel') };
        });
      });
    },
    remove: target => {
      targetId(target);
      return safely('저장한 연결을 삭제하지 못했습니다. 저장소 설정이나 서버 연결을 확인하세요.', async () => {
        if (location === 'account') { await request(`/stream-connections/${target}`, { method: 'DELETE', cache: 'no-store', signal: AbortSignal.timeout(12_000) }); return; }
        await local(db => transaction<void>(db, 'connections', 'readwrite', identity, values => { values.delete(id(target)); }));
      });
    },
  });
}
