// Actual Chromium -> multipart HTTP -> local R2 binding. Account/catalog API
// responses are synthetic; production credentials and external downloads are absent.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import http from 'node:http';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const root = process.cwd();
const { chromium } = require(process.env.REPLAY_PLAYWRIGHT_MODULE || 'playwright');
const { Miniflare, convertV4MiniflareOptions } = require(path.join(root, 'deploy/cloudflare/node_modules/miniflare'));
const { build } = require(path.join(root, 'deploy/cloudflare/node_modules/esbuild'));
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'replay-large-e2e-'));
let browser, runtime, webServer;
try {
  const clip = path.join(temporary, 'long-large.mp4');
  execFileSync('ffmpeg', ['-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=1', '-f', 'lavfi',
    '-i', 'anullsrc=r=8000:cl=mono', '-t', '2199', '-c:v', 'libx264', '-preset', 'ultrafast',
    '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', clip]);
  const original = await fs.stat(clip);
  const padding = Buffer.alloc(65 * 1024 ** 2 - original.size);
  padding.writeUInt32BE(padding.length, 0); padding.write('free', 4, 'ascii');
  await fs.appendFile(clip, padding); // A legal MP4 free box, not corrupt trailing bytes.
  execFileSync('ffmpeg', ['-v', 'error', '-xerror', '-i', clip, '-f', 'null', '-']);
  const bytes = (await fs.stat(clip)).size;
  const digest = createHash('sha256').update(await fs.readFile(clip)).digest('hex');
  const probe = net.createServer(); await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port; await new Promise(resolve => probe.close(resolve));
  const origin = `http://127.0.0.1:${port}`;
  webServer = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://localhost');
      const base = path.join(root, 'web/dist-vercel');
      const file = path.resolve(base, url.pathname === '/' ? 'index.html' : url.pathname.slice(1));
      if (!file.startsWith(base + path.sep)) throw Error('Invalid fixture path');
      response.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html');
      response.end(await fs.readFile(file));
    } catch { response.statusCode = 404; response.end(); }
  });
  await new Promise(resolve => webServer.listen(0, '127.0.0.1', resolve));
  const web = `http://127.0.0.1:${webServer.address().port}`;
  const token = 'synthetic-control-'.repeat(4), tenant = 'large-e2e';
  const key = `replay/${createHash('sha256').update(tenant).digest('hex')}/media/large.mp4`;
  const bundled = await build({ entryPoints: [path.join(root, 'deploy/cloudflare/tests/storage-worker.ts')], bundle: true, write: false, format: 'esm', target: 'es2022' });
  runtime = new Miniflare(convertV4MiniflareOptions({ name: 'large-e2e', modules: true, host: '127.0.0.1', port, rootPath: temporary,
    script: bundled.outputFiles[0].text, compatibilityDate: '2026-08-18', r2Buckets: ['MEDIA'], bindings: {
      REPLAY_CONTROL_TOKEN: token, REPLAY_OBJECT_KEY: 'synthetic-object-'.repeat(4), REPLAY_PUBLIC_URL: origin, REPLAY_ORIGINS: web,
    } }));
  assert.equal((await runtime.ready).origin, origin);
  const control = async (operation, extra = {}) => {
    const response = await runtime.dispatchFetch(origin + '/api/blob-control', { method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation, tenant_id: tenant, object_key: key, ...extra }) });
    assert.equal(response.status, 200, await response.clone().text()); return response.json();
  };
  browser = await chromium.launch({ channel: 'chrome', headless: true });
  const context = await browser.newContext(); const page = await context.newPage(); page.setDefaultTimeout(60000);
  let ready = false, pending = false, multipartPuts = 0; const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text().replace(/https?:\/\/\S+/g, '[test-url]')); });
  page.on('requestfailed', request => errors.push(`${new URL(request.url()).pathname}: ${request.failure()?.errorText}`));
  page.on('request', request => { if (new URL(request.url()).origin === origin && request.method() === 'PUT') multipartPuts++; });
  await context.addInitScript(({ web }) => {
    if (location.origin === web) sessionStorage.setItem('replay-google-session', JSON.stringify({ token: 'synthetic_application_session_012345678901234',
      expires_at: Date.now() / 1000 + 600, absolute_expires_at: Date.now() / 1000 + 600 }));
  }, { web });
  const media = status => ({ id: 'large-media', name: 'long-large.mp4', status, duration: 2199, bytes });
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin === origin) return route.continue();
    if (url.origin !== web) return route.abort();
    const key = url.pathname;
    const json = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (!key.startsWith('/api/')) return route.continue();
    if (key === '/api/me') return json({ subject: 'synthetic', tenant_id: tenant, roles: ['operator'] });
    if (key === '/api/health' || key === '/api/limits') return json({ ready: true, max_upload_mb: 0, max_duration_seconds: 0, max_concurrent: 1, retention_days: 30 });
    if (key === '/api/stream-targets') return json({ targets: [], max_destinations: 1 });
    if (key === '/api/media-sources') return json({ sources: [{ id: 'youtube', label: 'YouTube', note: '' }] });
    if (key === '/api/usage') return json({ storage_bytes: pending || ready ? bytes : 0, storage_reserved_bytes: pending ? bytes : 0,
      storage_available_bytes: 256 * 1024 ** 2 - (pending || ready ? bytes : 0), storage_limit_bytes: 256 * 1024 ** 2 });
    if (key === '/api/media') return json(ready ? [media('ready')] : []);
    if (key === '/api/broadcasts' || key === '/api/stream-connections') return json([]);
    if (key === '/api/uploads') {
      const payload = route.request().postDataJSON(); assert.equal(payload.bytes, bytes); assert.equal(payload.sha256, digest);
      pending = true; return json({ media: media('uploading'), upload: await control('upload', { size: bytes, sha256: digest }) }, 201);
    }
    if (key === '/api/uploads/large-media/complete') {
      await control('verify', { size: bytes, sha256: digest }); pending = false; ready = true; return json(media('ready'), 202);
    }
    if (key === '/api/media/large-media' && route.request().method() === 'DELETE') return json({ deleted: true });
    if (key.endsWith('/preview')) return json(await control('download'));
    if (key.endsWith('/output-estimate')) return json({ media_id: 'large-media', estimated_output_bytes: 1, storage_available_bytes: 1, can_create: true });
    errors.push(`Unexpected synthetic API route ${key}`); return json({ detail: 'Unexpected test request' }, 500);
  });
  await page.goto(web); await page.locator('.connection').filter({ hasText: '스튜디오 연결됨' }).waitFor();
  await page.getByText('보관함의 남은 공간까지', { exact: true }).waitFor();
  assert.equal(await page.getByText('최대 2분', { exact: false }).count(), 0);
  await page.locator('input[type=file]').setInputFiles(clip);
  await Promise.race([
    page.getByRole('button', { name: 'long-large.mp4 선택', exact: true }).waitFor(),
    page.locator('[role=alert]').waitFor().then(async () => { throw Error((await page.locator('[role=alert]').allTextContents()).join(' ') + ' ' + JSON.stringify(errors)); }),
  ]);
  assert.equal(multipartPuts, 3); assert.equal(ready, true);
  await page.waitForFunction(() => document.querySelector('video')?.readyState >= 2);
  const playback = await page.locator('video').evaluate(async video => {
    video.muted = true; video.currentTime = video.duration - 2; await video.play();
    await new Promise((resolve, reject) => { video.onended = resolve; video.onerror = reject; });
    return { ended: video.ended, duration: video.duration, width: video.videoWidth, error: video.error?.code || null };
  });
  assert.equal(playback.duration, 2199); assert.equal(playback.ended, true); assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, headless: true, synthetic_account_api: true, actual_local_r2: true,
    bytes, duration: 2199, multipart_puts: multipartPuts, checksum_verified: true, playback }));
} finally {
  await browser?.close(); await runtime?.dispose();
  if (webServer) await new Promise(resolve => webServer.close(resolve));
  await fs.rm(temporary, { recursive: true, force: true });
}
