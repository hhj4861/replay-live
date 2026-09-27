# Remove per-video import limits

The operator requested removal of both the two-minute duration limit and the
50 MiB per-file limit. The existing 256 MiB account library quota is unchanged.

## Behavior

- `REPLAY_MAX_DURATION=0` disables the per-recording duration policy during
  metadata extraction, segmented downloading, MP4 preparation, full decoding
  and publication. Positive limits still work. Invalid/non-finite durations,
  live streams, playlists, invalid codecs, private addresses and unauthorized
  sources remain rejected.
- `REPLAY_MAX_UPLOAD_BYTES=0` removes the per-file policy. Link imports reserve
  the account's remaining space atomically. Direct file uploads reserve their
  declared size. Concurrent requests cannot overbook the account.
- The UI removes the hardcoded two-minute/50 MB labels and displays “보관함의
  남은 공간까지”. Existing failed tasks retain their history; users retry them
  after deployment. Old duration failures are described as the prior policy.
- These are recording-admission settings, not unlimited compute or storage.
  Account quotas, finite download/validation deadlines, leases, daily runtime
  budgets and broadcast/output capacity remain enforced. The R2 adapter supports
  objects up to its technical 5 GiB single-copy ceiling; the current account
  quota is much smaller. Existing optional PC helpers keep their own legacy
  transfer limits; the primary server import route does not use them.

## Large R2 objects

Objects over 64 MiB use 32 MiB requests, below the edge ingress ceiling. Only
the authenticated control plane can allocate a multipart upload. Its signed
capability binds the final tenant-scoped key, temporary staging key, upload ID,
size, type, whole-file checksum and expiry. It cannot create another upload.

The browser and isolated server worker upload consecutive bounded parts and
preserve opaque R2 ETags. Completion validates the exact part list and size,
then streams the staged object into the immutable final key using R2's SHA-256
verification and conditional non-overwrite. A multipart ETag is never treated
as a whole-file checksum. Temporary completed staging objects are deleted;
failed/cancelled clients abort their upload. Incomplete uploads whose client
disappears rely on R2's incomplete-multipart lifecycle cleanup. No lifecycle
rule, bucket, subscription or billing setting is changed by this PR.

There can be a temporary staging copy during completion, so transient provider
storage can exceed the final logical library usage. Publication and reservation
settlement happen only after the final object's size/hash/type are verified.

## Validation

- Full local Python regression: 1,030 passed, 25 skipped (explicit PostgreSQL
  environment absent). Dedicated tests validate a real 2,199-second MP4,
  >50 MiB worker multipart transfers, cancellation, and atomic remaining quota.
- Client tests: 227 passed; lint, TypeScript and commercial build passed.
- Local R2 integration: successful 65 MiB multipart assembly, full checksum,
  retry-safe completion and range playback; bad checksums never publish.
- `scripts/large-upload-headless.mjs` uploads a valid 36:39, 65 MiB MP4 from
  headless Chrome through three actual HTTP PUTs into a local R2 binding,
  verifies its SHA-256 and plays its ending through a range request. Account
  and catalog API responses are synthetic. It uses no production credentials,
  YouTube traffic or proxy quota, and closes its browser/servers afterward.
- The existing real production PR #28 test succeeded before these limit
  changes: application-session connection 1,764 ms, reloads 543/526 ms,
  YouTube import 42,614 ms, complete R2 playback, zero helper calls and no HTTP
  errors. Stored usage stayed 7.1 MiB while 50 MiB was reserved, then settled
  to 8.2 MiB. This is not evidence that the new limits are deployed or that
  download latency improved. Its temporary session was revoked and both
  build/job Sandboxes were confirmed stopped.

## Rollout after PR approval

1. Deploy the backward-compatible R2 storage Worker first. Small PUT/GET
   capabilities issued by the preceding release continue to work.
2. Build and verify a new immutable media-worker snapshot from the merged
   release. Deploy the matching API and web/dispatcher version together.
3. Set only the relevant production settings: `REPLAY_MAX_DURATION=0`,
   `REPLAY_MAX_UPLOAD_BYTES=0`, `REPLAY_VALIDATION_TIMEOUT=1800`,
   `REPLAY_WORKER_MAX_SECONDS=3600`. Keep the account's existing 256 MiB quota,
   keys and data. Verify the flags and perform bounded production checks.

This work does not complete the unrelated full Cloudflare hosting migration;
the current production API/web and isolated download workers remain on Vercel,
with private video storage on Cloudflare R2.
