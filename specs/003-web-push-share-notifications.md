# 003 — Web Push notification on session share

**Status**: in progress
**Requested by**: @babs
**Date**: 2026-10-27

## Problem

When someone shares a conversation with a user, the invite only reaches them through the sync
WebSocket (`share_invite` → accept/decline popup) or the `GET /shares/pending` recovery fetch on the
next page load. A user with no Inkwest tab open learns about the invite only whenever they next open
the app, so a "come look at this incident" share can sit unseen for hours.

## Solution

A user can turn on browser notifications for Inkwest from Settings, per browser/device. Once on,
being invited to a conversation raises an OS notification (`<inviter> shared <title> with you`)
even when no Inkwest tab is open. Clicking it opens (or focuses) Inkwest, where the existing invite
popup lets them accept or decline. When an Inkwest window is already focused, nothing extra pops:
the in-app popup covers it. The feature is off for a deployment until an operator sets the VAPID
keys.

## Scope

- **Config**: `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY` (`SecretStr`), `VAPID_SUBJECT`
  (`mailto:<user>@<domain>` or `https://<domain>`, no path or port). All three set and consistent
  ⇒ feature on; none ⇒ off; partial, malformed or mismatched ⇒ fail-fast at boot. Documented in `.env.example` with the key-generation command.
- **Public key exposure**: `GET /api/config` gains `push_public_key: str | null` (null = feature off,
  the UI hides the toggle).
- **Subscriptions**: new `push_subscriptions` table, one row per browser endpoint (owned by one user).
  At most 20 rows per user: a new subscription past the cap evicts the user's least recently seen
  one (`last_seen_at`, then `created_at`, then `id`), never the one just written.
  `PUT /api/me/push-subscriptions` upserts the caller's subscription (`endpoint`, `keys.p256dh`,
  `keys.auth`), `DELETE /api/me/push-subscriptions` with the endpoint removes it. An endpoint
  re-registered by another user moves to that user (one browser profile = one current owner).
- **Sender**: `app/services/web_push.py` using `pywebpush.webpush_async`. Called from
  `create_share` after commit, as a background task so the HTTP response never waits on a push
  service. Fans out to every subscription of the invitee. `404`/`410` from the push service deletes
  the row (rows older than 60 s); other failures are logged (`push_send_failed`, status, push-service host only) and
  counted (`inkwest_push_sent_total{result}`), never retried, never surfaced to the inviter.
- **Payload** (encrypted per RFC 8291, opaque to the push service): `{type: "share_invite",
  share_id, session_id, session_title, invited_by}`. TTL 24h, `urgency: normal`, `topic` = share id as 32 hex chars (RFC 8030 caps topics at 32 base64url chars)
  so a re-invite replaces an undelivered one.
- **Service worker** `frontend/public/sw.js` (served at `/sw.js`, scope `/`, `no-cache`, excluded
  from the SPA `index.html` fallback): `push` → skip if a focused Inkwest window exists, else
  `showNotification` with `tag` = share id; `notificationclick` → focus an existing Inkwest window or
  open `/`.
- **Settings UI**: a "Notifications" tab with one toggle for this browser. On: request permission
  (only on this click, never on page load), `pushManager.subscribe` with the VAPID key, `PUT`. Off:
  `unsubscribe` + `DELETE`. Shows the blocked state when the browser permission is `denied`, and an
  unsupported message when `PushManager` is missing.
- **Self-heal**: on app load with permission granted, re-`PUT` the current subscription (covers a
  browser-rotated endpoint and a VAPID key change: a key mismatch triggers unsubscribe + resubscribe).
- **Logout**: unsubscribes this browser and deletes its row, so a shared machine stops receiving the
  previous user's invites.
- **Docs**: `docs/WEB_PUSH.md` (TL;DR: keys, egress, flow), `DATABASE_SCHEMA.md`, `CLAUDE.md`
  architecture line.

## Out of scope

- Pushes for any other event: invite accepted/declined, access revoked, AI turn finished, MCP ask.
  The sender is generic enough to add them later; no event other than `share_invite` calls it now.
- Anonymous share links (`session_share_links`): no recipient user.
- Accept/decline action buttons on the notification itself (would need an authenticated call from
  the service worker).
