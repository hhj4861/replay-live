// The central broker must explicitly trust this repository and dedicated key.
// Credentials live only in this process and the deployment child's environment.
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const broker = 'https://cak-credential-broker.guswhd1085.workers.dev';
const credential = 'REPLAY_DEPLOY_VERCEL_TOKEN';
const mask = value => console.log(`::add-mask::${value.replaceAll('%', '%25').replaceAll('\r', '%0D').replaceAll('\n', '%0A')}`);

export async function deploymentCredential(env, fetcher = fetch, redact = mask) {
  if (env.GITHUB_REPOSITORY !== 'hhj4861/replay-live' || env.GITHUB_REPOSITORY_ID !== '1365111099'
      || env.GITHUB_REPOSITORY_OWNER_ID !== '71001056' || env.GITHUB_REF !== 'refs/heads/deploy/replay'
      || env.GITHUB_REF_PROTECTED !== 'true' || !/^[a-f0-9]{40}$/.test(env.GITHUB_SHA || '')
      || !['push', 'workflow_dispatch'].includes(env.GITHUB_EVENT_NAME)) throw new Error('RELEASE_IDENTITY_REQUIRED');
  const oidc = new URL(env.ACTIONS_ID_TOKEN_REQUEST_URL);
  if (oidc.protocol !== 'https:' || !oidc.hostname.endsWith('.actions.githubusercontent.com')
      || oidc.username || oidc.password || oidc.hash || !env.ACTIONS_ID_TOKEN_REQUEST_TOKEN) throw new Error('INVALID_OIDC_ENDPOINT');
  oidc.searchParams.set('audience', 'cak-cloudflare-secrets');
  const response = await fetcher(oidc, {
    headers: { Authorization: `Bearer ${env.ACTIONS_ID_TOKEN_REQUEST_TOKEN}` },
    redirect: 'error', signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error('GITHUB_OIDC_UNAVAILABLE');
  const { value } = await response.json();
  if (typeof value !== 'string' || !value || value.length > 32000) throw new Error('GITHUB_OIDC_INVALID');
  redact(value);
  const result = await fetcher(`${broker}/github/secrets`, {
    method: 'POST', headers: { Authorization: `Bearer ${value}`, 'Content-Type': 'application/json' },
    body: '{}', redirect: 'error', signal: AbortSignal.timeout(20000),
  });
  if (!result.ok) throw new Error('REPLAY_BROKER_AUTHORIZATION_REQUIRED');
  const body = await result.json();
  if (!body.values || Object.keys(body.values).length !== 1
      || typeof body.values[credential] !== 'string' || !body.values[credential]
      || /[\r\n\0]/.test(body.values[credential])) throw new Error('REPLAY_BROKER_SCOPE_INVALID');
  redact(body.values[credential]);
  return body.values[credential];
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const token = await deploymentCredential(process.env);
    const result = spawnSync('python', ['scripts/deploy-production.py', '--execute', '--report', 'release-artifacts/production.json'],
      { env: { ...process.env, VERCEL_TOKEN: token }, stdio: 'inherit' });
    process.exitCode = result.status ?? 1;
  } catch {
    console.error('Replay deployment credential unavailable; verify the dedicated broker policy and key. Secret values suppressed.');
    process.exitCode = 1;
  }
}
