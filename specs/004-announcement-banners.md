# 004 — Announcement banners

**Status**: in progress
**Requested by**: @babs
**Date**: 2026-10-28

## Problem

Admins have no way to tell every Inkwest user something: a planned maintenance, a degraded
connector, a new feature. Today that goes through Teams or mail, which users read late or never,
and nothing in the app itself says "Bedrock is throttled, expect slow answers".

## Solution

An admin writes a banner from the console: a short message (inline markdown, links allowed), a
level (info, success, warning, danger) that sets its color, and optionally a custom background
color, text color and opacity. The admin chooses whether users may close it or whether it stays
pinned, and may give it an end date after which it disappears on its own. Enabled banners show at
the top of every page, chat and admin console alike, for every signed-in user, and appear or
vanish within seconds of an admin edit without a reload. A user who closes a banner does not see
it again in that browser, unless the admin edits it.

Admin-managed banners, without a ticker or audience targeting.

## Scope

- `announcements` table: message, level, optional bg color, optional text color, opacity,
  dismissible, enabled, optional `ends_at`, author, timestamps.
- `GET /api/announcements` (any signed-in user): enabled and unexpired rows, oldest first.
- Admin CRUD `GET|POST /api/admin/announcements`, `PUT|DELETE /api/admin/announcements/{id}`
  (`require_admin`), structured log on every write.
- Live refresh: every write broadcasts `announcements_changed` on the sync channel; the front
  refetches on it and on each sync-socket (re)connect.
- `AnnouncementBanners` stack in `ShellLayout`, below `BackendBanner` and `UpdateBanner`.
- Per-browser dismissal in localStorage, keyed `id:updated_at`, pruned to served ids.
- Client-side withdrawal at the exact `ends_at` minute (timer), not at the next refetch.
- Admin page "Announcements": list, create/edit form with live preview, enable toggle, delete.

## Out of scope

- Ticker / scrolling text.
- Audience targeting (per role, per user). Every signed-in user sees every banner.
- `starts_at` scheduling. An admin enables the banner when it should start.
- Per-user dismissal stored in DB (follows the user across browsers).
- Banners on the anonymous `/shared/:token` page and the login page.
- Block markdown (headings, lists, images, raw HTML).

## Acceptance criteria

- [ ] **AC1** Given an enabled banner with no `ends_at`, when a signed-in user loads any page of
      the app or the admin console, then the banner shows at the top with its message.
- [ ] **AC2** A disabled banner, or one whose `ends_at` is past, is absent from
      `GET /api/announcements` and from the page.
- [ ] **AC3** Given an open app tab, when an admin creates, edits, disables or deletes a banner,
      then the tab reflects it within 5 s without a reload. An admin-console tab reflects it after
      its own write, or when it regains focus.
- [ ] **AC4** Given a dismissible banner, when the user closes it and reloads, then it stays
      hidden; when an admin then edits it, it shows again.
- [ ] **AC5** A non-dismissible banner has no close button.
- [ ] **AC6** Given an open tab showing a banner whose `ends_at` is 1 min away, then the banner
      leaves the page at `ends_at` with no refetch.
- [ ] **AC7** With no custom color, the background is the level's color; with a custom
      background and no text color, the text is black or white by luminance; an explicit text
      color always wins; opacity below 100 renders a translucent background.
- [ ] **AC8** Message markdown renders bold, italic, inline code and links (new tab,
      `rel="noopener noreferrer"`); raw HTML, images, headings and lists render as plain text or
      are dropped, never as markup.
- [ ] **AC9** A non-admin gets 403 on every `/api/admin/announcements` route; an unauthenticated
      caller gets 401 on `GET /api/announcements`.
- [ ] **AC10** The API rejects with 422: an empty or >500-char message, an unknown level, a color
      not `#rrggbb`, opacity outside 10..100, a create with `ends_at` in the past, a naive datetime.
      The DB enforces the same bounds with CHECKs.
- [ ] **AC11** A failed `GET /api/announcements` keeps the banners already shown (no flicker on a
      backend blip) and never raises the 5xx strip for a gateway error.

