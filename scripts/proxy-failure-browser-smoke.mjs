// Exercise the actual commercial build with local synthetic auth/API fixtures.
// Usage: REPLAY_PLAYWRIGHT_MODULE=/path/to/playwright node scripts/proxy-failure-browser-smoke.mjs /path/to/build
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.REPLAY_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(process.argv[2]);
const server = http.createServer(async (req, res) => {
  try {
    const relative = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    const filename = path.resolve(root, '.' + (relative === '/' ? '/index.html' : relative));
    if (!filename.startsWith(root + path.sep)) { res.writeHead(403); res.end(); return; }
    const content = await fs.readFile(filename);
    res.setHeader('Content-Type', filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(content);
  } catch { res.writeHead(404); res.end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let browser;
const evidence = [];
try {
  browser = await chromium.launch({ headless: true,
    ...(process.env.REPLAY_CHROME_PATH ? { executablePath: process.env.REPLAY_CHROME_PATH } : {}) });
  const cases = [
    ['SOURCE_PROXY_QUOTA_EXHAUSTED', false, '사용량이 소진'],
    ['SOURCE_PROXY_CONFIGURATION', false, '설정을 확인'],
    ['SOURCE_PROXY_ACCESS_DENIED', false, '접근을 제한'],
    ['SOURCE_BOT_CHECK_REQUIRED', false, '봇 확인'],
    ['SOURCE_PROXY_CONNECT_FAILED', true, '영상 서버에 연결하지'],
    ['SOURCE_PROXY_POOL_EMPTY', true, '사용할 수 있는 다운로드 연결'],
    ['SOURCE_PROXY_BUSY', true, '요청이 몰리고'],
    ['SOURCE_PROXY_UNAVAILABLE', true, '연결을 사용할 수 없습니다'],
  ];
  for (const [code, retry, expected] of cases) {
    const context = await browser.newContext();
    try {
      await context.addInitScript(() => {
        // Synthetic local fixture only; never load production cookies/tokens.
        sessionStorage.setItem('oidc.user:https://synthetic.example:synthetic-client', JSON.stringify({
          access_token: 'synthetic-access-token', token_type: 'Bearer', scope: 'openid profile',
          profile: { sub: 'synthetic-user' }, expires_at: Math.floor(Date.now() / 1000) + 3600,
        }));
      });
      let imported = false, requests = 0, pageErrors = 0;
      const item = { id: 'synthetic-media', name: '테스트 영상', status: 'failed', bytes: 0, duration: 0, error_code: code };
      const limits = { ready: true, max_upload_mb: 0, max_duration_seconds: 0, retention_days: 30, max_concurrent: 1 };
      await context.route('**/*', async route => {
        const u = new URL(route.request().url());
        if (u.origin === base) return route.continue();
        if (u.origin !== 'http://127.0.0.1:18193') return route.abort('blockedbyclient');
        let result;
        if (u.pathname === '/api/media/imports') { imported = true; requests++; result = { media: item }; }
        else if (u.pathname === '/api/me') result = { tenant_id: 'synthetic', subject: 'synthetic-user', roles: ['operator'] };
        else if (u.pathname === '/api/media') result = imported ? [item] : [];
        else if (u.pathname === '/api/health' || u.pathname === '/api/limits') result = limits;
        else if (u.pathname === '/api/usage') result = { storage_bytes: 0, storage_reserved_bytes: 0, storage_available_bytes: 268435456, storage_limit_bytes: 268435456, reserved_runtime_seconds_today: 0, runtime_limit_seconds_per_day: 3600 };
        else if (u.pathname === '/api/stream-targets') result = { targets: [], max_destinations: 1 };
        else if (u.pathname === '/api/media-sources') result = { sources: [{ id: 'youtube', label: 'YouTube', note: '' }] };
        else result = [];
        return route.fulfill({ status: u.pathname === '/api/media/imports' ? 202 : 200,
          contentType: 'application/json', headers: { 'Access-Control-Allow-Origin': base }, body: JSON.stringify(result) });
      });
      const page = await context.newPage();
      page.on('pageerror', () => { pageErrors++; });
      await page.goto(base);
      await page.getByLabel('녹화 영상 링크', { exact: true }).fill('https://youtu.be/SzrcusiORCI');
      await page.getByRole('button', { name: '영상 가져오기', exact: true }).click();
      const alert = page.getByRole('alert').filter({ hasText: '영상을 준비하지 못했습니다.' });
      await alert.waitFor();
      assert.ok((await alert.innerText()).includes(expected), code);
      assert.equal(await alert.getByRole('button', { name: '현재 링크로 다시 가져오기' }).count(), Number(retry), code);
      assert.equal(await alert.getByRole('button', { name: '파일로 업로드', exact: true }).isVisible(), true);
      assert.equal(requests, 1); assert.equal(pageErrors, 0);
      evidence.push({ code, retry_visible: retry, upload_visible: true, import_requests: requests, javascript_errors: pageErrors });
    } finally { await context.close(); }
  }
  console.log(JSON.stringify({ passed: true, headless: true, synthetic_api: true, cases: evidence }));
} finally {
  if (browser) await browser.close();
  await new Promise(resolve => server.close(resolve));
}