- PWA install, manifest, offline caching: the service worker handles push only.
- In-DB / auto-generated VAPID keys, key rotation tooling.
- Per-event notification preferences, quiet hours, email fallback.

## Acceptance criteria

- [ ] **AC1** Given all three `VAPID_*` vars are set, `GET /api/config` returns the public key in
      `push_public_key`; with none set it returns `null`.
- [ ] **AC2** Given only one or two `VAPID_*` vars are set, the backend refuses to boot with an error
      naming the missing ones; it also refuses to boot when the public key is not an uncompressed
      P-256 point, when `VAPID_PRIVATE_KEY` does not derive `VAPID_PUBLIC_KEY`, or when
      `VAPID_SUBJECT` is not `mailto:<user>@<domain>` / `https://<domain>`.
- [ ] **AC3** Given a logged-in user, when they `PUT` a subscription, then exactly one row exists for
      that endpoint, owned by them; a second `PUT` of the same endpoint updates it, and a `PUT` of the
      same endpoint by another user moves the row to that user.
- [ ] **AC4** An unauthenticated `PUT`/`DELETE` on `/api/me/push-subscriptions` gets 401; a user's
      `DELETE` never removes another user's row (404, row intact).
- [ ] **AC5** A `PUT` with a non-`https` endpoint, or keys that are not valid base64url of the
      expected length, gets 422 and stores nothing.
- [ ] **AC6** Given the invitee has two subscriptions, when the owner invites them, then
      `create_share` returns 201 without waiting on the push service and one push is sent per
      subscription with the `share_invite` payload.
- [ ] **AC7** Given the push service answers 410 (or 404) for a subscription older than 60 s, then
      that row is deleted and the other subscriptions are unaffected; a younger row is kept and the
      send counts as `failed`.
- [ ] **AC8** Given the push service times out or answers 5xx, then the share is still created, the
      failure is logged with no endpoint token or payload in the log, and the metric counts it.
- [ ] **AC9** Given the feature is off (no VAPID keys), inviting a user sends nothing and raises
      nothing.
- [ ] **AC10** Given a real Chromium with notifications granted and no Inkwest tab focused, when a
      session is shared with the user, then an OS notification titled with the session title appears;
      clicking it focuses/opens Inkwest and the invite popup is shown.
- [ ] **AC11** Given an Inkwest window is focused when the push arrives, then no OS notification is
      shown.
- [ ] **AC12** `GET /sw.js` returns the worker as JavaScript with `Cache-Control: no-cache`; a missing
      `/sw.js` 404s instead of returning `index.html`.
- [ ] **AC13** The Settings toggle: requests permission only when clicked; on success the row exists;
      off removes it; `denied` permission shows the blocked message; no `PushManager` shows the
      unsupported message; feature off hides the tab.
- [ ] **AC14** Logging out removes the current browser's subscription row.
- [ ] **AC15** An endpoint whose host is not a known browser push service (`fcm.googleapis.com`,
      `*.push.services.mozilla.com`, `*.notify.windows.com`, `*.push.apple.com`) gets 422 on `PUT`,
      and a stored row with such a host is never sent to.

## Phases

### Phase 1 — Config, table, subscription API
- Work: settings + boot validation, `push_public_key` in `/api/config`, migration + model,
  `PUT`/`DELETE /api/me/push-subscriptions` with validation and ownership move.
- **Covers**: AC1, AC2, AC3, AC4, AC5
- **Data model impact**: new table `inkwest.push_subscriptions` (`id` UUID PK, `user_id` FK users ON DELETE
  CASCADE + index, `endpoint` TEXT UNIQUE, `p256dh` TEXT, `auth` TEXT, `user_agent` TEXT NULL,
  `created_at`, `last_seen_at`).
- **DoD**: `pytest tests/test_push_subscriptions.py` green; on a throwaway Postgres,
  `db_migrate.py` up → `--rollback` → up exits 0 and leaves `inkwest.push_subscriptions` with its
  unique endpoint index; `pre-commit run --all-files` green.

### Phase 2 — Sender wired to invites
- Work: `pywebpush` dependency + `uv lock`, `web_push.py` (fan-out, 404/410 prune past 60 s, log + metric),
  background call from `create_share`; push-service host allowlist (PUT + send); boot check that
  `VAPID_PRIVATE_KEY` parses and derives `VAPID_PUBLIC_KEY`.
