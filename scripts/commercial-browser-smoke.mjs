// Actual Chromium UI driver. All endpoints are local synthetic fixtures.
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require('/Users/admin/.npm/_npx/a80a913f4f8f2557/node_modules/playwright');
const cfg = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const headless = process.env.REPLAY_E2E_HEADED !== '1';
const evidence = { passed: false, stage: 'launch', checks: {}, javascript_errors: 0, headless };
const safePath = value => { const url = new URL(value); return url.origin + url.pathname.replace(/[a-f0-9]{32,}/g, '[id]'); };
const browser = await chromium.launch({ headless,
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  args: ['--disable-background-networking', '--disable-component-update', '--no-first-run'] });
const context = await browser.newContext({ ignoreHTTPSErrors: true, acceptDownloads: true, viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(25_000);
let currentToken = '', mediaId = '', jobId = '';
let browserFileTransfers = 0, cloudSourceImports = 0, helperRequests = 0;
const tokens = new Set();
page.on('pageerror', () => { evidence.javascript_errors++; });
page.on('requestfailed', request => {
  evidence.request_failures ||= [];
  if (evidence.request_failures.length < 12) evidence.request_failures.push({ path: safePath(request.url()), error: request.failure()?.errorText });
});
page.on('response', response => {
  if (response.status() >= 400) {
    evidence.http_failures ||= [];
    if (evidence.http_failures.length < 12) evidence.http_failures.push({ path: safePath(response.url()), status: response.status() });
  }
});
page.on('request', request => {
  if ((cfg.device_import || cfg.server_import) && ((request.url() === cfg.api + '/api/uploads' && request.method() === 'POST')
      || new URL(request.url()).pathname.endsWith('/file')
      || (new URL(request.url()).pathname === '/api/storage/local' && request.method() === 'PUT'))) browserFileTransfers++;
  if (request.url().includes('/api/device-imports') || new URL(request.url()).port === '17833') helperRequests++;
  if (request.url() === cfg.api + '/api/media/imports' && request.method() === 'POST') cloudSourceImports++;
  if (request.url().startsWith(cfg.api + '/api/')) {
    const authorization = request.headers().authorization;
    if (authorization?.startsWith('Bearer ')) { currentToken = authorization.slice(7); tokens.add(currentToken); }
  }
});
await context.route('**/*', async route => {
  const url = new URL(route.request().url());
  if (!['127.0.0.1', 'localhost'].includes(url.hostname)) return route.abort('blockedbyclient');
  return route.continue();
});

let healthReleased = false;
let releaseHealth;
const healthGate = new Promise(resolve => { releaseHealth = resolve; });
if (cfg.server_import) await page.route(cfg.api + '/api/health', async route => {
  await healthGate; healthReleased = true; return route.continue();
});

try {
  evidence.browser_version = browser.version();
  evidence.stage = 'oidc_login';
  await page.goto(cfg.web);
  const loginStarted = performance.now();
  await page.getByRole('button', { name: '계정으로 로그인', exact: true }).click();
  await page.getByRole('heading', { name: '새 방송 만들기', exact: true }).waitFor();
  await page.locator('.connection').filter({ hasText: '스튜디오 연결됨' }).waitFor();
  assert.ok(currentToken);
  if (cfg.server_import) {
    assert.equal(healthReleased, false);
    evidence.checks.login_connected_without_waiting_for_storage_health = true;
    evidence.login_to_connected_ms = Math.round(performance.now() - loginStarted);
    releaseHealth();
  }
  evidence.checks.real_oidc_redirect_login = true;
  evidence.stage = 'synthetic_401_real_refresh';
  const tokenBeforeRefresh = currentToken;
  let injected401 = 0;
  await page.route(cfg.api + '/api/usage', async route => {
    if (route.request().method() === 'GET' && injected401 === 0) {
      injected401++;
      return route.fulfill({ status: 401, contentType: 'application/json',
        headers: { 'Access-Control-Allow-Origin': cfg.web }, body: JSON.stringify({ detail: 'Synthetic one-time token rejection' }) });
    }
    return route.continue();
  });
  await page.waitForResponse(response => response.url() === cfg.api + '/api/usage' && response.status() === 200
    && response.request().headers().authorization !== 'Bearer ' + tokenBeforeRefresh);
  assert.equal(injected401, 1);
  assert.notEqual(currentToken, tokenBeforeRefresh);
  evidence.checks.synthetic_401_triggered_real_oidc_refresh_and_retry = true;
  evidence.injected_usage_401 = injected401;
  const tokenBeforeLogout = currentToken;

  evidence.stage = 'upload_and_validation';
  if (cfg.server_import) {
    evidence.stage = 'server_link_import';
    await page.getByRole('button', { name: '영상 링크', exact: true }).click();
    await page.getByLabel('원본 영상 플랫폼', { exact: true }).selectOption('direct');
    await page.getByLabel('녹화 영상 링크', { exact: true }).fill('https://media.example/synthetic.mp4');
    await page.locator('.studio-optional-field summary').click();
    await page.locator('#source-name').fill('synthetic-browser.mp4');
    const response = page.waitForResponse(r => r.url() === cfg.api + '/api/media/imports' && r.request().method() === 'POST');
    await page.getByRole('button', { name: '영상 가져오기', exact: true }).click();
    const imported = await response;
    assert.equal(imported.status(), 202);
    mediaId = (await imported.json()).media.id;
    assert.equal(helperRequests, 0); assert.equal(browserFileTransfers, 0); assert.equal(cloudSourceImports, 1);
    evidence.checks.server_import_without_helper_or_browser_file_transfer = true;
  } else if (cfg.device_import) {
    evidence.stage = 'pc_link_import_and_direct_upload';
    await page.getByRole('button', { name: '영상 링크', exact: true }).click();
    await page.getByLabel('도우미 연결 코드', { exact: true }).fill('SYNTHETIC-BROWSER');
    await page.getByRole('button', { name: '내 컴퓨터 연결', exact: true }).click();
    await page.getByText('내 컴퓨터를 연결했습니다. 영상 링크를 입력해 주세요.', { exact: true }).waitFor();
    await page.getByLabel('원본 영상 플랫폼', { exact: true }).selectOption('direct');
    await page.getByLabel('녹화 영상 링크', { exact: true }).fill('https://media.example/synthetic.mp4');
    await page.locator('.studio-optional-field summary').click();
    await page.locator('#source-name').fill('synthetic-browser.mp4');
    const taskResponse = page.waitForResponse(response => response.url() === cfg.api + '/api/device-imports'
      && response.request().method() === 'POST' && response.status() === 201);
    const completion = page.waitForResponse(response => response.url().startsWith(cfg.api + '/api/device-imports/')
      && response.request().method() === 'GET' && response.status() === 200, { timeout: 60_000 });
    await page.getByRole('button', { name: '영상 가져오기', exact: true }).click();
    mediaId = (await (await taskResponse).json()).id;
    const imported = await (await completion).json();
    assert.equal(imported.state, 'completed');
    assert.equal(imported.media.id, mediaId);
    assert.equal(browserFileTransfers, 0);
    assert.equal(cloudSourceImports, 0);
    evidence.checks.pc_link_download_direct_cloud_upload_no_browser_file_transfer = true;
  } else {
  const upload = page.waitForResponse(response => response.url() === cfg.api + '/api/uploads' && response.request().method() === 'POST');
  await page.locator('input[type=file]').setInputFiles(cfg.sample);
  const uploadResponse = await upload;
  assert.equal(uploadResponse.status(), 201);
  mediaId = (await uploadResponse.json()).media.id;
  }
  await page.getByRole('button', { name: '보관함', exact: true }).click();
  const recording = page.getByRole('button', { name: 'synthetic-browser.mp4 선택', exact: true });
  await recording.waitFor({ state: 'visible', timeout: 60_000 });
  await page.waitForFunction(() => !document.querySelector('[aria-label="synthetic-browser.mp4 선택"]')?.disabled);
  await recording.click();
  await page.getByRole('button', { name: '파일 테스트', exact: true }).click();
  await page.waitForFunction(() => {
    const video = document.querySelector('video');
    return video && video.readyState >= 2 && video.videoWidth > 0;
  }, undefined, { timeout: 30_000 });
  evidence.checks.direct_upload_validated_and_playable_preview = true;
  await page.getByText('예상 결과 파일', { exact: false }).waitFor();
  const authHeaders = () => ({ Authorization: 'Bearer ' + currentToken });
  const originalQuote = await (await fetch(cfg.api + `/api/media/${mediaId}/output-estimate`, { headers: authHeaders() })).json();
  assert.equal(originalQuote.can_create, true);
  evidence.output_quote = { estimated_output_bytes: originalQuote.estimated_output_bytes,
    storage_available_bytes: originalQuote.storage_available_bytes, can_create: originalQuote.can_create };
  evidence.checks.output_reservation_quote_visible = true;

  evidence.stage = 'network_outage_reconnect';
  await context.setOffline(true);
  const reconnectNotice = page.locator('output.studio-message').filter({ hasText: '서버에 다시 연결하고 있습니다.' });
  await reconnectNotice.waitFor({ state: 'visible', timeout: 15_000 });
  assert.equal(await page.getByRole('button', { name: '계정으로 로그인', exact: true }).count(), 0);
  await context.setOffline(false);
  await reconnectNotice.getByRole('button', { name: '다시 연결', exact: true }).click();
  await page.locator('.connection').filter({ hasText: '스튜디오 연결됨' }).waitFor();
  assert.equal(currentToken, tokenBeforeLogout);
  evidence.checks.network_outage_kept_login_and_reconnected = true;

  evidence.stage = 'ambiguous_create_retry';
  const attempts = [];
  let loseResponse = true;
  await page.route(cfg.api + '/api/broadcasts', async route => {
    if (route.request().method() !== 'POST') return route.continue();
    const response = await route.fetch();
    const result = await response.json();
    attempts.push({ id: result.id, status: response.status(), key: route.request().headers()['idempotency-key'], replayed: result.replayed === true });
    if (loseResponse) {
      loseResponse = false;
      const usage = await (await fetch(cfg.api + '/api/usage', { headers: authHeaders() })).json();
      assert.equal(usage.storage_reserved_bytes, originalQuote.estimated_output_bytes);
      assert.equal(usage.storage_limit_bytes, 20 * 1024 ** 2);
      evidence.reserved_usage = { storage_bytes: usage.storage_bytes, storage_reserved_bytes: usage.storage_reserved_bytes,
        storage_limit_bytes: usage.storage_limit_bytes, storage_available_bytes: usage.storage_available_bytes };
      return route.abort('failed');
    }
    return route.fulfill({ response });
  });
  await page.getByLabel('방송 이름', { exact: true }).fill('브라우저 합성 검증');
  await page.getByRole('button', { name: '송출 시작', exact: true }).click();
  await page.locator('[role=alert]').waitFor();
  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].status, 201);
  await page.getByText('저장 공간이 부족합니다.', { exact: false }).waitFor({ timeout: 10_000 });
  assert.equal(await page.getByRole('button', { name: '송출 시작', exact: true }).isEnabled(), true);
  await page.getByRole('button', { name: '송출 시작', exact: true }).click();
  await page.getByText('방송을 등록했습니다. 시작 상태를 확인하세요.', { exact: true }).waitFor();
  assert.equal(attempts.length, 2);
  assert.equal(attempts[0].key, attempts[1].key);
  assert.equal(attempts[0].id, attempts[1].id);
  assert.equal(attempts[1].replayed, true);
  jobId = attempts[1].id;
  evidence.checks.lost_commit_response_retry_reused_same_job = true;
  evidence.checks.ambiguous_retry_allowed_after_quota_reserved = true;
  evidence.create_attempts = attempts.length;
  evidence.created_jobs = new Set(attempts.map(value => value.id)).size;

  evidence.stage = 'stream_complete_download';
  await page.locator('.studio-job-list article').getByText('완료', { exact: true }).waitFor({ timeout: 60_000 });
  const downloadPromise = page.waitForEvent('download', { timeout: 25_000 });
  await page.getByRole('button', { name: '결과 받기', exact: true }).click();
  const download = await downloadPromise;
  await download.saveAs(cfg.download);
  assert.equal(await download.failure(), null);
  evidence.checks.local_ffmpeg_completed_and_downloaded = true;
  const settled = await (await fetch(cfg.api + '/api/usage', { headers: authHeaders() })).json();
  const uploadBytes = (await fs.stat(cfg.sample)).size;
  const outputBytes = (await fs.stat(cfg.download)).size;
  assert.equal(settled.storage_reserved_bytes, 0);
  assert.equal(settled.storage_bytes, uploadBytes + outputBytes);
  evidence.settled_usage = { storage_bytes: settled.storage_bytes, storage_reserved_bytes: settled.storage_reserved_bytes,
    storage_limit_bytes: settled.storage_limit_bytes, storage_available_bytes: settled.storage_available_bytes };
  evidence.checks.output_reservation_settled_to_actual_bytes = true;

  evidence.stage = 'second_output_quota_rejection';
  await page.getByLabel('방송 이름', { exact: true }).fill('두 번째 결과 공간 초과');
  const deniedPromise = page.waitForResponse(response => response.url() === cfg.api + '/api/broadcasts'
    && response.request().method() === 'POST' && response.status() === 409);
  await page.getByRole('button', { name: '송출 시작', exact: true }).click();
  const denied = await deniedPromise;
  const denial = await denied.json();
  assert.equal(denial.code, 'OUTPUT_STORAGE_QUOTA_EXCEEDED');
  await page.locator('[role=alert]').filter({ hasText: '공간' }).waitFor();
  const afterDenied = await (await fetch(cfg.api + '/api/broadcasts', { headers: authHeaders() })).json();
  assert.equal(afterDenied.length, 1);
  assert.equal(afterDenied[0].id, jobId);
  evidence.quota_rejection = { status: denied.status(), code: denial.code, jobs_after: afterDenied.length };
  evidence.checks.second_new_job_rejected_before_worker = true;
  evidence.javascript_errors_before_logout = evidence.javascript_errors;

  evidence.stage = 'logout_relogin_persistence';
  // The login button renders before the asynchronous OIDC sign-out finishes.
  // Wait for its return navigation before checking revocation or logging in again.
  await Promise.all([
    page.waitForNavigation({ url: cfg.web + '/', waitUntil: 'domcontentloaded' }),
    page.getByRole('button', { name: '로그아웃', exact: true }).click(),
  ]);
  await page.getByRole('button', { name: '계정으로 로그인', exact: true }).waitFor();
  const revoked = await fetch(cfg.api + '/api/me', { headers: { Authorization: 'Bearer ' + tokenBeforeLogout } });
  assert.equal(revoked.status, 401, `Old access token returned HTTP ${revoked.status} after logout`);
  evidence.checks.logout_revoked_old_token = true;
  await page.getByRole('button', { name: '계정으로 로그인', exact: true }).click();
  await page.getByRole('heading', { name: '새 방송 만들기', exact: true }).waitFor();
  await page.locator('.connection').filter({ hasText: '스튜디오 연결됨' }).waitFor();
  await page.getByRole('button', { name: '브라우저 합성 검증 실행 기록', exact: true }).waitFor();
  assert.notEqual(currentToken, tokenBeforeLogout);
  const headers = { Authorization: 'Bearer ' + currentToken };
  const media = await (await fetch(cfg.api + '/api/media', { headers })).json();
  const jobs = await (await fetch(cfg.api + '/api/broadcasts', { headers })).json();
  assert.equal(media.length, 1); assert.equal(media[0].id, mediaId);
  assert.equal(jobs.length, 1); assert.equal(jobs[0].id, jobId); assert.equal(jobs[0].state, 'completed');
  evidence.checks.relogin_same_tenant_preserved_media_and_completed_job = true;
  evidence.access_tokens_issued = tokens.size;
  assert.equal(evidence.javascript_errors, 0);
  evidence.stage = 'completed';
  if (cfg.device_import) {
    assert.equal(browserFileTransfers, 0);
    assert.equal(cloudSourceImports, 0);
    evidence.browser_file_transfers = browserFileTransfers;
    evidence.cloud_source_import_requests = cloudSourceImports;
  }
  evidence.passed = true;
} catch (error) {
  evidence.browser_failure_type = error.constructor.name;
  evidence.failure_action = error.message.split('\n')[0].slice(0, 180).replace(/https?:\/\/[^\s]+/g, '[url]');
  evidence.last_page = safePath(page.url());
  // Only synthetic page labels, never browser storage, URLs, JWTs or headers.
  evidence.visible_error = (await page.locator('[role=alert]').allTextContents().catch(() => [])).join(' ').slice(0, 400);
  evidence.visible_heading = (await page.locator('h1').allTextContents().catch(() => [])).join(' ').slice(0, 100);
} finally {
  releaseHealth?.();
  await fs.writeFile(cfg.result, JSON.stringify(evidence, null, 2));
  await browser.close();
}
process.exitCode = evidence.passed ? 0 : 1;
