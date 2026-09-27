# Import interruption recovery and MP4 preparation

## Observed production failure

On 2026-09-27 the user confirmed the same YouTube recording, `GcOe4ILS6Ow`,
which had succeeded in the earlier production browser test. The new import
`b66b1b7995dc4eb6951a17dfa5282cb9` failed with `SOURCE_INCOMPLETE` after
approximately 27.37 seconds, on its first job attempt.

That code can mean an HTTP response ended before its declared Content-Length,
or the downloaded recording is shorter than the platform/manifest declares.
The deployed revision did not record which condition failed. The precise cause
of this production occurrence is **not established**, and these changes alone
are not evidence that the production issue is resolved.

## Changes

- Recover once from an incomplete response or transient transport failure. The
  recovery allowance is shared across the entire import, not reset per segment.
  A partial segment is truncated back to its original offset before retry, so
  completed earlier segments remain intact without duplicated media bytes.
- Failed transfer bytes still count against the same cumulative limits. Retries
  preserve deadline, cancellation, request, wire, metadata and media budgets.
  Redirect/DNS/TLS/proxy restrictions are rechecked normally. Metadata POSTs,
  denied access, rate limits and media-completeness failures are not retried.
- Copy H.264/yuv420p video and AAC audio rather than encoding them again. Convert
  only incompatible tracks. Keep the existing duration, output and decode checks;
  no short preview or missing segment is accepted as a complete recording.
- Emit numeric stage timings for metadata, download, MP4 preparation and checksum.
  Incomplete-media diagnostics distinguish short HTTP bodies from short duration.
  Logs exclude source URLs, response content, proxy credentials and user identity.

## Verification

Deterministic network tests exercise interrupted transfers, shared retry limits,
byte/deadline enforcement, cancellation, redirect revalidation and status errors.
A real generated MP4 goes through interrupted download, recovery, remux and
checksum. Packet hashes verify that compatible video/audio are preserved;
incompatible codecs still convert to H.264/AAC and outputs pass FFmpeg decoding.
These are local tests, not a new real YouTube/proxy/cloud browser E2E.

The complete local Python run finished with 1,019 passed, 24 skipped and one
failure: the execution sandbox denied a loopback TCP bind in the pre-existing
proxy-budget test. Rerunning that test file with loopback permission passed both
tests (including the previously failing test). No test was weakened or disabled.
The 24 environment-dependent skips remain unverified by this local run.

A local comparison used the original `7c8e673` implementation and this revision
on the same generated 17-second 360x640 H.264/yuv420p/AAC recording, alternating
three runs each. MP4 preparation times in seconds:

| Implementation | Runs | Median |
| --- | --- | --- |
| Original | 10.841, 5.750, 4.712 | 5.750 |
| Compatible track copying | 4.838, 2.270, 4.457 | 4.457 |

All six outputs decoded successfully. Host contention caused substantial
variation; these measurements exclude proxy download, worker startup, storage
upload and browser polling. They do not establish production end-to-end latency.

## Rollout boundary

This fix requires PR-specific `fix → develop → main` approval, a fresh worker
snapshot and deployment. It is not applied to production by committing this file.
After rollout, retest the user's exact recording through the authenticated site,
verify stored playback and inspect the new stage/reason diagnostics if it fails.
Report production recovery only after that verification succeeds.