## Phases

### Phase 1 — Backend: table, read route, admin CRUD, broadcast
- Work: migration + SQLAlchemy model, Pydantic schemas, `GET /api/announcements`, admin CRUD,
  `pubsub.notify({"type": "announcements_changed"}, user_id=None)` after each committed write.
- **Covers**: AC2, AC9, AC10
- **Data model impact**: new table `inkwest.announcements` (see summary).
- **DoD**: `pytest tests/test_api_announcements.py` green; `db_migrate.py` up, `--rollback`, up
  on a scratch Postgres with each CHECK probed by a failing INSERT; `pre-commit run --all-files` green.

### Phase 2 — Front: banner stack, dismissal, expiry, live refresh
- Work: `store/announcements.ts` (fetch + keep-on-failure), `AnnouncementBanners` in
  `ShellLayout`, localStorage dismissal, expiry timer, `announcements_changed` handling and
  reconnect refetch in `useSyncSocket`, inline markdown renderer.
- **Covers**: AC1, AC3, AC4, AC5, AC6, AC7, AC8, AC11
- **Data model impact**: none
- **DoD**: `npm test -- --run` and `npm run lint` green; headless browser against a local stack
  shows a banner, closes it, survives reload, and disappears on an admin disable without reload.

### Phase 3 — Admin page
- Work: `/admin/announcements` page (list, form, preview, toggle, delete), nav entry,
  `adminApi` calls.
- **Covers**: AC3 (end to end from the console), AC7 (preview matches the banner)
- **Data model impact**: none
- **DoD**: vitest covers create, edit, delete and the 422 error display; headless browser creates a
  banner from the console and sees it in a second tab.

## Data model impact (summary)

New table `inkwest.announcements`:

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `uuid_generate_v4()` |
| `message` | TEXT NOT NULL | CHECK trimmed length 1..500 |
| `level` | TEXT NOT NULL | CHECK in `info`, `success`, `warning`, `danger` |
| `color` | VARCHAR(7) NULL | CHECK `^#[0-9a-fA-F]{6}$`; NULL = level color |
| `text_color` | VARCHAR(7) NULL | same CHECK; NULL = automatic |
| `opacity` | SMALLINT NOT NULL DEFAULT 100 | CHECK 10..100 |
| `dismissible` | BOOLEAN NOT NULL DEFAULT true | |
| `enabled` | BOOLEAN NOT NULL DEFAULT false | a hand INSERT stays unpublished |
| `ends_at` | TIMESTAMPTZ NULL | |
| `created_by` | UUID NULL FK `users(id)` ON DELETE SET NULL | |
| `created_at`, `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | `updated_at` bumped on every edit |

Partial index on `(created_at) WHERE enabled`.

## Open questions

- (none)

## Decisions

- Read route requires sign-in: every Inkwest page is behind auth, and a
  banner may name internal systems.
- No author exposed on the read route; the admin list shows it.
- Message cap 500 chars: incident notes here carry a link plus a sentence or two.
- `enabled` and `dismissible` are required in the write body: a PUT is a full replace, and a body
  that forgets `enabled` must not silently switch the banner off.
- Create refuses a past `ends_at`; update accepts it, as a way to retire a banner.
- Live refresh reuses the sync socket broadcast (precedent `mcp_catalog_changed`) instead of a poll.
- Read route uncached: one indexed query per tab on load (two in the app: mount, then sync socket
  open), on focus, on reconnect and per broadcast; add a short cache if it ever shows in latency.
- Writes are logged (`announcement_created|updated|deleted`, id, admin id), not written to
  `connector_audit_log`, which records connector access grants.
- Level colors come from the Tailwind palette already used by the app's banners (blue, green,
  amber, red), in hex so opacity can apply through `rgba()`.
- The admin console has no sync socket (only the chat layout mounts it), so AC3's live push
  applies to app tabs; admin tabs refetch after their own writes and when the tab regains focus.
  Mounting the sync socket in the console would also start session sync and the MCP DEK handshake
  there.
