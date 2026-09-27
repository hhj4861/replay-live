# Studio loading and storage accounting

## Changes

- Normal Google application-session authentication now uses one joined, read-only
  SQL statement instead of five PostgreSQL round trips and mutation locks. Each
  request still checks current committed session expiry, retirement, family
  revocation, enabled account, allowed-account policy, roles and tenant. Refresh,
  logout and administrative mutations keep their existing locking behavior.
- Media, usage and broadcast history render independently after `/me` confirms
  the account. A completed import no longer waits for slower history/catalog
  responses before appearing as ready.
- Stream targets, source catalog and limits are cached for 60 seconds within the
  current browser session. Account, media, jobs and usage are never cached here.
  Logout invalidates the cache and fences late responses. Routine polling now
  uses four API requests instead of seven while configuration is cached.
- The storage total previously included download/output reservations. For
  example, 5.9 MiB of files plus a 50 MiB import reservation displayed 55.9 MiB,
  then fell back when the reservation was released. The main figure now shows
  stored bytes; an adjacent label shows temporary reservations. Backend quota
  enforcement and available-space calculations are unchanged.
- Duration failures include the configured maximum. The reported YouTube video
  `lUbkOv4gHys` is 2,199 seconds (36:39); production permits 120 seconds. This
  failure is expected and is separate from download truncation. No upload,
  duration, storage or proxy budget limit is increased by this change.

## Verification

- Google authentication, API and member-management tests: 82 passed locally.
- Client unit tests: 224 passed; lint, TypeScript and commercial build passed.
- `scripts/studio-performance-headless.mjs`: actual headless Chromium against
  the built application with synthetic API responses. Both successful and
  failed imports pass: connection/usage render while catalog is blocked,
  repeated polls reuse configuration, 50 MiB reservation does not inflate the
  stored-file figure, and media status renders while history is blocked.
- A PostgreSQL regression holds account mutation and Python locks while an
  independent authentication read must complete within five seconds; a later
  committed logout must immediately reject the same session. This runs in CI
  against its explicit disposable `replay_test` database, not production.

Run the browser regression after a Google-mode build:

```sh
cd web
REPLAY_COMMERCIAL=1 REPLAY_LOGIN_PROVIDER=google \
  REPLAY_GOOGLE_CLIENT_ID=1234567890-synthetictest.apps.googleusercontent.com \
  npm run build:vercel
cd ..
REPLAY_PLAYWRIGHT_MODULE=/path/to/playwright node scripts/studio-performance-headless.mjs
```

The UI test uses no real account, Google OAuth exchange, proxy or media download.
It validates response ordering rather than claiming a production speedup from
synthetic timings. Production performance must be measured after deployment.

## Deployment boundary

The earlier retry/remux change (PR #26, main `f08aa1c`) was deployed as
`rpl-f08aa1c5b2bc525c`. This document's authentication/storage/UI changes are
separate and require their own PR review and deployment.
