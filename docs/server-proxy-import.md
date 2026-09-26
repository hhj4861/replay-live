# Shared server imports and proxy quota notifications

Status: implemented on `feat/server-youtube-probe`; not merged or deployed to production.
The official personal-plan balance API and the intended Telegram bot/recipient
were verified. A real test message was delivered and server secrets were saved;
monitoring is not enabled on the current production deployment.

## User flow

Google/OIDC sign-in → studio → video link → `POST /api/media/imports` → tenant-owned
import job → isolated server worker → YouTube through the shared DataImpulse plan →
H.264/AAC MP4 → existing private object storage → that user's library and preview.
No PC helper, loopback request, installation or browser file upload is used for link imports.
The shared proxy quota does not grant access to another user's media, source URLs or jobs.
An uncertain import acknowledgement reuses the same idempotency key on retry.

The studio previously awaited media, jobs, account, usage, two catalogs **and** an
object-storage/worker health probe before showing connected. Account verification now
updates independently. Authenticated `/api/limits` reads configuration without object
storage access. Health runs separately every 30 seconds instead of every 3 seconds.
Broadcast readiness still requires a successful health check; a successful login is
not presented as proof that the broadcast worker is ready.

## Download boundary

- Only YouTube import processes receive `REPLAY_SOURCE_PROXY_URL` in their runtime
  environment. It is not written to the snapshot, job JSON, frontend or logs.
- Accept only `http://<plan-login>:<plan-password>@gw.dataimpulse.com:823` and keep one
  residential session for metadata, signatures and media. Gateway CONNECT may only
  target `youtube.com`, `googlevideo.com`, `ytimg.com` and their subdomains on 443.
- TLS verification remains end-to-end. Each redirect is validated again. Other
  providers/direct MP4 retain the existing public IPv4, DNS-pinned direct transport.
- yt-dlp uses built-in extractors and the project's bounded HTTP handler; bundled
  `yt-dlp-ejs==0.8.0` and Node handle signatures with remote components disabled.
- Existing duration, byte, request, disk/output and deadline limits remain active.
  Import processing checks lease cancellation throughout download and FFmpeg work.
  VP9/AV1/Opus inputs are converted locally to H.264/AAC; FFmpeg only opens local binary
  demuxers, never a network URL or text playlist.
- A production YouTube worker without the proxy fails closed. It never silently
  falls back to the blocked direct-cloud route.

## Quota alerts

DataImpulse traffic has no expiry. Monitor actual **remaining bytes**, not a renewal
date or completed-video sizes. Support confirmed the official User API reference:
`GET https://gw.dataimpulse.com:777/api/stats`, Basic authentication with the plan's
existing proxy login/password. The reader validates `status`, plan identity and
numeric traffic fields, then uses `traffic_left` in bytes. It never substitutes the
reseller deposit balance, estimates from MP4 size, or zero on a failed response.
The fixed HTTPS endpoint rejects redirects, has a five-second timeout and limits
the response to 16 KiB. Error logs never retain provider bodies or credentials.

The existing dispatcher observes balance before claiming a notice. Queue mode
keeps an idle follow-up alive every five minutes (up to 30 seconds of queue rounding);
earlier media jobs still take priority. Existing daily recovery cron and deployment
version fencing remain active. Minute-cron deployments poll on their existing tick.
Provider/Telegram failures retry on a subsequent tick without blocking media work.

`POST /internal/proxy-balance` accepts a recent numeric balance only with the existing
control credential. `replay_proxy_balance` stores a single shared balance and the
1,000,000,000/250,000,000-byte notification latches. Repeated observations do not emit
repeated alerts. A confirmed refill above a threshold +10% rearms that threshold and
retires obsolete pending notices. Old/out-of-order observations are ignored.

`/internal/proxy-alerts/claim` leases a pending alert for five minutes. Only the lease
holder can acknowledge successful delivery. Telegram messages include the shared
remaining quota and a button to `https://app.dataimpulse.com/plans`; no payment or
auto-recharge is performed. Telegram does not offer a sendMessage idempotency key:
if delivery succeeds but its acknowledgement is lost, a later retry can duplicate
that message. Normal polls and concurrent dispatchers are deduplicated durably.

Server-only configuration:

- `REPLAY_SERVER_IMPORTS=1`: allow production server imports.
- `REPLAY_SOURCE_PROXY_URL`: existing plan proxy credentials; dispatcher/runtime only.
- `REPLAY_TELEGRAM_BOT_TOKEN`, `REPLAY_TELEGRAM_CHAT_ID`: the selected private admin chat.
- `REPLAY_PROXY_MONITOR_ENABLED=1`: enable quota polling/delivery and idle queue follow-ups.

The intended bot is `@replay_proxy_account_bot`. A localhost password form validates
the bot and waits for a one-time `/start` code to identify its recipient. Token/chat
secrets are stored in Vercel's server-only secret settings only after that validation.
Do not treat a submitted form as successful delivery; verify the sendMessage result.
`docs/server-proxy-quota-evidence.json` records the real reader returning
5,363,037,539 remaining bytes and the successful bot delivery, without credentials.

## Verification and rollout

`docs/server-proxy-cloud-evidence.json`: a disposable Vercel VM ran the real bounded
production downloader against the supplied `GcOe4ILS6Ow` video. It produced a 17.02s
H.264/AAC MP4 and headless Chromium decoded 509 frames through `ended=true`, with no
media error. The VM was stopped. No local daemon or Google/YouTube cookies were used.
The report fingerprints the files actually tested; the later outgoing-byte counter
addition is covered by local boundary tests rather than that recorded cloud run.

`docs/server-proxy-browser-evidence.json`: headless Chrome with a synthetic OIDC issuer,
real local API/database/worker/object storage and a generated source fixture verified
login, a deliberately blocked health response, token renewal, server import, playable
preview, reconnect, lost acknowledgement retry, broadcast output, quota rejection,
logout/relogin and retained library. This is **not** a real Google login or R2 E2E.
The separate cloud test proves the actual YouTube/proxy/download/playback boundary.

Before production: run migration `011_proxy_quota.sql` through
`commercial-migrate.py` after backup,
build a fresh immutable worker snapshot (including EJS/Node), configure the server
secrets and switches, and follow PR-specific `feat → develop → main` merge approvals.
Deploy and then verify real Google login → YouTube import → R2 → playback and a live
quota test notification. Do not mark production restored/complete from local tests.

References: [DataImpulse User API reference](https://documenter.getpostman.com/view/7041120/2sAY4rGRZC),
[traffic pricing/no expiry](https://help.dataimpulse.com/en/articles/15931005-pricing-and-plans-how-to-choose-the-right-one),
[Telegram Bot API](https://core.telegram.org/bots/api).
