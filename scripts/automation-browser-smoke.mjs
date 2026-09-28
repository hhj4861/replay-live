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
let ruleId;
try {
  await page.goto(web);
  await page.getByRole('button', { name: '개발용 미리보기로 접속', exact: true }).click();
  await page.getByRole('heading', { name: '새 방송 만들기', exact: true }).waitFor();
  await page.locator('.connection').filter({ hasText: '스튜디오 연결됨' }).waitFor();
  assert.ok(authorization);
  // Save through the actual UI, including local automation's account storage selection.
  await page.locator('#platform-youtube').click();
  await page.locator('#key-youtube').fill('synthetic-browser-test-key');
  await page.getByRole('button', { name: /YouTube.*연결 저장/ }).click();
  await page.getByText('내 계정에 저장됨', { exact: true }).waitFor();
  assert.equal((await request('/stream-connections'))[0].target, 'youtube');
  report.checks.push('platform saved from UI to account storage');
  const panel = page.getByRole('region', { name: '자동 송출', exact: true });
  await panel.locator('.automation-heading').click();
  await panel.getByRole('button', { name: '일정 만들기', exact: true }).click();
  await panel.getByLabel('일정 이름', { exact: true }).fill('매주 신제품 E2E');
  await panel.getByLabel('내 YouTube 최신 영상', { exact: true }).check();
  await panel.getByText('새 공개 영상이 있을 때만 송출해요.', { exact: true }).waitFor();
  assert.equal(await panel.getByRole('button', { name: '자동 송출 시작', exact: true }).isDisabled(), true);
  report.checks.push('channel required before latest-video schedule');
  await panel.getByLabel('보관함 영상 반복', { exact: true }).check();
  const items = await request('/media');
  const ready = items.find(item => item.status === 'ready');
  assert.ok(ready);
  await panel.getByLabel('보관함 영상', { exact: true }).selectOption(ready.id);
  await panel.getByRole('checkbox', { name: 'YouTube', exact: true }).check();
  await panel.getByLabel('송출 시간', { exact: true }).fill('18:00');
  await panel.locator('input[list=automation-zones]').fill('Asia/Seoul');
  await panel.getByRole('button', { name: '자동 송출 시작', exact: true }).click();
  await panel.getByRole('heading', { name: '매주 신제품 E2E', exact: true }).waitFor();
  const stored = await request('/automations');
  const rule = stored.items.find(item => item.name === '매주 신제품 E2E');
  assert.deepEqual(rule.targets, ['youtube']);
  assert.equal(rule.media_id, ready.id);
  assert.equal(rule.timezone, 'Asia/Seoul');
  ruleId = rule.id;
  report.checks.push('form saves source, recurrence and destination in API database');
  await page.reload();
  await panel.locator('.automation-heading').click();
  await panel.getByRole('heading', { name: '매주 신제품 E2E', exact: true }).waitFor();
  await panel.getByRole('button', { name: '매주 신제품 E2E 일시중지', exact: true }).click();
  await panel.getByRole('button', { name: '매주 신제품 E2E 다시 시작', exact: true }).waitFor();
  assert.equal((await request('/automations')).items.find(item => item.id === ruleId).enabled, false);
  report.checks.push('reload persistence and pause');
  if (process.env.REPLAY_E2E_SCREENSHOT) await panel.screenshot({ path: process.env.REPLAY_E2E_SCREENSHOT });
  await page.setViewportSize({ width: 390, height: 844 });
  await panel.scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  report.checks.push('mobile layout has no horizontal overflow');
  await panel.getByRole('button', { name: '매주 신제품 E2E 다시 시작', exact: true }).click();
  await panel.getByRole('button', { name: '매주 신제품 E2E 일시중지', exact: true }).waitFor();
  await panel.getByRole('button', { name: '매주 신제품 E2E 삭제', exact: true }).click();
  await panel.getByRole('heading', { name: '매주 신제품 E2E', exact: true }).waitFor({ state: 'detached' });
  assert.equal((await request('/automations')).items.some(item => item.id === ruleId), false);
  ruleId = undefined;
  report.checks.push('resume and delete persisted');
  assert.deepEqual(errors, []);
  report.passed = true;
  console.log(JSON.stringify(report, null, 2));
} catch (error) {
  console.error(await page.locator('.automation-panel').innerText().catch(() => 'No automation panel'));
  throw error;
} finally {
  if (ruleId) await request('/automations/' + ruleId, { method: 'DELETE' }).catch(() => {});
  if (authorization) await request('/stream-connections/youtube', { method: 'DELETE' }).catch(() => {});
  await browser.close();
}
