// Visible-browser link import against isolated API/storage and a real local daemon.
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('/Users/admin/.npm/_npx/a80a913f4f8f2557/node_modules/playwright');
const cfg = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const headless = process.env.REPLAY_E2E_HEADED !== '1';
const evidence = { passed: false, stage: 'launch', headless, checks: {}, javascript_errors: 0,
  browser_file_uploads: 0, browser_daemon_file_reads: 0, cloud_source_import_requests: 0 };
const browser = await chromium.launch({ headless,
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  args: ['--disable-background-networking', '--disable-component-update', '--no-first-run'] });
const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(30_000);
page.on('pageerror', () => evidence.javascript_errors++);
page.on('request', request => {
  const url = new URL(request.url());
  if (request.method() === 'PUT' || (request.method() === 'POST' && url.pathname === '/api/uploads')) evidence.browser_file_uploads++;
  if (url.port === '17833' && url.pathname.endsWith('/file')) evidence.browser_daemon_file_reads++;
  if (url.pathname === '/api/media/imports') evidence.cloud_source_import_requests++;
});
await context.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname)
  ? route.continue() : route.abort('blockedbyclient'));
try {
  evidence.browser_version = browser.version();
  evidence.stage = 'login';
  await page.goto(cfg.web);
  await page.getByRole('button', { name: '계정으로 로그인', exact: true }).click();
  await page.getByRole('heading', { name: '새 방송 만들기', exact: true }).waitFor();
  await page.locator('.connection').filter({ hasText: '스튜디오 연결됨' }).waitFor();
  evidence.stage = 'pairing';
  await page.getByRole('button', { name: '영상 링크', exact: true }).click();
  await page.getByLabel('도우미 연결 코드', { exact: true }).fill('SYNTHETIC-BROWSER');
  await page.getByRole('button', { name: '내 컴퓨터 연결', exact: true }).click();
  await page.getByText('내 컴퓨터를 연결했습니다. 영상 링크를 입력해 주세요.', { exact: true }).waitFor();
  evidence.checks.pc_pairing = true;
  await page.getByLabel('원본 영상 플랫폼', { exact: true }).selectOption('youtube');
  await page.getByLabel('녹화 영상 링크', { exact: true }).fill(cfg.source_url);
  await page.locator('.source-import-form .studio-optional-field summary').click();
  await page.locator('#source-name').fill('actual-pc-import.mp4');
  evidence.stage = 'cloud_task_pc_download_direct_upload';
  const created = page.waitForResponse(r => r.url() === cfg.api + '/api/device-imports' && r.status() === 201);
  const completed = page.waitForResponse(r => r.url().startsWith(cfg.api + '/api/device-imports/')
    && r.request().method() === 'GET' && r.status() === 200, { timeout: 180_000 });
  await page.getByRole('button', { name: '영상 가져오기', exact: true }).click();
  const id = (await (await created).json()).id;
  const result = await (await completed).json();
  assert.equal(result.state, 'completed');
  assert.equal(result.media.id, id);
  evidence.checks.cloud_task_pc_download_direct_upload = true;
  evidence.stage = 'validation_and_preview';
  await page.getByRole('button', { name: '보관함', exact: true }).click();
  const recording = page.getByRole('button', { name: 'actual-pc-import.mp4 선택', exact: true });
  await recording.waitFor({ timeout: 90_000 });
  await recording.click({ timeout: 90_000 });
  await page.waitForFunction(() => {
    const video = document.querySelector('video');
    return video && video.readyState >= 2 && video.videoWidth > 0;
  }, undefined, { timeout: 30_000 });
  await page.locator('video').evaluate(async video => { video.muted = true; await video.play(); });
  await page.waitForFunction(() => {
    const video = document.querySelector('video');
    return video && !video.paused && video.currentTime >= 1 && !video.error;
  }, undefined, { timeout: 30_000 });
  evidence.preview = await page.locator('video').evaluate(video => ({ duration: video.duration,
    width: video.videoWidth, height: video.videoHeight, ready_state: video.readyState,
    playback_seconds: video.currentTime, paused: video.paused }));
  evidence.checks.validated_library_video_playable = true;
  assert.equal(evidence.browser_file_uploads, 0);
  assert.equal(evidence.browser_daemon_file_reads, 0);
  assert.equal(evidence.cloud_source_import_requests, 0);
  assert.equal(evidence.javascript_errors, 0);
  if (cfg.screenshot) await page.screenshot({ path: cfg.screenshot, fullPage: true });
  evidence.stage = 'completed';
  evidence.passed = true;
} catch (error) {
  evidence.failure_type = error.constructor.name;
  evidence.failure_action = error.message.split('\n')[0].slice(0, 180).replace(/https?:\/\/[^\s]+/g, '[url]');
  evidence.visible_error = (await page.locator('[role=alert]').allTextContents().catch(() => [])).join(' ').slice(0, 400);
} finally {
  await fs.writeFile(cfg.result, JSON.stringify(evidence, null, 2));
  await browser.close();
}
process.exitCode = evidence.passed ? 0 : 1;
