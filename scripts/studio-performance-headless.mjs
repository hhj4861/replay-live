// Synthetic API responses exercise the actual built UI in headless Chromium.
// Build web/dist-vercel with Google login enabled before running this script.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.REPLAY_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve('web/dist-vercel');
const origin = 'https://studio.example';
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const checks = [];
try {
  for (const failed of [false, true]) {
    const context = await browser.newContext();
    const page = await context.newPage();
    page.setDefaultTimeout(12000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const counts = {};
    let imported = false, released = false, historyReleased = false;
    let releaseCatalog, releaseMedia, releaseHistory;
    const catalogGate = new Promise(resolve => { releaseCatalog = resolve; });
    const mediaGate = new Promise(resolve => { releaseMedia = resolve; });
    const historyGate = new Promise(resolve => { releaseHistory = resolve; });
    const stored = Math.round(5.9 * 1024 ** 2), reservation = 50 * 1024 ** 2;
    const media = status => ({ id: 'synthetic-media', name: 'Synthetic recording', status, bytes: 1024,
      duration: 17, ...(status === 'failed' ? { error_code: 'SOURCE_DURATION_EXCEEDED' } : {}) });
    await context.addInitScript(({ origin }) => {
      if (location.origin === origin) sessionStorage.setItem('replay-google-session', JSON.stringify({
        token: 'synthetic_application_session_012345678901234',
        expires_at: Date.now() / 1000 + 600, absolute_expires_at: Date.now() / 1000 + 600,
      }));
    }, { origin });
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin !== origin) return route.abort();
      const key = url.pathname;
      const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      if (!key.startsWith('/api/')) {
        const file = path.resolve(root, key === '/' ? 'index.html' : key.slice(1));
        if (!file.startsWith(root + path.sep)) return route.abort();
        return route.fulfill({ body: await fs.readFile(file), contentType: file.endsWith('.js') ? 'application/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html' });
      }
      counts[key] = (counts[key] || 0) + 1;
      if (key === '/api/me') return json({ subject: 'synthetic', tenant_id: 'synthetic', roles: ['operator'], profile: null, manage_members: false });
      if (key === '/api/health' || key === '/api/limits') return json({ ready: true, max_upload_mb: 50, max_duration_seconds: 120, retention_days: 30, max_concurrent: 1 });
      if (key === '/api/stream-targets') { await catalogGate; return json({ targets: [], max_destinations: 1 }); }
      if (key === '/api/media-sources') return json({ sources: [{ id: 'youtube', label: 'YouTube', note: '' }] });
      if (key === '/api/stream-connections') return json([]);
      if (key === '/api/usage') return json({ storage_bytes: stored + (imported ? reservation : 0), storage_reserved_bytes: imported ? reservation : 0,
        storage_limit_bytes: 256 * 1024 ** 2, storage_available_bytes: 200 * 1024 ** 2, reserved_runtime_seconds_today: 0, runtime_limit_seconds_per_day: 3600 });
      if (key === '/api/media/imports') { imported = true; return json({ media: media('importing') }, 202); }
      if (key === '/api/media') { if (imported) await mediaGate; return json(imported ? [media(failed ? 'failed' : 'ready')] : []); }
      if (key === '/api/broadcasts') { if (imported) { await historyGate; historyReleased = true; } return json([]); }
      if (key.endsWith('/preview')) return json({ detail: 'Preview omitted from synthetic UI timing test' }, 404);
      if (key.endsWith('/output-estimate')) return json({ media_id: 'synthetic-media', estimated_output_bytes: 1024, max_output_bytes: reservation, storage_available_bytes: reservation, can_create: true, reason: null });
      throw new Error(`Unexpected synthetic API path: ${key}`);
    });
    try {
      await page.goto(origin);
      await page.locator('.connection').filter({ hasText: '스튜디오 연결됨' }).waitFor();
      await page.locator('.studio-storage strong').filter({ hasText: '5.9 MiB' }).waitFor();
      assert.equal(released, false);
      released = true; releaseCatalog();
      await page.waitForFunction(() => !document.querySelector('#source-url')?.disabled);
      await page.waitForResponse(r => new URL(r.url()).pathname === '/api/me' && counts['/api/me'] >= 2);
      assert.equal(counts['/api/stream-targets'], 1);
      await page.getByLabel('녹화 영상 링크', { exact: true }).fill('https://youtu.be/GcOe4ILS6Ow');
      await page.getByRole('button', { name: '영상 가져오기', exact: true }).click();
      await page.getByText('처리 중 임시 예약 50.0 MiB', { exact: true }).waitFor();
      assert.match(await page.locator('.studio-storage strong').innerText(), /^5\.9 MiB/);
      releaseMedia();
      if (failed) await page.getByText('가져올 수 있는 영상은 최대 2분 0초입니다. 이보다 짧은 영상을 선택하세요.', { exact: false }).first().waitFor();
      else await page.getByText('영상이 준비됐습니다. 방송할 채널을 선택해 주세요.', { exact: true }).waitFor();
      assert.equal(historyReleased, false);
      assert.deepEqual(errors, []);
      checks.push({ failed_import: failed, connected_and_usage_before_catalog: true, configuration_cached: true,
        stored_bytes_stable_during_reservation: true, media_visible_before_history: true });
    } finally { releaseCatalog(); releaseMedia(); releaseHistory(); await context.close(); }
  }
  console.log(JSON.stringify({ passed: true, headless: true, synthetic: true, checks }));
} finally { await browser.close(); }
