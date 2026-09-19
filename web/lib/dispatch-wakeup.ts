import { send } from '@vercel/queue';

const topic = 'replay-dispatch';
const region = 'iad1';
const retentionSeconds = 7 * 24 * 3600;
const maximumDelaySeconds = 5 * 24 * 3600;
const unavailable = () => new Error('Dispatch wakeup unavailable');

export function dispatchQueueEnabled() {
  return process.env.REPLAY_COMMERCIAL === '1' && process.env.REPLAY_DISPATCH_MODE === 'queue';
}

export function isDispatchWakeup(value: unknown): value is { v: 1 } {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === 1 && Object.hasOwn(value, 'v') && (value as { v?: unknown }).v === 1;
}

export async function publishDispatchWakeup(nextAt?: number): Promise<void> {
  if (!dispatchQueueEnabled()) throw unavailable();
  let schedule: { delaySeconds: number; idempotencyKey: string } | undefined;
  if (nextAt !== undefined) {
    const version = process.env.REPLAY_VERSION;
    if (typeof nextAt !== 'number' || !Number.isFinite(nextAt) || nextAt < 0 || nextAt > Number.MAX_SAFE_INTEGER / 1000
        || !releaseVersion(version)) throw unavailable();
    const nowSeconds = Date.now() / 1000;
    const targetSeconds = Math.min(
      Math.ceil(Math.max(nextAt, nowSeconds + 30) / 30) * 30,
      Math.floor((nowSeconds + maximumDelaySeconds) / 30) * 30,
    );
    schedule = { delaySeconds: Math.ceil(targetSeconds - nowSeconds), idempotencyKey: `wake-${version}-${targetSeconds}` };
  }
  const deadline = AbortSignal.timeout(8000);
  let onTimeout: (() => void) | undefined;
  try {
    // A wakeup contains no job, tenant, credentials, or media. Immediate API/cron
    // notifications have no deduplication key. Only coalesce future followups:
    // their target remains at least 30 seconds ahead of this consumer, so a
    // consumed signal's key cannot suppress the next one in its chain.
    // SDK 0.5.1 cannot abort send. Bound the caller's wait; a late acceptance can
    // create a duplicate on retry, which the existing database claims tolerate.
    await Promise.race([
      send(topic, { v: 1 }, { region, retentionSeconds, ...schedule }),
      new Promise<never>((_, reject) => {
        onTimeout = () => reject(unavailable());
        deadline.addEventListener('abort', onTimeout, { once: true });
        if (deadline.aborted) onTimeout();
      }),
    ]);
  } catch { throw unavailable(); }
  finally { if (onTimeout) deadline.removeEventListener('abort', onTimeout); }
}

function configuration() {
  const base = new URL(process.env.REPLAY_CONTROL_URL || '');
  const version = process.env.REPLAY_VERSION;
  const controlToken = process.env.REPLAY_CONTROL_TOKEN;
  const cronSecret = process.env.CRON_SECRET;
  if (base.protocol !== 'https:' || base.username || base.password || base.pathname !== '/' || base.search || base.hash
      || !releaseVersion(version)
      || !controlToken || controlToken.length < 32 || !cronSecret || cronSecret.length < 32) throw unavailable();
  return { base: base.origin, version, controlToken, cronSecret };
}

function releaseVersion(value: unknown): value is string {
  if (typeof value !== 'string' || !value.length || value.length > 200) return false;
  for (const character of value) {
    if (character.charCodeAt(0) <= 32 || character.charCodeAt(0) === 127) return false;
  }
  return true;
}

export async function consumeDispatchWakeup(message: unknown, dispatch: (request: Request) => Promise<Response>): Promise<void> {
  // Retire already-published messages when the feature is explicitly disabled.
  if (!dispatchQueueEnabled()) return;
  try {
    if (!isDispatchWakeup(message)) throw unavailable();
    const { base, version, controlToken, cronSecret } = configuration();
    const live = await fetch(`${base}/api/live`, {
      method: 'GET', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(8000),
    });
    if (!live.ok) throw unavailable();
    const health: unknown = await live.json();
    if (!health || typeof health !== 'object' || Array.isArray(health)
        || !releaseVersion((health as { version?: unknown }).version)) throw unavailable();
    // Vercel pins queued messages to their publishing deployment. An old
    // deployment must acknowledge its chain without claiming or scheduling work.
    if ((health as { version: string }).version !== version) return;

    const response = await dispatch(new Request('https://internal.invalid/api/dispatch', {
      method: 'GET', headers: { Authorization: `Bearer ${cronSecret}` },
    }));
    if (!response.ok) throw unavailable();
    const upcoming = await fetch(`${base}/internal/next-wakeup`, {
      method: 'POST', headers: { Authorization: `Bearer ${controlToken}`, 'Content-Type': 'application/json' },
      body: '{}', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(8000),
    });
    if (!upcoming.ok) throw unavailable();
    const next: unknown = await upcoming.json();
    if (!next || typeof next !== 'object' || Array.isArray(next) || !Object.hasOwn(next, 'at')) throw unavailable();
    const at = (next as { at: unknown }).at;
    if (at === null) return;
    if (typeof at !== 'number' || !Number.isFinite(at) || at < 0 || at > Number.MAX_SAFE_INTEGER / 1000) throw unavailable();
    await publishDispatchWakeup(at);
  } catch {
    // Callback errors are logged by the SDK. Never retain upstream error bodies,
    // fetch URLs, response objects, or secret-bearing causes in the thrown error.
    throw unavailable();
  }
}
