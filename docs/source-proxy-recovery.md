# Proxy connection recovery

## Observed production failure

On 2026-09-30, import `79ce886df91b439dbf14e09fbe7c780b` ran release
`8506ae5b14b47bf00dd845c4e08b286ee4c04fdb` for the user-provided video
`https://youtu.be/SzrcusiORCI`. The worker failed after 115,844 ms from claim
with `SOURCE_PROXY_UNAVAILABLE`, download-stage `proxy_rejected`, HTTP 502,
and one session rotation. Source processing took 107,125 ms.
This is a failed import, not a successful speed measurement. The final failure
came from the proxy CONNECT response before destination TLS. It does not prove
whether the egress peer, destination filtering, or network path caused it.

## Recovery change

- Reuse fully consumed HTTP/1.1 200 connections between parallel fragments of
  the same hostname and the same proxy session. Four workers exclusively own
  their checked-out connections. Drop idle tunnels to other hosts before opening
  replacements and close the entire idle pool after the fragment workers join.
- Never pool redirects, unread or partial responses, failed requests, or responses
  that require connection closure. URL validation, public DNS checks, destination
  allowlists, TLS verification, and header filtering still apply to every request.
- Retry transient proxy GET/HEAD metadata and media requests up to twice on the
  same session, with 0.2 / 0.4 second cancellable backoff. Retry CONNECT 502/503/504,
  interrupted responses and network errors; do not retry access denial, auth,
  quota, TLS, cancellation, or exhausted budgets. POST is not locally retried.
- Across the whole import, at most eight additional request retries are allowed,
  including after a session rotation. Failed/partial bytes, wire bytes, requests,
  and time remain charged to the original limits. Completed fragments remain in
  place during request recovery; only the failed fragment is truncated and retried.
- If short recovery fails, retain the existing one-time session rotation with fresh
  metadata and signed URLs. Do not carry partially downloaded files across IPs.
- Persist optional bounded `request_retries` in failure callbacks so it remains
  available after worker cleanup; `retries` still counts whole-import recovery.

## Validation and limits

Tests use actual local HTTP/1.1 keep-alive sockets with synthetic proxy CONNECT
failures and a real FFmpeg fixture. They cover ordered fragments, partial/stale
connections, request caps, cancellation, budgets, origin isolation, library and
preview success, and persistent-failure evidence after cleanup. No paid proxy
traffic or production configuration is changed by these tests.

This reduces repeated handshakes and recovers transient failures. It cannot make
an unreachable destination or persistently broken proxy work. Production success
and latency for the same video must be measured after separately approved release.

