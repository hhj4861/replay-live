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
