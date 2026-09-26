// Isolated headless browser; never attaches to a user's Chrome/profile.
// OS hints, helper responses and release downloads are synthetic fixtures.
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises';
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
async function scenario({ name, os = 'macOS', arch = 'arm', label, published = true, online = false, unknown = false, mobile = false, launchExisting = false }) {
  console.log('Checking ' + name);
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
    assert.equal(await page.locator('dialog[open]').count(), 0);
    assert.equal(downloads.length, 0); assert.equal(assetRequests.length, 0);
    if (online) {
      await page.getByRole('button', { name: '영상 가져오기', exact: true }).click();
      await page.getByText('연결됨', { exact: true }).waitFor();
      assert.equal(await page.locator('dialog[open]').count(), 0);
      assert.equal(downloads.length, 0); assert.deepEqual(errors, []);
      checks.push(`${name}: on-demand connection without download`); return;
    }
    const trigger = page.getByRole('button', { name: '영상 가져오기', exact: true });
    await trigger.click();
    const dialog = page.locator('dialog[open]');
    await dialog.waitFor();
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    await page.waitForFunction(() => document.activeElement?.textContent === '영상 가져오기');
    assert.equal(downloads.length, 0); assert.equal(assetRequests.length, 0);
    await trigger.click();
    await dialog.waitFor();
    await dialog.locator('.helper-primary-action').waitFor();
    assert.equal(await dialog.getByRole('button', { name: '취소', exact: true }).count(), 0);
    assert.equal(await dialog.getByRole('button', { name: '다시 연결', exact: true }).isVisible(), false);
    if (!mobile) {
      await dialog.locator('summary').focus(); await page.keyboard.press('Enter');
      assert.equal(await dialog.getByRole('button', { name: '다시 연결', exact: true }).isVisible(), true);
      await dialog.locator('summary').click();
    }
    for (const [width, height] of [[1280, 900], [390, 844]]) {
      await page.setViewportSize({ width, height });
      const box = await dialog.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width + 1 && box.y >= 0 && box.y + box.height <= height + 1);
      assert.equal(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth), true);
      if (process.env.REPLAY_HELPER_SCREENSHOTS) {
        await mkdir(process.env.REPLAY_HELPER_SCREENSHOTS, { recursive: true });
        await dialog.screenshot({ animations: 'disabled', path: path.join(process.env.REPLAY_HELPER_SCREENSHOTS, `dialog-${name}-${width}.png`) });
      }
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    const confirm = dialog.getByRole('button', { name: '동의하고 다운로드', exact: true });
    if (launchExisting) {
      // Exercise the app-link handler without launching an installed OS app.
      await page.evaluate(() => document.addEventListener('click', event => {
        if (event.target.closest?.('a[href="replay-live-helper://start"]')) event.preventDefault();
      }));
      await dialog.getByRole('link', { name: '도우미 실행', exact: true }).click();
      await dialog.getByText('도우미가 열리면 자동으로 연결돼요.', { exact: true }).waitFor();
      assert.equal(await dialog.locator('.helper-primary-action button, .helper-primary-action a').count(), 0);
      assert.equal(downloads.length, 0); assert.equal(assetRequests.length, 0);
      online = true;
      await page.getByText('연결됨', { exact: true }).waitFor();
      await dialog.waitFor({ state: 'hidden' });
      assert.deepEqual(errors, []);
      checks.push(`${name}: one launch action waits and reconnects automatically without download`); return;
    }
    if (!published || mobile || unknown) {
      assert.equal(await confirm.count(), 0);
      await dialog.getByRole('button', { name: '도우미 연결 닫기', exact: true }).click();
      await dialog.waitFor({ state: 'hidden' });
      assert.equal(downloads.length, 0); assert.equal(assetRequests.length, 0);
      checks.push(`${name}: no unavailable/unsupported installer offered`);
    } else {
      await dialog.getByText(label, { exact: true }).waitFor();
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
      await dialog.getByText('설치 후 자동으로 연결돼요.', { exact: false }).waitFor();
      online = true; // Simulate helper becoming available after installation.
      await page.getByText('연결됨', { exact: true }).waitFor();
      await dialog.waitFor({ state: 'hidden' });
      checks.push(`${name}: confirmation downloads correct fixture, then connects and closes`);
    }
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
}