Reference: [DataImpulse error meanings](https://docs.dataimpulse.com/errors).

## Recovery verification (2026-10-01)

The current project checkout is `/Users/admin/workSpace/replay-live`; the previous
iCloud checkout is preserved. This fix was recovered into the separate worktree
`/Users/admin/workSpace/replay-live-worktrees/proxy-connection-reuse`.

After recovery, all 292 tests passed in the nine-module source-download, proxy,
worker and media-import regression selection (65.84 seconds). The two warnings
are existing Starlette/httpx and AnyIO deprecations. This is local verification;
it does not establish production download success.

### CI dependency audit follow-up

The first PR CI failed its dependency audit on urllib3 2.7.0 and PyJWT 2.14.0.
Upgrade only these packages to urllib3 2.8.0 and PyJWT 2.15.0 across the runtime
pins, lock and input constraints. No audit exemptions or CI checks were removed.

After the upgrade, dependency compatibility passed, pip-audit 2.9.0 reported no
known vulnerabilities, and 510 source/proxy, authentication, storage, public
security and preflight tests passed in 64.55 seconds.

Release references: [urllib3 2.8.0](https://github.com/urllib3/urllib3/releases/tag/2.8.0),
[PyJWT 2.15.0](https://github.com/jpadilla/pyjwt/releases/tag/2.15.0).

## Provider failure classification (2026-10-01)

CONNECT responses previously retained only HTTP status. Recognize exact documented
DataImpulse status-line identifiers and persist an optional allowlisted `proxy_error`
in the existing failure callback. Never retain arbitrary reason phrases, headers,
response bodies, source URLs, egress IPs or credentials. Unknown or mismatched
identifiers remain generic; HTTP status alone must not be labelled as exhausted quota.

Customer errors now distinguish destination connection failure, no available peer,
concurrent connection limits, exhausted traffic, account/configuration errors and
proxy access restrictions. Only the existing transient 502/503/504 recovery policy
retries and rotates; quota, authentication and restrictions still stop immediately.
The UI hides the same-link retry for known quota/configuration/access failures and
keeps file upload available. This introduces no new provider, purchase or DB migration.

A read-only production log query returned no events during this investigation.
That is insufficient to classify a new user failure or establish a production
success rate. The historic 502 is still only proven at CONNECT, not attributed to
an exact provider subreason. New classification needs an actual provider status-line
identifier in a subsequent import after deployment.

Library media can already be reused for broadcasts without another download.
Automatically matching a newly pasted URL to earlier media is not implemented in
this diagnostic change; it needs a tenant-scoped retained source identity, including
correct deletion, changed-name and retention behavior.

Validation for provider classification:
- 38 new tests passed for documented CONNECT reasons, unknown/mismatched messages,
  retry/session bounds, callback validation, worker cleanup and released quota.
- The existing 256 source/diagnostic/proxy/worker tests passed unchanged.
- Web lint, TypeScript and the commercial build passed.
- The actual built UI passed eight headless Chromium cases using synthetic local
  auth/API fixtures: quota/configuration/access/bot failures hide same-link retry;
  connect/no-peer/busy/unknown failures retain it. File upload remains available,
  one import request is made, and no JavaScript errors occur in every case.
- No production cookies, paid proxy traffic or real broadcast was used in tests.

To reproduce the browser check, build with `REPLAY_COMMERCIAL=1`,
`REPLAY_API_URL=http://127.0.0.1:18193`,
`REPLAY_OIDC_AUTHORITY=https://synthetic.example`, and
`REPLAY_OIDC_CLIENT_ID=synthetic-client`, then run
`node scripts/proxy-failure-browser-smoke.mjs <build-directory>`.
Set `REPLAY_PLAYWRIGHT_MODULE` and optionally `REPLAY_CHROME_PATH` to installed
local packages/binaries; the script always runs headless and blocks external URLs.

## Delayed recovery and customer progress (2026-10-01)

After the existing short request retries and first immediate proxy session change
fail, wait five seconds and make one final fresh-session attempt. The maximum is
three sessions total (two rotations), with the existing eight extra request retries,
original deadline, request count and byte budgets shared across all sessions.
The wait is cancellable and does not extend a lease or storage reservation beyond
its existing deadline. Authentication, exhausted quota, access restrictions and
TLS failures do not enter this recovery path. Metadata and signed media URLs are
fetched anew after each session change; cross-session partial download resume is
not supported. A persistent failure remains terminal after this final attempt.

The worker reports recovery through its authenticated lease heartbeat. The existing
media error-code column temporarily holds SOURCE_RECOVERING only while importing;
the public API exposes this as recovering=true with no failure code. Lease validation,
tenant matching and the importing-state condition protect the update. No schema
migration is required. Normal downloading clears the phase, and terminal completion
or failure replaces it and releases the reservation under the existing policy.
The UI shows “연결 복구 중”, keeps the import button disabled, and recovers that state
from the server after refresh. It still selects the prepared video when ready.

This is bounded recovery using the existing provider, not a backup provider or an
unlimited background retry queue. Verification uses simulated 502s, actual local
media preparation/storage/preview and headless UI fixtures; no paid proxy traffic
or production release is implied by local tests.

Validation of this change:
- 341 related Python tests passed (159 recovery/worker tests and 182 repository,
  API, source and parallel-transfer tests). The old two-session expectation was
  updated to the new three-session limit and revalidated.
- 231 client tests, lint, TypeScript and the commercial build passed.
- Ten headless scenarios passed, including recovery followed by success or final
  failure across a page refresh, exactly one import POST, and zero JavaScript errors.
- The recovery integration test injects two failed sessions, measures at least
  five seconds of waiting, then verifies real local MP4 storage and preview.
- Cancellation/deadline tests prevent a third connection; stale callbacks cannot
  restore recovery after terminal failure. Tenant and reservation checks remain intact.

### Image-build CI follow-up

PR #57's push check twice failed on a `files.pythonhosted.org` socket read timeout
while downloading pinned packages (API image first, media image on rerun). The same
commit's PR check and both application verification runs passed. Both image builds
now explicitly allow 60 seconds per socket read, retaining five connection attempts.
Package pins, TLS verification and all CI gates remain unchanged. Validate the exact
image builds in the hosted Cloudflare check before merging.
