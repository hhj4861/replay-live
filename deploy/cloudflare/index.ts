import { Container, getContainer } from '@cloudflare/containers';
import { DurableObject } from 'cloudflare:workers';
import { authorized, storageControl, storageObject, type StorageEnv } from './storage';

interface Env extends StorageEnv {
  API: DurableObjectNamespace<ApiContainer>;
  MEDIA_JOBS: DurableObjectNamespace<MediaContainer>;
  DISPATCH: DurableObjectNamespace<Dispatcher>;
  API_CONFIG: string;
  REPLAY_VERSION: string;
}
type Job = { id: string; lease_version: number; version: string; mode: string; deadline: number; target: string; [key: string]: unknown };
const response = (body: unknown, status = 200) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });
const mediaName = (id: string, version: number) => `job-${id}-${version}`;
const api = (env: Env) => getContainer(env.API, 'control');
const dispatcher = (env: Env) => env.DISPATCH.get(env.DISPATCH.idFromName('dispatch'));

export class ApiContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = '2m';
  override envVars: Record<string, string>;
  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    const config: unknown = JSON.parse(env.API_CONFIG);
    if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error('INVALID_API_CONFIG');
    this.envVars = {};
    for (const [key, value] of Object.entries(config)) {
      if (!/^REPLAY_[A-Z0-9_]+$/.test(key) || typeof value !== 'string') throw new Error('INVALID_API_CONFIG');
      this.envVars[key] = value;
    }
    Object.assign(this.envVars, { REPLAY_MODE: 'production', REPLAY_VERSION: env.REPLAY_VERSION,
      REPLAY_PUBLIC_URL: env.REPLAY_PUBLIC_URL, REPLAY_ORIGINS: env.REPLAY_ORIGINS,
      REPLAY_CONTROL_TOKEN: env.REPLAY_CONTROL_TOKEN, REPLAY_STORAGE_PROVIDER: 'cloudflare-r2',
      REPLAY_SECRET_PROVIDER: 'env-aesgcm', REPLAY_AWS_AUTH_MODE: 'standard',
      REPLAY_BLOB_CONTROL_URL: `${env.REPLAY_PUBLIC_URL}/api/blob-control`,
      REPLAY_DISPATCH_MODE: 'cloudflare', REPLAY_DISPATCH_WAKEUP_URL: `${env.REPLAY_PUBLIC_URL}/api/wake`,
      REPLAY_MAX_UPLOAD_BYTES: String(50 * 1024 ** 2), REPLAY_MAX_OUTPUT_BYTES: String(64 * 1024 ** 2) });
  }
  override onError(): void { throw new Error('API_CONTAINER_UNAVAILABLE'); }
}

export class MediaContainer extends Container<Env> {
  defaultPort = 8080;
  // A claimed job has a <=20 minute deadline. No account, DB or R2 secrets.
  sleepAfter = '21m';
  override envVars = { REPLAY_VERSION: this.env.REPLAY_VERSION };
  async run(job: Job) {
    if (!/^[a-f0-9]{32}$/.test(job.id) || !Number.isSafeInteger(job.lease_version)
        || job.lease_version < 1 || job.version !== this.env.REPLAY_VERSION || job.mode !== 'production'
        || job.deadline <= Date.now() / 1000 || job.deadline > Date.now() / 1000 + 1200
        || job.target === 'import') throw new Error('INVALID_MEDIA_JOB');
    const first = await this.ctx.blockConcurrencyWhile(async () => {
      if (await this.ctx.storage.get('started')) return false;
      // Persist only a tombstone. Never persist job URLs, stream keys or tokens.
      await this.ctx.storage.put('started', true);
      return true;
    });
    if (!first) return;
    await this.startAndWaitForPorts();
    const result = await this.containerFetch('http://container/run', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(job) });
    if (result.status !== 202) throw new Error('MEDIA_START_FAILED');
  }
  async retire() {
    const state = await this.getState();
    if (state.status === 'running' || state.status === 'healthy' || state.status === 'stopping') await this.stop();
  }
  override onError(): void { throw new Error('MEDIA_CONTAINER_UNAVAILABLE'); }
}

