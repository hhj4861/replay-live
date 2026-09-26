import { createHash, timingSafeEqual } from 'node:crypto';
import { isIP } from 'node:net';
import { Sandbox, APIError, type NetworkPolicyRule } from '@vercel/sandbox';
import { importNetworkPolicy } from '../lib/worker-network-policy.js';
import { monitorProxyQuota } from '../lib/proxy-alerts.js';

// Only this short control function holds cloud credentials. Media workers receive one lease.
type Job = { id: string; lease_version: number; lease_seconds: number; deadline: number; callback_base: string; callback_token: string;
  input: { url: string } | null; target: string; version: string; mode: string; [key: string]: unknown };
type StreamDestination = { target: string; server_url: string; stream_key: string; hostname: string;
  port: number; protocol: string; addresses: string[] };
const liveTargets = new Set(['youtube', 'twitch', 'facebook', 'instagram', 'tiktok', 'naver', 'chzzk', 'kick', 'custom']);
function publicIPv4(value: unknown): value is string {
  if (typeof value !== 'string' || isIP(value) !== 4) return false;
  const [a, b, c] = value.split('.').map(Number);
  return !(a === 0 || a === 10 || a === 127 || a >= 224 || (a === 100 && b >= 64 && b <= 127)
    || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31)
    || (a === 192 && (b === 168 || (b === 0 && (c === 0 || c === 2)) || (b === 88 && c === 99)))
    || (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) || (a === 203 && b === 0 && c === 113));
}

function streamDestination(job: Job): StreamDestination | null {
  if (job.target === 'local' || job.target === 'validate' || job.target === 'import') return null;
  if (!liveTargets.has(job.target) || !job.stream_destination || typeof job.stream_destination !== 'object') throw new Error('Stream pin missing');
  const value = job.stream_destination as StreamDestination;
  const url = new URL(value.server_url);
  if (value.target !== job.target || !['rtmp:', 'rtmps:'].includes(url.protocol) || url.username || url.password || url.search || url.hash
      || value.hostname !== url.hostname || value.protocol !== url.protocol.slice(0, -1)
      || value.port !== Number(url.port || (url.protocol === 'rtmps:' ? 443 : 1935))
      || !(url.protocol === 'rtmp:' ? [1935] : [443, 1935]).includes(value.port)
      || !Array.isArray(value.addresses) || !value.addresses.length || value.addresses.length > 16 || !value.addresses.every(publicIPv4)
      || typeof value.stream_key !== 'string' || !value.stream_key.length || value.stream_key.length > 1024) throw new Error('Invalid stream pin');
  if (isIP(value.hostname) && !value.addresses.every(address => address === value.hostname)) throw new Error('Invalid literal stream pin');
  return value;
}

function required(name: string) {
  const value = process.env[name];
  if (!value) throw new Error(`Missing ${name}`);
  return value;
}

// Keep 30 seconds below the configured 300-second Function limit. Phase clocks
// are monotonic in production; the fallback also permits deterministic fixtures.
const now = () => typeof performance === 'object' ? performance.now() : Date.now();
const remaining = (deadline: number) => deadline - now();
function signalUntil(deadline: number, maximumMs: number) {
  const milliseconds = Math.floor(Math.min(remaining(deadline), maximumMs));
  if (milliseconds <= 0) throw new Error('Dispatcher phase budget exhausted');
  return AbortSignal.timeout(milliseconds);
}

