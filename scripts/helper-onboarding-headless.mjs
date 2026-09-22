// Isolated headless browser; never attaches to a user's Chrome/profile.
// OS hints, helper responses and release downloads are synthetic fixtures.
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('..', import.meta.url));
const require = createRequire(path.join(root, 'web/package.json'));
const { chromium } = require(process.env.REPLAY_PLAYWRIGHT_MODULE || 'playwright');
const port = process.env.REPLAY_HELPER_PREVIEW_PORT || '0';
let origin;
const scratch = await mkdtemp(path.join(tmpdir(), 'replay-helper-headless-'));
const server = spawn(process.execPath, ['scripts/helper-browser-preview.mjs'], {
  cwd: root, env: { ...process.env, REPLAY_HELPER_PREVIEW_PORT: port }, stdio: ['ignore', 'pipe', 'pipe'],
});
let serverLog = '';
server.stdout.on('data', value => { serverLog += value; });
server.stderr.on('data', value => { serverLog += value; });
const serverExit = once(server, 'exit');
let browser;
const checks = [];
const fixtureBytes = Buffer.from('Replay helper download test fixture. No executable content.\n');
const assets = [
  ['macOS Apple Silicon', 'macos-arm64'], ['macOS Intel', 'macos-x64'], ['Windows x64', 'windows-x64'],
].map(([label, suffix]) => ({ label, sha256: 'a'.repeat(64),
  url: `https://github.com/hhj4861/replay-live/releases/download/helper-v0.2.0/ReplayLiveHelper-0.2.0-${suffix}.zip` }));