export class Dispatcher extends DurableObject<Env> {
  async kick() {
    await this.ctx.blockConcurrencyWhile(async () => {
      const version = (await this.ctx.storage.get<number>('wakeVersion') ?? 0) + 1;
      await this.ctx.storage.put('wakeVersion', version);
      const pending = await this.ctx.storage.getAlarm();
      if (pending === null || pending > Date.now() + 1000) await this.ctx.storage.setAlarm(Date.now() + 1000);
    });
  }
  private async control<T>(path: string, body: unknown = {}): Promise<T> {
    const result = await api(this.env).fetch(new Request(`${this.env.REPLAY_PUBLIC_URL}/internal/${path}`, {
      method: 'POST', headers: { Authorization: `Bearer ${this.env.REPLAY_CONTROL_TOKEN}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body), signal: AbortSignal.timeout(60_000),
    }));
    if (!result.ok) throw new Error('CONTROL_UNAVAILABLE');
    return result.json() as Promise<T>;
  }
  async alarm() {
    const version = await this.ctx.storage.get<number>('wakeVersion') ?? 0;
    // Recovery is scheduled before any external await, including cold starts.
    await this.ctx.storage.setAlarm(Date.now() + 30_000);
    try {
      const { runtimes } = await this.control<{ runtimes: { id: string; lease_version: number; state: string; deadline: number }[] }>('runtimes');
      for (const runtime of runtimes.slice(0, 20)) {
        if (!['completed', 'failed', 'stopped', 'retry_wait'].includes(runtime.state) && runtime.deadline > Date.now() / 1000) continue;
        await getContainer(this.env.MEDIA_JOBS, mediaName(runtime.id, runtime.lease_version)).retire();
        await this.control('runtime-cleaned', { job_id: runtime.id, lease_version: runtime.lease_version });
      }
      await this.control('maintenance');
      for (let i = 0; i < 4; i++) {
        const { job } = await this.control<{ job: Job | null }>('claim', { worker_id: 'cloudflare-dispatch', version: this.env.REPLAY_VERSION });
        if (!job) break;
        await getContainer(this.env.MEDIA_JOBS, mediaName(job.id, job.lease_version)).run(job);
      }
      await this.control('monitor');
      const { at } = await this.control<{ at: number | null }>('next-wakeup');
      // Daily retention cleanup also runs while there is no browser open.
      const next = at === null ? Date.now() + 24 * 3600_000 : Math.max(Date.now() + 15_000, at * 1000);
      await this.ctx.blockConcurrencyWhile(async () => {
        const current = await this.ctx.storage.get<number>('wakeVersion') ?? 0;
        await this.ctx.storage.setAlarm(current !== version ? Date.now() + 1000 : Math.min(next, Date.now() + 24 * 3600_000));
      });
    } catch {
      console.error(JSON.stringify({ event: 'dispatch_retry', code: 'CONTROL_UNAVAILABLE' }));
      // The previously committed alarm retries. DB leases prevent duplication.
    }
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const path = new URL(request.url).pathname;
    if (path.startsWith('/objects/')) return storageObject(request, env);
    if (path === '/api/blob-control') return storageControl(request, env);
    if (path === '/api/wake') {
      if (request.method !== 'POST' || !await authorized(request, env.REPLAY_CONTROL_TOKEN)) return response({ code: 'UNAUTHORIZED' }, 401);
      await dispatcher(env).kick();
      return response({ queued: true }, 202);
    }
    if (!path.startsWith('/api/') && !/^\/internal\/jobs\/[a-f0-9]{32}\/(heartbeat|output|finish)$/.test(path)) return response({ code: 'NOT_FOUND' }, 404);
    try { return await api(env).fetch(request); }
    catch { return response({ detail: '서버를 준비하지 못했습니다. 잠시 후 다시 시도하세요.', code: 'API_UNAVAILABLE' }, 503); }
  },
  async scheduled(_event: ScheduledController, env: Env) { await dispatcher(env).kick(); },
} satisfies ExportedHandler<Env>;
