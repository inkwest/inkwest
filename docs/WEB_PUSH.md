# Web Push notifications (session share)

> Spec: `specs/003-web-push-share-notifications.md`.

## TL;DR

- When someone shares a conversation with a user, each browser where that user turned
  notifications on gets an OS notification (`<inviter> shared "<title>" with you`), even with no
  Inkwest tab open. Clicking it focuses an Inkwest window (or opens `/`), where the usual
  accept/decline popup shows the invite.
- No notification when an Inkwest window is focused: the in-app popup already covers it.
- Off until an operator sets the three `VAPID_*` vars. Opt-in per browser, from
  Settings > Notifications.
- Share invites only. Best effort: no retry, a missed push leaves the invite pending and the
  popup picks it up on next load.

## Enabling it

Generate a VAPID key pair (P-256) with the backend venv:

```bash
backend/.venv/bin/python -c 'import base64; from cryptography.hazmat.primitives.asymmetric import ec; from cryptography.hazmat.primitives import serialization as s; k = ec.generate_private_key(ec.SECP256R1()); b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode(); print("VAPID_PRIVATE_KEY=" + b(k.private_numbers().private_value.to_bytes(32, "big"))); print("VAPID_PUBLIC_KEY=" + b(k.public_key().public_bytes(s.Encoding.X962, s.PublicFormat.UncompressedPoint)))'
```

It prints two `.env` lines: the private key as base64url raw 32 bytes, the public key as
base64url uncompressed point (65 bytes, `0x04` prefix). Store the private key like any other
secret.

| Var | Value |
|-----|-------|
| `VAPID_PUBLIC_KEY` | base64url uncompressed P-256 point. Served to browsers as `push_public_key` in `GET /api/config` |
| `VAPID_PRIVATE_KEY` | base64url raw or DER P-256 private key (`SecretStr`) |
| `VAPID_SUBJECT` | `mailto:<user>@<domain>` or `https://<domain>`, nothing after the domain. Contact the push services may use |

Boot checks (`validate_settings`), each one refuses to start:

- one or two of the three set: error names the missing ones. None set = feature off.
- public key not a base64url uncompressed P-256 point on the curve (PEM, whitespace or a
  trailing newline all fail).
- private key unparseable, or it does not derive the public key.
- subject not exactly `mailto:<user>@<domain>` / `https://<domain>` (no path, query, port).

Feature off: `push_public_key` is `null`, the Settings tab is hidden, `PUT
/api/me/push-subscriptions` answers 404, invites send nothing.

## Egress

- The backend POSTs over HTTPS to the browser's push service. Allowed hosts
  (`PUSH_SERVICE_HOSTS` in `app/core/push_keys.py`): `fcm.googleapis.com`,
  `*.push.services.mozilla.com`, `*.notify.windows.com`, `*.push.apple.com`. Open these if a
  NetworkPolicy or firewall restricts the backend.
- Direct connection only: the push client ignores `HTTP(S)_PROXY` (aiohttp `trust_env` is off).
- Redirects are refused: a 301/302/303/307/308 counts as `failed` (`error=RedirectRefused`).
  The allowlist only vets the first URL.