async function run(request: Request) {
  // Queue callbacks need additional time for version checks, publishing the
  // next wakeup and the SDK acknowledgement within the same 300-second limit.
  const tickDeadline = now() + (process.env.REPLAY_DISPATCH_MODE === 'queue' ? 240_000 : 270_000);
  const expected = process.env.CRON_SECRET;
  const digest = (value: string) => createHash('sha256').update(value).digest();
  if (!expected || expected.length < 32 || !timingSafeEqual(digest(request.headers.get('authorization') || ''), digest(`Bearer ${expected}`))) {
    return Response.json({ detail: 'Unauthorized' }, { status: 401 });
  }
  if (!['GET', 'POST'].includes(request.method)) return new Response(null, { status: 405 });
  if (process.env.REPLAY_COMMERCIAL !== '1') return Response.json({ disabled: true });
  try {
    const base = new URL(required('REPLAY_CONTROL_URL'));
    if (base.protocol !== 'https:' || base.username || base.password || base.pathname !== '/' || base.search) throw new Error('Invalid control origin');
    const controlToken = required('REPLAY_CONTROL_TOKEN');
    const snapshotId = required('REPLAY_WORKER_SNAPSHOT_ID');
    const version = required('REPLAY_VERSION');
    async function control(path: string, body?: unknown, deadline = tickDeadline, maximumMs = 10_000) {
      const response = await fetch(`${base.origin}/internal/${path}`, {
        method: 'POST', headers: { Authorization: `Bearer ${controlToken}`, 'Content-Type': 'application/json' },
        body: JSON.stringify(body ?? {}), signal: signalUntil(deadline, maximumMs), redirect: 'error',
      });
      if (!response.ok) throw new Error('Control request failed');
      return response.json();
    }
    let stopped = 0;
    let cleanupFailed = false;
    const deferred = new Set<string>();
    const cleanupDeadline = Math.min(tickDeadline, now() + 40_000);
    try {
      const runtimeData = await control('runtimes', undefined, cleanupDeadline, 8_000) as { runtimes: { id: string; lease_version: number; state: string; deadline: number }[] };
      for (const runtime of runtimeData.runtimes.slice(0, 20)) {
        if (remaining(cleanupDeadline) <= 0) { deferred.add('cleanup'); break; }
        if (!['completed', 'failed', 'stopped', 'retry_wait'].includes(runtime.state) && runtime.deadline * 1000 > Date.now()) continue;
        const name = `replay-job-${runtime.id}-${runtime.lease_version}`;
        try {
          const sandbox = await Sandbox.get({ name, resume: false, signal: signalUntil(cleanupDeadline, 8_000) });
          await sandbox.stop({ signal: signalUntil(cleanupDeadline, 8_000) });
          await control('runtime-cleaned', { job_id: runtime.id, lease_version: runtime.lease_version }, cleanupDeadline, 5_000);
          stopped += 1;
        } catch (error) {
          if (error instanceof APIError && error.response.status === 404) {
            try {
              await control('runtime-cleaned', { job_id: runtime.id, lease_version: runtime.lease_version }, cleanupDeadline, 5_000);
            } catch { deferred.add('cleanup'); cleanupFailed = true; }
          } else {
            deferred.add('cleanup');
            cleanupFailed = true;
          }
          // Uncertain stop/ack remains durable; never resume or replace it.
        }
      }
    } catch { deferred.add('cleanup'); cleanupFailed = true; }
    try {
      await control('maintenance', undefined, Math.min(tickDeadline, now() + 30_000), 30_000);
    } catch { deferred.add('maintenance'); }
    let started = 0;
    let dispatchUnavailable = false;
    // Reserve the final 35 seconds for monitoring/delivery. Do not acquire a
    // new lease unless setup plus an uncertain-start stop can fit its budget.
    const dispatchDeadline = Math.min(tickDeadline - 35_000, now() + 160_000);
    for (let count = 0; count < 4 && remaining(dispatchDeadline) >= 100_000; count++) {
      const claimStartedAt = now();
      const attemptDeadline = Math.min(dispatchDeadline - 5_000, claimStartedAt + 90_000);
      let job: Job | null;
      try {
        ({ job } = await control('claim', { worker_id: 'vercel-cron', version }, attemptDeadline) as { job: Job | null });
      } catch {
        // A lost claim acknowledgement must never be replayed by this tick.
        dispatchUnavailable = true;
        break;
      }
      if (!job) break;
      if (job.version !== version || job.mode !== 'production') throw new Error('Worker release mismatch');
      // An immutable, secret-free snapshot contains the tested Python/FFmpeg release.
      // Each claimed attempt owns a distinct ephemeral VM and exposes no inbound ports.
      const timeout = Math.floor(Math.min(24 * 3600_000, Math.max(1_000, job.deadline * 1000 - Date.now())));
      let sandbox: Sandbox | undefined;
      let setupPhase: 'create' | 'verify' | 'pin' | 'write' | 'launch' = 'create';
      let phaseStartedAt = now();
      let verifyExitCode: number | undefined;
      try {
        if (!Number.isFinite(job.lease_seconds) || job.lease_seconds <= 0 || !Number.isFinite(job.deadline)) {
          throw new Error('Invalid worker startup budget');
        }
        // The lease starts during the claim request. Counting from before that
        // request is conservative, and reserves time for the worker's first beat.
        const setupDeadline = Math.min(attemptDeadline, claimStartedAt + (job.lease_seconds - 15) * 1000,
          now() + job.deadline * 1000 - Date.now() - 15_000);
        if (remaining(setupDeadline) <= 0) throw new Error('Worker startup budget exhausted');
        const allowed: Record<string, NetworkPolicyRule[]> = Object.fromEntries(
          [...new Set([base.hostname, ...(job.input ? [new URL(job.input.url).hostname] : [])])].map(host => [host, [{ transform: [{ headers: { Host: host } }] }]])
        );
        // Blob's signed output PUT uses its control-plane hostname. Never
        // derive extra worker egress destinations from browser/job input.
        if (process.env.REPLAY_STORAGE_PROVIDER === 'vercel-blob') {
          allowed['vercel.com'] = [{ transform: [{ headers: { Host: 'vercel.com' } }] }];
        }
        const destination = streamDestination(job);
        // Official Sandbox CIDR rules support raw TCP. No domain rule is added
        // for a user-controlled stream host: only its claim-time public /32s.
        // CIDRs do not restrict ports; the API/worker validate RTMP(S) ports.
        // Import CIDRs permit only the downloader's public IPv4 address set.
        // Omitting domain rules leaves DNS available for recording/CDN redirects.
        // Live raw TCP is restricted to the API's validated public IPv4 pins.
        // Default-deny covers every other raw destination without provider deny rules.
        const networkPolicy = job.target === 'import' ? importNetworkPolicy() : destination ? {
          allow: allowed, subnets: { allow: [...new Set(destination.addresses)].map(address => `${address}/32`) },
        } : { allow: allowed };
        sandbox = await Sandbox.create({ name: `replay-job-${job.id}-${job.lease_version}`,
          source: { type: 'snapshot', snapshotId }, timeout, persistent: false,
          resources: { vcpus: 2 }, ports: [], networkPolicy,
          tags: { app: 'replay-live', release: version.slice(0, 40) }, signal: signalUntil(setupDeadline, 30_000) });
        setupPhase = 'verify'; phaseStartedAt = now();
        const verified = await sandbox.runCommand({ cmd: '.venv/bin/python', cwd: '/vercel/sandbox/replay', args: ['-c', 'import json,sys; assert json.load(open("release.json"))["version"] == sys.argv[1]', version], timeoutMs: 15_000, signal: signalUntil(setupDeadline, 20_000) });
        if (Number.isSafeInteger(verified.exitCode)) verifyExitCode = verified.exitCode;
        if (verified.exitCode !== 0) throw new Error('Snapshot release mismatch');
        if (destination) {
          setupPhase = 'pin'; phaseStartedAt = now();
          // This runs only inside the new VM, before credentials are written.
          // Keeping the original hostname preserves TLS SNI/certificate checks.
          const pinned = await sandbox.runCommand({ cmd: '.venv/bin/python', cwd: '/vercel/sandbox/replay', sudo: true,
            args: ['-c', 'import json,sys; from server.stream_targets import install_host_pin; p=json.loads(sys.argv[1]); install_host_pin(p["hostname"],p["addresses"])',
              JSON.stringify({ hostname: destination.hostname, addresses: destination.addresses })],
            timeoutMs: 5_000, signal: signalUntil(setupDeadline, 8_000) });
          if (pinned.exitCode !== 0) throw new Error('Stream pin setup failed');
        }
        setupPhase = 'write'; phaseStartedAt = now();
        await sandbox.writeFiles([{ path: '/vercel/sandbox/replay/job.json', content: Buffer.from(JSON.stringify(job)) }], { signal: signalUntil(setupDeadline, 8_000) });
        setupPhase = 'launch'; phaseStartedAt = now();
        const source = job.source as { provider?: string } | undefined;
        const proxy = job.target === 'import' && source?.provider === 'youtube'
          ? required('REPLAY_SOURCE_PROXY_URL') : undefined;
        await sandbox.runCommand({ cmd: 'bash', args: ['-c', 'set -euo pipefail\ncd /vercel/sandbox/replay\nchmod 600 job.json\nexec .venv/bin/python -m server.worker job.json'],
          env: proxy ? { REPLAY_SOURCE_PROXY_URL: proxy } : {}, detached: true, signal: signalUntil(setupDeadline, 8_000) });
        started += 1;
      } catch (error) {
        // Only fixed categories and correlation fields may leave this boundary.
        // SDK messages, causes and stacks can contain signed URLs or credentials.
        const apiStatus = error instanceof APIError ? error.response.status : undefined;
        const errorCategory = error instanceof APIError ? 'APIError'
          : error && typeof error === 'object' && 'name' in error && error.name === 'AbortError' ? 'AbortError'
            : error && typeof error === 'object' && 'name' in error && error.name === 'TimeoutError' ? 'TimeoutError' : 'Error';
        const phaseElapsedMs = Math.floor(now() - phaseStartedAt);
        console.error(JSON.stringify({ event: 'worker_setup_failed', version, job_id: job.id,
          lease_version: job.lease_version, phase: setupPhase, error_category: errorCategory,
          ...(Number.isSafeInteger(phaseElapsedMs) && phaseElapsedMs >= 0 ? { phase_elapsed_ms: phaseElapsedMs } : {}),
          ...(verifyExitCode !== undefined ? { verify_exit_code: verifyExitCode } : {}),
          ...(typeof apiStatus === 'number' && Number.isInteger(apiStatus) && apiStatus >= 100 && apiStatus <= 599
            ? { http_status: apiStatus } : {}) }));
        // Never reclaim/start this lease again: creation/command acknowledgement could be lost.
        // Kill an acquired VM if possible; database expiry records uncertain work as failed.
        deferred.add('worker_setup');
        if (sandbox) {
          try { await sandbox.stop({ signal: signalUntil(Math.min(dispatchDeadline, now() + 5_000), 5_000) }); }
          catch { /* Runtime reconciliation handles an uncertain stop on the next tick. */ }
        }
      } finally {
        job.callback_token = '';
        job.stream_key = '';
        if (job.source && typeof job.source === 'object') {
          (job.source as Record<string, unknown>).url = '';
        }
        if (job.stream_destination && typeof job.stream_destination === 'object') {
          (job.stream_destination as StreamDestination).stream_key = '';
          (job.stream_destination as StreamDestination).server_url = '';
        }
      }
    }
    try {
      const monitoring = await control('monitor', undefined, Math.min(tickDeadline, now() + 10_000)) as { alerts_pending: number };
      const webhook = process.env.REPLAY_ALERT_WEBHOOK_URL;
      if (webhook && monitoring.alerts_pending) {
        const alertDeadline = Math.min(tickDeadline, now() + 25_000);
        const destination = new URL(webhook);
        if (destination.protocol !== 'https:' || destination.username || destination.password || destination.hash) throw new Error('Invalid alert destination');
        const { alerts } = await control('alerts', undefined, alertDeadline, 5_000) as { alerts: { id: string; code: string; count: number; created: number }[] };
        for (const alert of alerts.slice(0, 10)) {
          if (remaining(alertDeadline) <= 0) { deferred.add('alerts'); break; }
          try {
            const response = await fetch(destination, { method: 'POST', redirect: 'error', signal: signalUntil(alertDeadline, 5_000),
              headers: { 'Content-Type': 'application/json', 'Idempotency-Key': alert.id,
                ...(process.env.REPLAY_ALERT_WEBHOOK_TOKEN ? { Authorization: `Bearer ${process.env.REPLAY_ALERT_WEBHOOK_TOKEN}` } : {}) },
              body: JSON.stringify({ service: 'replay-live', ...alert }) });
            if (response.ok) await control(`alerts/${alert.id}/ack`, undefined, alertDeadline, 5_000);
            else deferred.add('alerts');
          } catch { deferred.add('alerts'); }
          // Outbox retains delivery/ack failures; no credentials or bodies logged.
        }
      }
    } catch { deferred.add('monitoring'); }
    if (process.env.REPLAY_PROXY_MONITOR_ENABLED === '1') {
      try {
        const deadline = Math.min(tickDeadline, now() + 25_000);
        await monitorProxyQuota((path, body) => control(path, body, deadline, 4000), process.env);
      } catch { deferred.add('proxy-alerts'); }
    }
    console.info(JSON.stringify({ event: 'dispatch_tick', version, started, stopped, deferred: [...deferred], dispatch_unavailable: dispatchUnavailable }));
    // A persistent stop failure must retry this bounded queue message, rather
    // than publish a fresh message indefinitely and evade maxDeliveries.
    const retry = (dispatchUnavailable && !started) || (cleanupFailed && process.env.REPLAY_DISPATCH_MODE === 'queue');
    return Response.json({ started, stopped, version }, { status: retry ? 503 : 200, headers: { 'Cache-Control': 'no-store' } });
  } catch {
    console.error(JSON.stringify({ event: 'dispatch_failed', code: 'CONTROL_UNAVAILABLE' }));
    return Response.json({ detail: 'Dispatcher unavailable' }, { status: 503 });
  }
}

const dispatcherHandler = { fetch: run };
export default dispatcherHandler;