// Render the real authenticated studio. Only session/API/helper data are fixtures.
async function studioScenario() {
  console.log('Checking real studio');
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const errors = []; const unexpected = []; const writes = []; const localRequests = [];
  let online = false; let rejectPair = false; let viewer = false; let delayPair = null; let published = false;
  let imported = false;
  const id = 'a'.repeat(32);
  const media = { id: 'fixture-media', name: '기존 보관 영상.mp4', status: 'ready', duration: 30, bytes: 1000 };
  const importedMedia = { ...media, id, name: '자동 가져온 영상.mp4' };
  const job = { id: 'fixture-job', title: '진행 중인 검증 방송', media_id: media.id, media_name: media.name,
    target: 'youtube', state: 'streaming', progress: 1, duration: 60, scheduled: 1700000000 };
  const seedSession = () => sessionStorage.setItem('replay-google-session', JSON.stringify({
    token: 'headless_fixture_token_'.padEnd(43, 'x'), expires_at: Date.now() / 1000 + 3500,
    absolute_expires_at: Date.now() / 1000 + 85000,
  }));
  try {
    await context.addInitScript(seedSession);
    await context.addInitScript(() => Object.defineProperty(navigator, 'userAgentData', { configurable: true, value: { platform: 'macOS', getHighEntropyValues: async () => ({ architecture: 'arm', bitness: '64' }) } }));
    await context.route('**/*', async route => {
      const request = route.request(); const url = new URL(request.url());
      if (url.origin === 'http://127.0.0.1:17833') {
        const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Headers': 'Content-Type, X-Replay-Local, Authorization', 'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS' };
        if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
        localRequests.push(url.pathname);
        if (!online) return route.fulfill({ status: 503, headers, json: { detail: '검증용 도우미 미실행 상태' } });
        if (url.pathname === '/pairing-code') return route.fulfill({ headers, json: { version: 1, code: 'ABCDEF123456', features: ['tab-reconnect'] } });
        if (url.pathname === '/pair') {
          if (delayPair) await delayPair;
          return rejectPair ? route.fulfill({ status: 403, headers, json: { detail: '검증용 연결 거부' } })
            : route.fulfill({ headers, json: { version: 1, token: 'x'.repeat(43), features: ['cloud-direct-upload'] } });
        }
        if (url.pathname === '/cloud-imports') {
          imported = true;
          return route.fulfill({ headers, json: { id, state: 'ready', media_id: id } });
        }
        return route.fulfill({ status: 204, headers });
      }
      if (url.origin === origin && url.pathname === '/helper-release.json') return route.fulfill({ json: published ? { version: '0.2.0', downloads: assets } : { version: null, downloads: [] } });
      if (assets.some(item => item.url === url.href)) return route.fulfill({ contentType: 'application/octet-stream', headers: { 'Content-Disposition': 'attachment; filename=helper-fixture.zip' }, body: fixtureBytes });
      if (url.origin === origin && url.pathname.startsWith('/api/')) {
        if (request.method() !== 'GET') writes.push({ path: url.pathname, body: request.postDataJSON() });
        const fixtures = {
          '/api/media': imported ? [importedMedia, media] : [media], '/api/broadcasts': [job],
          '/api/health': { ready: true, max_upload_mb: 50, max_duration_seconds: 120, retention_days: 7, max_concurrent: 1 },
          '/api/usage': { storage_bytes: 1000, storage_limit_bytes: 100000000, storage_reserved_bytes: 0, storage_available_bytes: 99999000 },
          '/api/me': { tenant_id: 'fixture-tenant', subject: 'fixture-user', roles: [viewer ? 'viewer' : 'operator'] },
          '/api/stream-targets': { max_destinations: 1, targets: [{ id: 'youtube', label: 'YouTube Live', default_server_url: 'rtmp://a.rtmp.youtube.com/live2', requires_server_url: false, requires_stream_key: true, note: '', setup_url: null }] },
          '/api/media-sources': { sources: [{ id: 'youtube', label: 'YouTube', note: '' }, { id: 'direct', label: 'MP4 링크', note: 'HTTPS MP4 파일 주소를 입력하세요.' }] },
          '/api/device-imports': { id, token: 'task-only-fixture' },
          [`/api/device-imports/${id}`]: { state: 'completed', media: importedMedia },
        };
        if (/\/media\/[^/]+\/preview$/.test(url.pathname)) return route.fulfill({ json: { url: origin + '/fixture-preview.mp4' } });
        if (url.pathname in fixtures) return route.fulfill({ json: fixtures[url.pathname] });
        if (url.pathname.includes('revoke') || url.pathname.includes('logout')) return route.fulfill({ json: {} });
        unexpected.push(url.pathname); return route.fulfill({ status: 404, json: { detail: 'Unexpected fixture request' } });
      }
      if (url.origin === origin && url.pathname === '/fixture-preview.mp4') return route.fulfill({ status: 204 });
      if (url.origin === origin) return route.continue();
      unexpected.push(url.origin); return route.abort('blockedbyclient');
    });
    const page = await context.newPage(); page.setDefaultTimeout(10_000);
    page.on('pageerror', error => errors.push(error.message));
    const opened = async () => {
      await page.getByLabel('원본 영상 플랫폼', { exact: true }).waitFor();
      await page.getByLabel('녹화 영상 링크', { exact: true }).waitFor();
      await page.getByLabel('방송 이름', { exact: true }).waitFor();
      assert.equal(await page.locator('.studio-launch-dock').isVisible(), true);
      await page.getByRole('button', { name: '중지', exact: true }).waitFor();
      assert.equal(await page.getByText('도우미 데몬 사용', { exact: true }).count(), 0);
    };
    const trigger = page.getByRole('button', { name: '영상 가져오기', exact: true });
    const dialog = page.getByRole('dialog');
    const tickets = () => writes.filter(item => item.path === '/api/device-imports');
    await page.goto(origin + '/studio.html'); await opened();
    assert.equal(localRequests.length, 0); assert.deepEqual(writes, []);
    // Progressive help stays out of the form until requested, including mobile.
    const help = page.getByRole('button', { name: '영상 링크 도움말', exact: true });
    const helpPopup = page.getByRole('dialog', { name: '영상 링크 안내', exact: true });
    const nameSummary = page.locator('.studio-optional-field summary');
    for (const [width, height] of [[1280, 900], [390, 844]]) {
      await page.setViewportSize({ width, height });
      assert.equal(await page.locator('#source-name').isVisible(), false);
      assert.equal(await page.getByText('YouTube의 로그인·봇 확인으로 제한될 수 있어요.', { exact: false }).isVisible(), false);
      await page.getByLabel('녹화 영상 링크', { exact: true }).fill('https://youtu.be/GcOe4ILS6Ow');
      await help.scrollIntoViewIfNeeded();
      const formHeight = (await page.locator('.source-import-form').boundingBox()).height;
      if (process.env.REPLAY_HELPER_SCREENSHOTS) {
        await mkdir(process.env.REPLAY_HELPER_SCREENSHOTS, { recursive: true });
        await page.locator('.source-import-form').evaluate(element => element.scrollIntoView({ block: 'center' }));
        await page.locator('.source-import-form').screenshot({ animations: 'disabled', path: path.join(process.env.REPLAY_HELPER_SCREENSHOTS, `source-${width}.png`) });
      }
      await help.focus(); await page.keyboard.press('Enter'); await helpPopup.waitFor();
      await helpPopup.evaluate(element => Promise.all(element.getAnimations().map(animation => animation.finished)));
      await helpPopup.getByRole('heading', { name: '도우미는 언제 필요한가요?', exact: true }).waitFor();
      assert.equal((await page.locator('.source-import-form').boundingBox()).height, formHeight);
      const box = await helpPopup.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width + 1 && box.y >= 0 && box.y + box.height <= height + 1);
      if (process.env.REPLAY_HELPER_SCREENSHOTS) await page.screenshot({ animations: 'disabled', path: path.join(process.env.REPLAY_HELPER_SCREENSHOTS, `help-${width}.png`) });
      await page.keyboard.press('Escape'); await helpPopup.waitFor({ state: 'hidden' });
      await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === '영상 링크 도움말');
      assert.equal(await page.getByLabel('녹화 영상 링크', { exact: true }).inputValue(), 'https://youtu.be/GcOe4ILS6Ow');
      await help.click(); await helpPopup.waitFor();
      await helpPopup.getByRole('button', { name: '영상 링크 도움말 닫기', exact: true }).click();
      await helpPopup.waitFor({ state: 'hidden' });
      await help.click(); await helpPopup.waitFor();
      await page.getByLabel('원본 영상 플랫폼', { exact: true }).click();
      await helpPopup.waitFor({ state: 'hidden' });
      await page.keyboard.press('Escape');
      await nameSummary.click();
      await page.getByLabel('보관함 이름', { exact: true }).fill('간단한 이름');
      await nameSummary.click();
      assert.equal(await page.locator('#source-name').isVisible(), false);
      assert.equal(await page.locator('#source-name').inputValue(), '간단한 이름');
    }
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByLabel('원본 영상 플랫폼', { exact: true }).selectOption('direct');
    await help.click(); await helpPopup.waitFor();
    assert.equal(await helpPopup.getByRole('link', { name: 'YouTube 다운로드 방법', exact: true }).count(), 0);
    await helpPopup.getByText('HTTPS MP4 파일 주소를 입력하세요.', { exact: true }).waitFor();
    await page.keyboard.press('Escape'); await helpPopup.waitFor({ state: 'hidden' });
    await page.getByLabel('원본 영상 플랫폼', { exact: true }).selectOption('youtube');
    assert.equal(localRequests.length, 0); assert.deepEqual(writes, []);
    checks.push('link UX: concise initial form, click/keyboard help, Escape/close/outside dismissal, focus restoration, no layout shift, desktop/mobile bounds, provider-specific advice, collapsed optional name retains value, no helper/API writes from help');
    // Library/file/broadcast inputs work without any helper interaction.
    await page.getByRole('button', { name: '파일 업로드', exact: true }).click();
    await page.locator('input[type=file]').waitFor({ state: 'attached' });
    await page.getByRole('button', { name: '보관함', exact: true }).click();
    await page.getByRole('button', { name: '기존 보관 영상.mp4 선택', exact: true }).click();
    await page.getByRole('button', { name: 'YouTube Live', exact: false }).click();
    await page.getByLabel('방송 이름', { exact: true }).fill('입력 유지 검증');
    await page.locator('input[type=password]').first().fill('fixture-stream-key');
    await page.getByLabel('선택한 채널의 라이브 준비를 마쳤어요.', { exact: true }).check();
    assert.equal(await page.getByRole('button', { name: '송출 시작', exact: true }).isEnabled(), true);
    assert.equal(localRequests.length, 0);
    await page.getByRole('button', { name: '영상 링크', exact: true }).click();
    await page.getByLabel('녹화 영상 링크', { exact: true }).fill('https://youtu.be/GcOe4ILS6Ow');
    await trigger.click(); await dialog.waitFor();
    await dialog.getByRole('alert').waitFor();
    assert.equal(await dialog.locator('input,select').count(), 0);
    assert.equal(await dialog.getByRole('button', { name: '동의하고 다운로드', exact: true }).count(), 0);
    assert.equal(tickets().length, 0);
    await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'hidden' });
    await trigger.waitFor();
    assert.equal(await page.getByLabel('녹화 영상 링크', { exact: true }).inputValue(), 'https://youtu.be/GcOe4ILS6Ow');
    // Late successful pair after Cancel must not resume the request.
    online = true; let releasePair;
    delayPair = new Promise(resolve => { releasePair = resolve; });
    const pairing = page.waitForRequest(request => request.url().endsWith(':17833/pair'));
    await trigger.click(); await pairing;
    await dialog.getByRole('button', { name: '도우미 연결 닫기', exact: true }).click();
    const revoked = page.waitForRequest(request => request.url().endsWith(':17833/session') && request.method() === 'DELETE');
    releasePair(); delayPair = null; await revoked;
    assert.equal(tickets().length, 0);
    // A rejected pair shows its error; retry automatically resumes exactly once.
    rejectPair = true; await trigger.click();
    await dialog.getByText('검증용 연결 거부', { exact: false }).waitFor();
    assert.equal(tickets().length, 0);
    rejectPair = false; await dialog.locator('summary').click(); await dialog.getByRole('button', { name: '다시 연결', exact: true }).click();
    await page.locator('.studio-selected-media').getByText('자동 가져온 영상.mp4', { exact: true }).waitFor();
    assert.equal(tickets().length, 1);
    assert.equal(tickets()[0].body.url, 'https://youtu.be/GcOe4ILS6Ow');
    assert.equal(tickets()[0].body.name, '간단한 이름');
    assert.equal(await page.locator('dialog[open]').count(), 0);
    assert.equal(await page.getByLabel('방송 이름', { exact: true }).inputValue(), '입력 유지 검증');
    // Reload never downloads again or re-pairs without a new explicit link action.
    await page.reload(); await opened(); const count = localRequests.length;
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    assert.equal(localRequests.length, count); assert.equal(tickets().length, 1);
    await page.getByLabel('녹화 영상 링크', { exact: true }).fill('https://youtu.be/GcOe4ILS6Ow');
    online = false; await trigger.click(); await dialog.waitFor();
    await dialog.getByRole('button', { name: 'MP4 파일 업로드', exact: true }).click();
    await page.locator('input[type=file]').waitFor({ state: 'attached' });
    assert.equal(tickets().length, 1);
    // Consent -> fixture download -> helper appears -> original link resumes,
    // without a PC/code field or a second import click.
    published = true;
    await page.getByRole('button', { name: '영상 링크', exact: true }).click();
    await trigger.click(); await dialog.waitFor();
    await dialog.getByText('macOS Apple Silicon', { exact: true }).waitFor();
    const downloaded = context.waitForEvent('request', request => assets.some(item => item.url === request.url()));
    await dialog.getByRole('button', { name: '동의하고 다운로드', exact: true }).click();
    await downloaded; online = true;
    await dialog.waitFor({ state: 'hidden' });
    await page.getByText('영상이 준비됐습니다. 방송할 채널을 선택해 주세요.', { exact: true }).waitFor();
    assert.equal(tickets().length, 2);
    assert.equal(tickets()[1].body.url, 'https://youtu.be/GcOe4ILS6Ow');
    await page.reload(); await opened(); online = false;
    await page.getByLabel('녹화 영상 링크', { exact: true }).fill('https://youtu.be/GcOe4ILS6Ow');
    // Session invalidation while installation is pending removes the intent.
    await page.getByRole('button', { name: '영상 링크', exact: true }).click();
    await trigger.click(); await dialog.waitFor();
    await page.evaluate(() => window.dispatchEvent(new Event('replay-signin-required')));
    await page.getByRole('heading', { name: '방송 준비를 시작하세요', exact: true }).waitFor();
    online = true; await page.reload(); await opened();
    assert.equal(tickets().length, 2); assert.equal(await page.locator('dialog[open]').count(), 0);
    viewer = true; await page.reload(); await page.locator('.studio-history').waitFor();
    assert.equal(await page.locator('#broadcast-form').count(), 0);
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    checks.push('real studio: full forms on login; file/library/broadcast work without helper; link-only modal; no manual inputs; cancel and late pairing cannot import; retry and install consent each resume their original link exactly once; reload/session changes never replay; viewer history retained');
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
  await scenario({ name: 'launch-installed', published: false, launchExisting: true });
  await scenario({ name: 'mobile', mobile: true });
  await scenario({ name: 'existing-helper', online: true });
  await studioScenario();
  console.log(JSON.stringify({ passed: true, headless: true, fixtureOnly: true, browser: browser.version(), checks }, null, 2));
} finally {
  await browser?.close();
  if (server.exitCode === null) server.kill('SIGTERM');
  await serverExit;
  await rm(scratch, { recursive: true, force: true });
}