- **Covers**: AC2, AC6, AC7, AC8, AC9, AC15
- **Data model impact**: none
- **DoD**: `pytest tests/test_web_push.py tests/test_session_sharing.py` green, with the push HTTP call
  mocked at the `pywebpush` boundary and one test asserting the real encrypted request shape
  (`Content-Encoding: aes128gcm`, `Authorization: vapid t=…, k=…`) built from a generated keypair.

### Phase 3 — Service worker + Settings UI + logout
- Work: `public/sw.js`, SPA serving rules for `/sw.js`, registration on load, Notifications tab,
  self-heal re-subscribe, unsubscribe on logout.
- **Covers**: AC11, AC12, AC13, AC14
- **Data model impact**: none
- **DoD**: `npm test -- --run` + `npm run lint` + `npm run typecheck` green (toggle states, logout
  path); `pytest` test for the `/sw.js` serving rules green.

### Phase 4 — End-to-end check + docs
- Work: run the combined image locally with generated VAPID keys, headless Google Chrome stable
  (Playwright's Chromium cannot subscribe to FCM) with notification permission granted, two users, real push service round-trip; write
  `docs/WEB_PUSH.md`, update `DATABASE_SCHEMA.md`, `.env.example`, `CLAUDE.md`.
- **Covers**: AC10
- **Data model impact**: none
- **DoD**: AC10 and AC11 observed in the browser run (notification shown / not shown, captured via
  the service worker's `getNotifications()`); docs merged in the same MR.
- **Note**: the AC10 click leg (notification click focuses/opens Inkwest) cannot be driven by
  automation (an OS notification click is not scriptable, a synthetic `notificationclick` is
  untrusted); it is covered by `frontend/src/services/sw.test.ts`. The notification and popup legs
  are browser-observed.

## Data model impact (summary)

- New table `inkwest.push_subscriptions` (phase 1). No change to existing tables, no backfill.

## Open questions

None.

## Decisions

- VAPID keys from env, feature off when unset: the operator owns the secret like every other
  credential here; a DB-generated key would silently break all subscriptions on a DB restore.
- `pywebpush` over in-house crypto: the RFC 8291/8292 code is the part not worth owning. Cost:
  `aiohttp`, `requests`, `http-ece`, `py-vapid` in the image (`cryptography>=47` already met, 49
  installed).
- Invite only: matches the request; other events are listed out of scope.
- Skip when a window is focused: the in-app popup already shows the invite.
- Notification shows the session title and inviter name. Both cross the push service encrypted
  end-to-end but appear on the lock screen. Overturnable to a generic "New shared conversation".
- One subscription row per endpoint, moved on re-register by another user: one browser profile
  delivers to one Inkwest user at a time, so the latest login wins.
- 20 subscriptions per user, oldest evicted: bounds the per-invite fan-out and table growth from a
  single account; 20 browsers/devices per person is far above real use. Module constant, no setting.
- Send from `create_share` directly, not via pubsub: the route runs once per invite, while pubsub
  fans out per replica and per socket, which would multiply pushes.
- No retry on failure: the invite stays pending and the in-app popup recovers it on next load.
- A 404/410 on a subscription created less than 60 s ago counts as `failed` and keeps the row:
  FCM answers 410 to some brand-new subscriptions for up to ~10 s (measured in the phase 4
  run), and pruning there would silently drop a just-enabled browser until its next page load.
- Backend egress needs no change. The deployment adds no NetworkPolicy and no proxy for the
  backend (squid only fronts the sandbox containers), and the backend already calls public hosts directly
  (`pricing.us-east-1.amazonaws.com`, external MCP/OIDC servers). The first invite after
  deploy confirms it (`inkwest_push_sent_total{result="ok"}`).
- Endpoint hosts restricted to the four browser push services (AC15): the backend POSTs to a
  user-supplied URL, and an allowlist of public push-service domains closes SSRF without IP
  pinning. A browser on another push service gets no notifications until its suffix is added
  (module constant).
- `VAPID_PRIVATE_KEY` must derive `VAPID_PUBLIC_KEY` (boot error otherwise, part of AC2): a
  mismatched pair would sign JWTs that every push service rejects.
- Migration follows the develop convention: dbmate `-- migrate:up`, schema-qualified
  `inkwest.push_subscriptions`.
