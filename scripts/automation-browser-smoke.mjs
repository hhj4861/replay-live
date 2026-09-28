// Headless UI -> local API -> durable SQLite verification. No external broadcasts.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.REPLAY_PLAYWRIGHT_MODULE || 'playwright');
const web = process.env.REPLAY_LOCAL_WEB_URL || 'http://127.0.0.1:13130';
const api = process.env.REPLAY_LOCAL_API_URL || 'http://127.0.0.1:18130';
for (const value of [web, api]) {
  assert.equal(new URL(value).protocol, 'http:');
  assert.ok(['localhost', '127.0.0.1'].includes(new URL(value).hostname));
}
const browser = await chromium.launch({ headless: true,
  ...(process.env.REPLAY_CHROME_PATH ? { executablePath: process.env.REPLAY_CHROME_PATH } : {}) });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.route('**/*', route => ['localhost', '127.0.0.1'].includes(new URL(route.request().url()).hostname)
  ? route.continue() : route.abort('blockedbyclient'));
const page = await context.newPage();
page.setDefaultTimeout(15000);
let authorization = '';
const errors = [];
page.on('pageerror', error => errors.push(error.message));
page.on('request', request => {
  if (request.url().startsWith(api + '/api/') && request.headers().authorization)
    authorization = request.headers().authorization;
});
const request = async (path, options = {}) => {
  const response = await context.request.fetch(api + '/api' + path, { ...options,
    headers: { Authorization: authorization, Origin: web, 'X-Replay-Client': '1', ...options.headers } });
  assert.ok(response.ok(), `${path}: ${response.status()}`);
  return response.json();
};
const report = { headless: true, local_only: true, external_streaming: false, checks: [] };
const capture = async name => {
  if (process.env.REPLAY_E2E_SCREENSHOT) await page.screenshot({
    path: process.env.REPLAY_E2E_SCREENSHOT.replace(/\.png$/, `-${name}.png`), fullPage: true });
};
let ruleId;
try {
  // Add a UI-only second library choice to verify exclusive radio selection.
  await page.route('**/api/media', async route => {
    const response = await route.fetch();
    const items = await response.json();
    const sample = items.find(item => item.status === 'ready');
    await route.fulfill({ response, json: sample ? [...items, { ...sample, id: 'ui-only-second-video', name: '단일 선택 확인용 영상.mp4' }] : items });
  });
  await page.goto(web);
  await page.getByRole('button', { name: '개발용 미리보기로 접속', exact: true }).click();
  await page.getByRole('heading', { name: '새 방송 만들기', exact: true }).waitFor();
  await page.locator('.connection').filter({ hasText: '스튜디오 연결됨' }).waitFor();
  assert.ok(authorization);
  // This isolated preview starts without connections; register through the UI.
  const menu = page.getByRole('navigation', { name: '스튜디오 메뉴' });
  const automationLink = menu.getByRole('link', { name: '자동 송출', exact: true });
  await automationLink.click();
  assert.equal(await automationLink.getAttribute('aria-current'), 'page');
  const panel = page.getByRole('region', { name: '자동 송출', exact: true });
  await panel.getByRole('heading', { name: '자동 송출', level: 1, exact: true }).waitFor();
  assert.equal(await page.locator('#broadcast-form').count(), 0);
  assert.equal(await page.locator('.studio-launch-dock').count(), 0);
  assert.equal(await page.getByRole('heading', { name: '방송 이력', exact: true }).count(), 0);
  report.checks.push('automation opens from top-level menu without manual broadcast controls');
  await panel.getByRole('button', { name: '일정 만들기', exact: true }).waitFor();
  assert.equal(await panel.getByRole('button', { name: '일정 만들기', exact: true }).count(), 1);
  await capture('empty');
  const help = panel.getByLabel('자동 송출 도움말');
  await help.focus();
  await help.press('Enter');
  const helpPopup = page.getByRole('dialog', { name: '자동 송출 이용 안내', exact: true });
  await helpPopup.waitFor();
  await helpPopup.evaluate(element => Promise.all(element.getAnimations().map(animation => animation.finished.catch(() => {}))));
  await capture('help-popup');
  await page.keyboard.press('Escape');
  await helpPopup.waitFor({ state: 'hidden' });
  await help.click();
  await helpPopup.getByRole('button', { name: '닫기', exact: true }).click();
  await helpPopup.waitFor({ state: 'hidden' });
  report.checks.push('one clear create action and keyboard/click-operable floating help popup');
  await panel.getByRole('button', { name: '일정 만들기', exact: true }).click();
  assert.equal(await panel.getByLabel('일정 이름', { exact: true }).evaluate(element => element === document.activeElement), true);
  await panel.getByLabel('일정 이름', { exact: true }).fill('매주 신제품 E2E');
  await panel.getByLabel('내 YouTube 최신 영상', { exact: true }).check();
  await panel.getByText('새 공개 영상이 있을 때만 송출해요.', { exact: true }).waitFor();
  assert.equal(await panel.getByRole('button', { name: '자동 송출 시작', exact: true }).isDisabled(), true);
  report.checks.push('channel required before latest-video schedule');
  await panel.getByLabel('보관함 영상 반복', { exact: true }).check();
  const items = await request('/media');
  const ready = items.find(item => item.status === 'ready');
  assert.ok(ready);
  await panel.getByRole('radio', { name: ready.name, exact: true }).check();
  await panel.getByRole('radio', { name: '단일 선택 확인용 영상.mp4', exact: true }).check();
  assert.equal(await panel.locator('input[name="automation-media"]:checked').count(), 1);
  assert.equal(await panel.getByRole('radio', { name: ready.name, exact: true }).isChecked(), false);
  await panel.getByRole('radio', { name: ready.name, exact: true }).check();
  assert.equal(await panel.locator('input[name="automation-media"]:checked').count(), 1);
  report.checks.push('library cards allow exactly one video selection');
  const addPlatform = panel.getByRole('button', { name: '플랫폼 추가', exact: true });
  await addPlatform.click();
  const connectionDialog = page.getByRole('dialog', { name: '송출 플랫폼 추가', exact: true });
  await connectionDialog.getByRole('heading', { name: '어느 플랫폼을 연결할까요?', exact: true }).waitFor();
  await page.keyboard.press('Escape');
  await connectionDialog.waitFor({ state: 'hidden' });
  assert.equal(await addPlatform.evaluate(element => element === document.activeElement), true);
  assert.equal(await panel.getByLabel('일정 이름', { exact: true }).inputValue(), '매주 신제품 E2E');
  await addPlatform.click();
  await connectionDialog.getByRole('button', { name: 'YouTube Live 연결 추가', exact: true }).click();
  const youtubeKey = connectionDialog.getByLabel('YouTube Live 스트림 키', { exact: true });
  await youtubeKey.fill('synthetic-browser-test-key');
  const failSave = route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Test unavailable' }) });
  await page.route('**/api/stream-connections/youtube', failSave);
  await connectionDialog.getByRole('button', { name: '연결 저장', exact: true }).click();
  await connectionDialog.getByRole('alert').waitFor();
  assert.equal(await youtubeKey.inputValue(), 'synthetic-browser-test-key');
  await page.unroute('**/api/stream-connections/youtube', failSave);
  await connectionDialog.getByRole('button', { name: '연결 저장', exact: true }).click();
  await connectionDialog.waitFor({ state: 'hidden' });
  assert.equal(await panel.getByLabel('일정 이름', { exact: true }).inputValue(), '매주 신제품 E2E');
  assert.equal(await panel.getByRole('radio', { name: ready.name, exact: true }).isChecked(), true);
  assert.equal((await request('/stream-connections')).some(item => item.target === 'youtube'), true);
  report.checks.push('empty-state registration, modal focus return, failed save retry and schedule draft preservation');
  // Additional registration remains available after the first saved platform.
  await addPlatform.click();
  await connectionDialog.getByRole('button', { name: 'Twitch 연결 추가', exact: true }).click();
  await connectionDialog.getByLabel('Twitch 스트림 키', { exact: true }).fill('synthetic-twitch-test-key');
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await connectionDialog.evaluate(element => element.scrollWidth <= element.clientWidth), true);
  await capture('add-platform-mobile');
  await connectionDialog.getByRole('button', { name: '연결 저장', exact: true }).click();
  await connectionDialog.waitFor({ state: 'hidden' });
  const twitchChoice = panel.getByRole('checkbox', { name: 'Twitch', exact: true });
  assert.equal(await twitchChoice.isChecked(), true);
  await twitchChoice.uncheck();
  await page.setViewportSize({ width: 1440, height: 1000 });
  report.checks.push('additional platform registration on mobile is auto-selected without losing the schedule');
  const youtubeChoice = panel.getByRole('checkbox', { name: 'YouTube', exact: true });
  assert.equal(await youtubeChoice.isChecked(), true);
  await youtubeChoice.uncheck();
  const youtubeCard = panel.locator('.automation-targets .channel-option').filter({ has: page.getByRole('checkbox', { name: 'YouTube', exact: true }) });
  await youtubeCard.locator('.channel-mark-youtube').waitFor();
  await youtubeCard.getByText('연결 저장됨', { exact: true }).waitFor();
  await youtubeChoice.focus();
  await youtubeChoice.press('Space');
  assert.equal(await youtubeChoice.isChecked(), true);
  await youtubeCard.getByText('선택됨', { exact: true }).waitFor();
  await youtubeChoice.press('Space');
  assert.equal(await youtubeChoice.isChecked(), false);
  await youtubeCard.getByText('선택하기', { exact: true }).waitFor();
  await youtubeCard.click();
  assert.equal(await youtubeChoice.isChecked(), true);
  report.checks.push('saved platform uses shared brand card and supports keyboard and whole-card selection');
  await panel.getByLabel('송출 시간', { exact: true }).fill('18:00');
  await panel.getByLabel('시간대', { exact: true }).selectOption({ label: '한국 시간 (서울)' });
  assert.equal(await panel.getByLabel('시간대', { exact: true }).inputValue(), 'Asia/Seoul');
  const buttons = await panel.locator('.automation-button').evaluateAll(elements => elements.map(element => element.getBoundingClientRect().height));
  assert.ok(buttons.every(height => height >= 44));
  await capture('form-desktop');
  report.checks.push('friendly timezone selection, focused creation form and 44px action targets');
  await panel.getByRole('button', { name: '자동 송출 시작', exact: true }).click();
  await panel.getByRole('heading', { name: '매주 신제품 E2E', exact: true }).waitFor();
  const stored = await request('/automations');
  const rule = stored.items.find(item => item.name === '매주 신제품 E2E');
  assert.deepEqual(rule.targets, ['youtube']);
  assert.equal(rule.media_id, ready.id);
  assert.equal(rule.timezone, 'Asia/Seoul');
  ruleId = rule.id;
  report.checks.push('form saves source, recurrence and destination in API database');
  await capture('list-desktop');
  await page.reload();
  await panel.getByRole('heading', { name: '자동 송출', level: 1, exact: true }).waitFor();
  assert.equal(await page.locator('#broadcast-form').count(), 0);
  await panel.getByRole('heading', { name: '매주 신제품 E2E', exact: true }).waitFor();
  await panel.getByRole('button', { name: '매주 신제품 E2E 일시중지', exact: true }).click();
  await panel.getByRole('button', { name: '매주 신제품 E2E 다시 시작', exact: true }).waitFor();
  assert.equal((await request('/automations')).items.find(item => item.id === ruleId).enabled, false);
  report.checks.push('reload persistence and pause');
  if (process.env.REPLAY_E2E_SCREENSHOT) await page.screenshot({ path: process.env.REPLAY_E2E_SCREENSHOT, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  report.checks.push('mobile layout has no horizontal overflow');
  await capture('list-mobile');
  await panel.getByRole('button', { name: '매주 신제품 E2E 다시 시작', exact: true }).click();
  await panel.getByRole('button', { name: '매주 신제품 E2E 일시중지', exact: true }).waitFor();
  await panel.getByRole('button', { name: '매주 신제품 E2E 삭제', exact: true }).click();
  await panel.getByRole('heading', { name: '매주 신제품 E2E', exact: true }).waitFor({ state: 'detached' });
  assert.equal((await request('/automations')).items.some(item => item.id === ruleId), false);
  ruleId = undefined;
  report.checks.push('resume and delete persisted');
  // A fresh visit uses the bookmarkable automation route; other views remain reachable.
  await page.getByRole('link', { name: 'Replay Live', exact: true }).click();
  await page.getByRole('heading', { name: '새 방송 만들기', exact: true }).waitFor();
  assert.equal(await panel.count(), 0);
  await automationLink.click();
  await panel.getByRole('heading', { name: '자동 송출', level: 1, exact: true }).waitFor();
  await panel.getByRole('button', { name: '일정 만들기', exact: true }).click();
  await panel.getByLabel('일정 이름', { exact: true }).fill('예약 실행 E2E');
  await panel.getByRole('radio', { name: ready.name, exact: true }).check();
  await panel.getByRole('checkbox', { name: 'YouTube', exact: true }).check();
  const due = new Date(Date.now() + 120000);
  await panel.getByLabel('송출 시간', { exact: true }).fill(due.toISOString().slice(11, 16));
  await panel.getByLabel('시간대', { exact: true }).selectOption('UTC');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await capture('form-mobile');
  report.checks.push('mobile creation form has no horizontal overflow');
  await panel.getByRole('button', { name: '자동 송출 시작', exact: true }).click();
  await panel.getByRole('heading', { name: '예약 실행 E2E', exact: true }).waitFor();
  ruleId = (await request('/automations')).items.find(item => item.name === '예약 실행 E2E').id;
  // Wait for real wall-clock scheduling, API tick, job queue and local worker rejection.
  // The preview deliberately rejects external streaming; never send a synthetic key to a platform.
  const deadline = Date.now() + 180000;
  let executed;
  while (Date.now() < deadline) {
    executed = (await request('/automations')).items.find(item => item.id === ruleId);
    if (executed.history[0]?.state === 'failed') break;
    await new Promise(resolve => setTimeout(resolve, 3000));
  }
  assert.equal(executed.history.length, 1);
  assert.equal(executed.history[0].state, 'failed');
  assert.equal(executed.history[0].error_code, 'BROADCAST_FAILED');
  const broadcasts = (await request('/broadcasts')).filter(item => item.media_id === ready.id && item.target === 'youtube');
  assert.equal(broadcasts.length, 1, 'scheduler must enqueue exactly one broadcast');
  assert.equal(broadcasts[0].error_code, 'PREVIEW_EXTERNAL_OUTPUT_FORBIDDEN');
  await panel.getByText('최근 결과: 실행 실패', { exact: true }).waitFor({ timeout: 25000 });
  await panel.getByText('최근 결과: 실행 실패', { exact: true }).click();
  await panel.getByText('방송 이력에서 실패한 플랫폼을 확인해 주세요.', { exact: true }).waitFor();
  report.checks.push('scheduled tick enqueues one job and surfaces local external-output rejection');
  await panel.getByRole('button', { name: '예약 실행 E2E 삭제', exact: true }).click();
  await panel.getByRole('heading', { name: '예약 실행 E2E', exact: true }).waitFor({ state: 'detached' });
  ruleId = undefined;
  assert.deepEqual(errors, []);
  report.passed = true;
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  console.error(await page.locator('.automation-page').innerText().catch(() => 'No automation panel'));
  throw error;
} finally {
  if (ruleId) await request('/automations/' + ruleId, { method: 'DELETE' }).catch(() => {});
  if (authorization) {
    await request('/stream-connections/youtube', { method: 'DELETE' }).catch(() => {});
    await request('/stream-connections/twitch', { method: 'DELETE' }).catch(() => {});
  }
  await browser.close();
}