- 10 s timeout per send.
- A browser whose push service is not in the list gets 422 on `PUT` (UI: "This browser's push
  service isn't supported by Inkwest."). Adding one = a new suffix in `PUSH_SERVICE_HOSTS`.

## Flow

1. Settings > Notifications toggle (the only place that asks for permission): registers
   `/sw.js`, `pushManager.subscribe` with the VAPID public key, then
   `PUT /api/me/push-subscriptions` (`endpoint`, `keys.p256dh`, `keys.auth`).
2. The backend upserts one row per endpoint. An endpoint already held by another user moves to
   the caller (one browser profile, one current owner).
3. `POST /api/sessions/{id}/shares` (`create_share`) commits, then calls
   `web_push.schedule_share_invite`: a background task, the response never waits on it.
4. The sender (`app/services/web_push.py`) loads the invitee's subscriptions in its own DB
   session and sends one encrypted push per row (RFC 8291 `aes128gcm`, VAPID JWT). Payload:
   `{type: "share_invite", share_id, session_id, session_title, invited_by}`. Headers: TTL 24 h,
   `Urgency: normal`, `Topic` = share id hex, so a re-invite replaces an undelivered push.
5. The service worker (`frontend/public/sw.js`) skips the push when any Inkwest window is
   focused, else shows the notification with `tag` = share id.
6. Click: focuses an existing Inkwest window without navigating, else opens `/`. The invite
   popup comes from the sync WebSocket or `GET /api/shares/pending`, as before.

Browser side, also:

- Self-heal on every app load, when permission is granted and a subscription exists: re-`PUT`
  it (covers a rotated endpoint). On a VAPID key mismatch it `DELETE`s the old row,
  unsubscribes and resubscribes.
- Logout unsubscribes the browser and `DELETE`s its row before the session ends, capped at 2 s
  so logout never hangs.
- In the combined image, `/sw.js` is served with `Cache-Control: no-cache` and 404s when missing
  (no `index.html` fallback).

## Data

- Table `inkwest.push_subscriptions` (see `docs/DATABASE_SCHEMA.md`): `endpoint` UNIQUE,
  `p256dh`, `auth`, `user_agent` (truncated to 512), `created_at`, `last_seen_at`. Rows go with
  the user (`ON DELETE CASCADE`).
- At most 20 rows per user. A `PUT` past the cap evicts the user's least recently seen rows
  (`last_seen_at`, then `created_at`, then `id`), never the one just written.
- Rows are removed by: the client `DELETE` (toggle off, logout, key change), a 404/410 from the
  push service on a row older than 60 s, eviction past the cap. A younger row is kept: FCM can
  answer 410 to a subscription in its first ~10 s.
- The endpoint is a capability URL: anyone holding it can push to that browser. It is never
  logged (logs carry the host only) and `DELETE` takes it in the body, not the query string.

## Ops

Metric `inkwest_push_sent_total{result}` (dedicated metrics port), one count per subscription
send except `dropped`:

| `result` | Meaning |
|----------|---------|
| `ok` | push service accepted it |
| `gone` | 404/410 on a row older than 60 s: subscription expired or revoked, row deleted |
| `failed` | other HTTP status, 404/410 on a row younger than 60 s, timeout, network error, refused redirect. Row kept |
| `rejected` | stored endpoint host not in the allowlist, nothing sent |
| `dropped` | invite not scheduled (256 pending invites, or shutdown in progress), one per invite |

Sends cancelled at shutdown are not counted.

Log events:

| Event | Level | Fields | When |
|-------|-------|--------|------|
| `push_send_failed` | warning | `host`, `status`, `error` | one send counted `failed` |
| `push_endpoint_rejected` | warning | `host` | one send counted `rejected` |
| `push_invite_dropped` | warning | `reason` (`backlog`/`shutdown`), `pending` | invite counted `dropped` |
| `push_invites_cancelled` | info | `count` | shutdown cancelled in-flight invites |
| `push_share_invite_failed` | exception | stack | unexpected error in an invite task (VAPID load, DB). No metric |

## Key rotation

No tooling: replace both keys (same pair) and restart.

- Each browser resubscribes with the new key on its next Inkwest load (self-heal), deleting its
  old row.
- A browser that does not come back keeps its old row. Only 404/410 on a row older than 60 s
  prune it, so if the push service rejects the new key's signature with another status, each
  invite counts it `failed` until that browser loads Inkwest again or the 20-row cap evicts it.
- Changing `VAPID_SUBJECT` alone needs no resubscribe.

## Limits

- No retry. Push service down = invites recovered from the in-app popup only.
- Bounds per process: 4 invites sending at once, 256 pending (beyond that, `dropped`).
- In-flight invites are cancelled on shutdown and lost (`push_invites_cancelled`).
- The task set, semaphore and pending cap are in-process, but no state is shared across
  requests: the process that serves `create_share` sends its pushes, at most once. Push adds no
  constraint to multi-worker or multi-replica runs (unlike the OAuth2 MCP and LLM broker stores,
  which fail fast on `WEB_CONCURRENCY>1`). The metric is per process, like every other.
- Session title and inviter name travel end-to-end encrypted, but show on the lock screen.
- Browsers whose push service is not allowlisted cannot subscribe.

## Manual end-to-end check

`scripts/web_push_e2e.py` drives a real Chrome against a running instance through FCM and
asserts 003-AC10 (notification with no Inkwest window, popup on open) and 003-AC11 (no
notification on a focused window), plus `ok` +2 on the metric.

Needs: the app with dev auth (`OIDC_ENABLED=false`) and VAPID keys, a fresh migrated DB, Google
Chrome stable (Playwright's Chromium has no FCM keys), `psql` set through `PG*` vars.

```bash
INKWEST_BASE_URL=http://localhost:8000 INKWEST_METRICS_URL=http://localhost:9090/metrics \
PGHOST=localhost PGDATABASE=inkwest uv run ./scripts/web_push_e2e.py
```

Exit 0 = pass. Screenshots and `run.json` land in `$OUT` (default: a new temp dir, printed at
start). The docstring lists every variable.

## Code map

| Path | Role |
|------|------|
| `backend/app/core/config.py` | `VAPID_*` settings, `push_enabled`, boot checks |
| `backend/app/core/push_keys.py` | key/subject/URL checks, `PUSH_SERVICE_HOSTS`, `load_vapid` |
| `backend/app/api/routes/me.py` | `PUT`/`DELETE /api/me/push-subscriptions`, 20-row cap |
| `backend/app/services/web_push.py` | sender, shared aiohttp client, task bounds |
| `backend/app/api/routes/sharing.py` | `create_share` schedules the invite |
| `frontend/public/sw.js` | service worker: `push`, `notificationclick` |
| `frontend/src/services/push.ts` | subscribe, unsubscribe, self-heal, logout cleanup |
| `frontend/src/components/settings/SettingsModal.tsx` | Notifications tab |