async function scenario({ name, os = 'macOS', arch = 'arm', label, published = true, online = false, unknown = false, mobile = false }) {
  const context = await browser.newContext({ acceptDownloads: true, viewport: { width: 1280, height: 900 } });
  try {
    const errors = []; const downloads = []; const assetRequests = [];
    let receiveDownload;
    const attach = page => { page.on('pageerror', error => errors.push(error.message)); page.on('download', value => { downloads.push(value); receiveDownload?.(value); }); };
    context.on('page', attach);
    await context.addInitScript(({ os, arch, unknown, mobile }) => {
      Object.defineProperty(navigator, 'userAgentData', { configurable: true, value: {
        platform: os, mobile, getHighEntropyValues: async () => unknown ? {} : { architecture: arch, bitness: '64' },
      } });
    }, { os, arch, unknown, mobile });
    await context.route('**/*', async route => {
      const request = route.request(); const url = new URL(request.url());
      if (url.origin === 'http://127.0.0.1:17833') {
        const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Headers': 'Content-Type, X-Replay-Local, Authorization', 'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS' };
        if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
        if (!online) return route.fulfill({ status: 503, headers, json: { detail: '검증용 도우미 미실행 상태' } });
        if (url.pathname === '/pairing-code') return route.fulfill({ headers, json: { version: 1, code: 'ABCDEF123456', features: ['tab-reconnect'] } });
        if (url.pathname === '/pair') return route.fulfill({ headers, json: { version: 1, token: 'x'.repeat(43), features: ['cloud-direct-upload'] } });
        return route.fulfill({ status: 204, headers });
      }
      if (url.origin === origin && url.pathname === '/helper-release.json') {
        return route.fulfill({ json: published ? { version: '0.2.0', downloads: assets } : { version: null, downloads: [] } });
      }
      const asset = assets.find(item => item.url === url.href);
      if (asset) {
        assetRequests.push(asset.label);
        return route.fulfill({ status: 200, contentType: 'application/octet-stream', headers: {
          'Content-Disposition': `attachment; filename="${path.basename(url.pathname)}"`,
        }, body: fixtureBytes });
      }
      if (url.origin === origin) return route.continue();
      return route.abort('blockedbyclient'); // no cloud/API/profile access
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10_000);
    await page.goto(origin);
    await page.getByRole('button', { name: '검증용 로그인', exact: true }).click();
    if (online) {
      await page.locator('.local-import-badge').filter({ hasText: '연결됨' }).waitFor();
      assert.equal(await page.locator('dialog[open]').count(), 0);
      await page.getByRole('button', { name: '검증용 로그아웃', exact: true }).click();
      await page.getByRole('button', { name: '검증용 로그인', exact: true }).click();
      await page.locator('.local-import-badge').filter({ hasText: '연결됨' }).waitFor();
      assert.equal(downloads.length, 0); assert.equal(assetRequests.length, 0);
      assert.deepEqual(errors, []); checks.push(`${name}: reconnect without installation/download`); return;
    }
    await page.getByText('검증용 도우미 미실행 상태', { exact: false }).first().waitFor();
    assert.equal(await page.locator('dialog[open]').count(), 0);
    assert.equal(downloads.length, 0); assert.equal(assetRequests.length, 0);
    const trigger = page.getByRole('button', { name: '영상 가져오기 (설치 안내 검증)', exact: true });
    await trigger.click();
    const dialog = page.getByRole('dialog', { name: '영상 가져오기에 도우미가 필요합니다' });
    await dialog.waitFor();
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(await trigger.evaluate(element => element === document.activeElement), true);
    assert.equal(downloads.length, 0); assert.equal(assetRequests.length, 0);
    await trigger.click();
    await dialog.waitFor();
    const confirm = dialog.getByRole('button', { name: '확인 · 설치 파일 다운로드', exact: true });
    if (!published || mobile) {
      assert.equal(await confirm.isDisabled(), true);
      await dialog.getByRole('button', { name: '취소', exact: true }).click();
      await dialog.waitFor({ state: 'hidden' });
      assert.equal(downloads.length, 0); assert.equal(assetRequests.length, 0);
      checks.push(`${name}: no unavailable/unsupported installer offered`);
    } else {
      if (unknown) {
        assert.equal(await confirm.isDisabled(), true);
        await dialog.getByLabel('도우미를 설치할 PC 종류').selectOption(label);
      }
      await dialog.getByText('선택한 설치 파일:', { exact: false }).filter({ hasText: label }).waitFor();
      assert.equal(await confirm.isEnabled(), true);
      const downloadReady = new Promise(resolve => { receiveDownload = resolve; });
      await confirm.click();
      let timer;
      const download = await Promise.race([downloadReady, new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Expected fixture download did not start')), 10_000);
      })]).finally(() => clearTimeout(timer));
      const file = path.join(scratch, `${name}.fixture`);
      await download.saveAs(file);
      assert.equal(await download.failure(), null);
      assert.deepEqual(await readFile(file), fixtureBytes);
      assert.deepEqual(assetRequests, [label]); assert.equal(downloads.length, 1);
      assert.equal(page.url(), origin + '/');
      await dialog.getByText('다운로드를 요청했습니다.', { exact: false }).waitFor();
      online = true; // Simulate helper becoming available after installation.
      await page.locator('.local-import-badge').filter({ hasText: '연결됨' }).waitFor();
      await dialog.waitFor({ state: 'hidden' });
      checks.push(`${name}: confirmation downloads correct fixture, then connects and closes`);
    }
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
}
try {
  for (let n = 0; !serverLog.includes('Helper browser verification:'); n++) {
    if (server.exitCode !== null || n > 100) throw new Error(`Preview failed: ${serverLog.slice(-800)}`);
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  origin = serverLog.match(/Helper browser verification: (http:\/\/127\.0\.0\.1:\d+)/)[1];
  browser = await chromium.launch({ headless: true,
    ...(process.env.REPLAY_CHROME_EXECUTABLE ? { executablePath: process.env.REPLAY_CHROME_EXECUTABLE } : {}),
    args: ['--disable-background-networking', '--disable-component-update', '--no-first-run'],
  });
  await scenario({ name: 'mac-arm64', label: 'macOS Apple Silicon' });
  await scenario({ name: 'mac-x64', arch: 'x86', label: 'macOS Intel' });
  await scenario({ name: 'windows-x64', os: 'Windows', arch: 'x86', label: 'Windows x64' });
  await scenario({ name: 'unknown-mac', unknown: true, label: 'macOS Intel' });
  await scenario({ name: 'unpublished', published: false });
  await scenario({ name: 'mobile', mobile: true });
  await scenario({ name: 'existing-helper', online: true });
  console.log(JSON.stringify({ passed: true, headless: true, fixtureOnly: true, browser: browser.version(), checks }, null, 2));
} finally {
  await browser?.close();
  if (server.exitCode === null) server.kill('SIGTERM');
  await serverExit;
  await rm(scratch, { recursive: true, force: true });
}
