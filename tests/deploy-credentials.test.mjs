import { test } from 'node:test';
import assert from 'node:assert/strict';
import { deploymentCredential } from '../scripts/deploy-credentials.mjs';

const env = {
  GITHUB_REPOSITORY: 'hhj4861/replay-live', GITHUB_REPOSITORY_ID: '1365111099',
  GITHUB_REPOSITORY_OWNER_ID: '71001056', GITHUB_REF: 'refs/heads/deploy/replay',
  GITHUB_REF_PROTECTED: 'true', GITHUB_SHA: 'a'.repeat(40), GITHUB_EVENT_NAME: 'push',
  ACTIONS_ID_TOKEN_REQUEST_URL: 'https://test.actions.githubusercontent.com/oidc',
  ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'synthetic-request',
};

test('OIDC requests exactly the Replay credential without writing an environment file', async () => {
  const calls = [], masked = [];
  const token = await deploymentCredential(env, async (url, init) => {
    calls.push({ url: String(url), init });
    return Response.json(calls.length === 1 ? { value: 'synthetic-oidc' }
      : { values: { REPLAY_DEPLOY_VERCEL_TOKEN: 'synthetic-vercel' } });
  }, value => masked.push(value));
  assert.equal(token, 'synthetic-vercel');
  assert.equal(new URL(calls[0].url).searchParams.get('audience'), 'cak-cloudflare-secrets');
  assert.equal(calls[1].url, 'https://cak-credential-broker.guswhd1085.workers.dev/github/secrets');
  assert.equal(calls[1].init.headers.Authorization, 'Bearer synthetic-oidc');
  assert.ok(calls.every(call => call.init.redirect === 'error'));
  assert.deepEqual(masked, ['synthetic-oidc', 'synthetic-vercel']);
});

test('wrong repository, main, pull request and unprotected branches cannot request credentials', async () => {
  for (const patch of [{ GITHUB_REF: 'refs/heads/main' }, { GITHUB_REPOSITORY_ID: 'another' },
    { GITHUB_REPOSITORY_OWNER_ID: 'another' }, { GITHUB_EVENT_NAME: 'pull_request' },
    { GITHUB_REF_PROTECTED: 'false' }, { GITHUB_SHA: '' }]) {
    await assert.rejects(deploymentCredential({ ...env, ...patch }, () => assert.fail('must not contact broker')), /RELEASE_IDENTITY_REQUIRED/);
  }
});

test('unrelated credentials and rejected broker policies fail closed', async () => {
  for (const values of [{ DEPLOY_VERCEL_TOKEN: 'unrelated' },
    { REPLAY_DEPLOY_VERCEL_TOKEN: 'test', OTHER_SECRET: 'unrelated' }, {},
    { REPLAY_DEPLOY_VERCEL_TOKEN: 'unsafe\nvalue' }]) {
    let count = 0;
    await assert.rejects(deploymentCredential(env, async () => Response.json(++count === 1 ? { value: 'oidc' } : { values }), () => {}),
      /REPLAY_BROKER_SCOPE_INVALID/);
  }
  let count = 0;
  await assert.rejects(deploymentCredential(env, async () => ++count === 1 ? Response.json({ value: 'oidc' })
    : new Response('sensitive upstream error', { status: 403 }), () => {}), /REPLAY_BROKER_AUTHORIZATION_REQUIRED/);
});
