# User Preferences & Personal Skills — Handoff Plan

> Status: design / not started. Author handoff. TL;DR style.

## Goal

Give each user a DB-backed **preferences document** plus **personal skills** they
manage from a reworked Settings modal. Personal skills consolidate with the
admin (global) skills inside the user's sessions. The whole document is
**exportable / importable as versioned JSON**.

## Decisions (from interview)

| Topic | Decision |
|---|---|
| User capabilities vs admin skills | (a) add own skills, (b) enable/disable admin skills per user, (c) override an admin skill by name |
| Override mechanics | "Override" **forks** the admin SKILL.md into an editable personal skill that **instantly shadows** the admin one. UI shows an **"override user" badge**. |
| Name-collision resolution | **Personal always wins** — one SKILL.md materialized in the sandbox, the personal one. |
| Storage | **Hybrid**: light `user_preferences` JSONB doc (UI settings + disabled-admin list) **+** dedicated `user_skills` table for heavy SKILL.md content. |
| Personal-skill scope | **Global to the user** — active in all of the user's sessions (not per-session like MCP). |
| Import/export | **Full versioned doc** (`schema_version`), includes UI settings (migrated off localStorage) + personal skills. |
| UI prefs storage | **DB is source of truth**; one-time migration from localStorage at login; localStorage becomes a cross-device cache. |
| Import safety | **Self-only** (writes only the current user, never admin/other users) + **frontmatter revalidation** through `skill_md.py`; malformed rejected. |
| Quota | **Soft caps, server-validated**, values tunable (protect agent context budget + storage). |
| Settings menu | New **Settings modal** (tabs). Header dropdown keeps: Replay tutorial, User dashboard (exists in another MR — out of scope), Admin link (admins). |

## Current state (grounding)

- Skills are **global**, `is_active` admin-only, no per-user/per-session selection
  (`backend/app/models/skill.py`, `backend/app/services/messages.py:609` `get_active_skills`).
- Skills materialized to the sandbox each turn by
  `backend/app/services/session_skills.py:apply_session_skills` (idempotent, prunes stale dirs),
  called from `orchestrator_cli.py:247` and `orchestrator_inkwest_agent.py:268`.
- `inkwest_agent` advertises `skills[{name,description}]`, loads body lazily via the
  synthetic `read_skill` tool (`backend/inkwest_agent/skills.py`).
- No per-user settings in DB. `User` model has no JSON column (`backend/app/models/user.py`).
- UI prefs live in localStorage Zustand store `inkwest-settings`
  (`frontend/src/store/settings.ts`): `compactToolCards`, `showTimestamps`,
  `wideComposer`, `sidebarWidth`.
- Established DB-settings cache pattern = `backend/app/services/app_settings.py`
  (in-memory dict, `asyncio.Lock` on write, refresh-on-write).
- Precedent for per-conversation JSONB selection = `Session.enabled_mcp_servers`.
- Admin skills CRUD UI = `frontend/src/pages/admin/SkillsPage.tsx`; API `routes/skills.py`.
- No import/export of any config exists today.

## Data model

### Table `user_skills`

```
id                    UUID PK
user_id               UUID FK users(id) ON DELETE CASCADE, indexed
name                  String(255)        # normalized slug, unique per user
content               Text               # full SKILL.md verbatim
description           Text NULL          # parsed from frontmatter
is_active             Boolean default True
is_override           Boolean default False   # true => shadows an admin skill of same name
source_admin_skill_id UUID NULL          # provenance when forked from an admin skill
created_at, updated_at  (TimestampMixin)
UNIQUE (user_id, name)
```

Mirrors the admin `skills` shape (`name`/`content`/`description`/`is_active`) so the
CRUD UI and `skill_md.py` parsing are reused verbatim. `is_override` drives the badge.

### Table `user_preferences` (one row per user)

```
user_id         UUID PK FK users(id) ON DELETE CASCADE
schema_version  Integer default 1
document        JSONB
updated_at
```

`document` shape (versioned):

```json
{
  "ui": {
    "compactToolCards": false,
    "showTimestamps": false,
    "wideComposer": false,
    "sidebarWidth": 280
  },
  "skills": {
    "disabled_admin": ["pod-crash-triage", "..."]   // admin skill names the user turned OFF
  }
}
```

Default = all admin skills active (empty `disabled_admin`). Storing the **disabled**
set (not enabled) keeps newly-added admin skills on by default.

Rationale for the split: UI settings + the small disabled-list stay queryable/cheap
in JSONB; heavy SKILL.md bodies live as rows (avoid a fat blob rewritten on every toggle).

## Effective-skills resolution (per user, per turn)

Replace the global `get_active_skills(db)` with `get_effective_skills(db, user_id)`:

```
admin_active   = skills WHERE is_active                       # global
disabled       = prefs.document.skills.disabled_admin         # set of names
user_active    = user_skills WHERE user_id=? AND is_active

effective = {}
for s in admin_active:
    if s.name not in disabled:
        effective[s.name] = s
for s in user_active:
    effective[s.name] = s        # personal ALWAYS wins (shadow)

return effective.values()        # ordered by name
```

`apply_session_skills` is unchanged (already takes a skill list). Thread `user_id`
from the session into the two orchestrator call sites. Confirm `Session.user_id`
is available at those points; if not, pass it through the turn context.

## Backend work

- **Migration**: Alembic — create `user_skills` + `user_preferences` (additive, reversible: down = drop both).
- **Service** `services/user_preferences.py`:
  - Per-user fetch on demand (one prefs row + skills query per turn — cheap; do **not**
    hold an all-users in-memory cache). Optional small TTL/LRU if profiling shows need.
  - Write path mirrors `app_settings.py` discipline (lock + post-commit consistency),
    scoped per user.
  - `get_effective_skills(db, user_id)` as above.
- **Validation** (reuse `core/skill_md.py`):
  - Reparse every personal SKILL.md on save/import → slug + description; reject malformed.
  - Normalize `name` to slug; enforce `UNIQUE(user_id, name)`.
- **Quota** (soft, server-enforced, env/settings-tunable; suggested defaults):
  - `USER_SKILLS_MAX_COUNT` = 50 (active), `USER_SKILL_MAX_BYTES` = 64 KiB,
    `USER_PREFS_DOC_MAX_BYTES` = 256 KiB. Reject over-limit with 4xx + clear message.
- **Endpoints** (all under the current user, auth required, **no admin role**):
  - `GET  /api/me/preferences` → `{schema_version, document, skills:[...]}`
  - `PUT  /api/me/preferences` → update `document` (ui + disabled_admin)
  - `GET  /api/me/skills` · `POST /api/me/skills` · `PUT /api/me/skills/{id}` · `DELETE /api/me/skills/{id}`
  - `GET  /api/me/skills/effective` → `get_effective_skills` rendered as `SkillList`: the
    exact set materialized into the user's sandbox. Backs the chat picker (palette + `/`
    autocomplete) so it can't drift from what the agent can trigger. `/api/skills` stays
    the **admin catalog** — the settings editor needs the rows the user disabled/overrode.
  - `POST /api/me/skills/override/{admin_name}` → fork admin SKILL.md into a personal
    skill (`is_override=true`, `source_admin_skill_id` set), returns the new skill.
  - `GET  /api/me/export` → versioned JSON (download)
  - `POST /api/me/import` → validate + apply (self-only)

### Import/export format

```json
{
  "schema_version": 1,
  "exported_at": "2026-06-26T...Z",
  "preferences": { "ui": {...}, "skills": { "disabled_admin": [...] } },
  "skills": [
    { "name": "...", "content": "<SKILL.md>", "is_active": true, "is_override": false }
  ]
}
```

Import rules:
- **Self-only**: writes solely the current user's prefs + skills; never touches admin
  `skills` or other users.
- Revalidate each skill via `skill_md.py`; reject the whole import (or per-skill, TBD)
  on malformed frontmatter.
- Enforce quotas.
- `is_override` recomputed: if an imported skill name collides with an existing **admin**
  skill, mark `is_override=true` regardless of the file's value.
- `schema_version` mismatch → run a forward migration or reject with a version error.
- Decide merge semantics: **replace** all personal skills vs **upsert** by name (recommend
  upsert-by-name + keep existing not in the file, with an explicit "replace all" option).

## Frontend work

- **Settings modal** (tabs): General/Display · Personal Skills · Import/Export.
  Header dropdown's "Settings" opens it; keep Replay tutorial, User dashboard, Admin link.
- **Skills tab**: extract the CRUD UI from `pages/admin/SkillsPage.tsx` into a shared
  component reused by admin + personal. Show **"override user" badge** when `is_override`.
  Add an "Override" action on each admin skill (calls the override endpoint, opens the
  fork in the editor).
- **Store** `store/preferences.ts` (DB-backed) supersedes `store/settings.ts`:
  - On login: if `inkwest-settings` exists in localStorage **and** server `document.ui`
    is empty → one-time push to server, then DB is authoritative; keep localStorage as cache.
  - Reads from cache, writes through to `PUT /api/me/preferences`.
- **Import/Export**: file download for export; upload + parse + POST for import, with a
  preview/confirm step (it can rewrite the user's skills).
- `sidebarOpen` stays transient (not persisted) as today.

## Security

- Personal skills are user-authored → self-trust; but their SKILL.md is materialized into
  the sandbox and read by the agent. Keep them **self-only** end-to-end.
- Import never escalates: no admin-skill writes, no cross-user writes, role check absent
  by design (operates on `current_user`).
- Frontmatter revalidation prevents malformed/oversized payloads; quotas bound context blowup.
- No creds in personal SKILL.md (unlike MCP config) — content is plain markdown.

## Migration / rollback

- All changes additive: two new tables. Down-migration drops them.
- localStorage retained as cache → disabling the feature degrades to current behavior.
- No change to admin `skills` table or its endpoints.

## Open items

- Confirm `Session.user_id` reachable at both orchestrator skill-apply call sites.
- Import merge semantics: upsert-by-name vs replace-all (recommend upsert + optional replace).
- Per-skill vs whole-doc rejection on a bad import entry.
- Exact quota defaults (above are starting points).
- Whether `read_skill` advertising in `inkwest_agent` needs any change (it consumes the
  merged list already — likely none).

## Suggested work breakdown

1. DB migration: `user_skills` + `user_preferences` (+ models).
2. `user_preferences` service + `get_effective_skills` + orchestrator wiring.
3. Personal-skills CRUD + override endpoints + validation/quota.
4. Export/import endpoints (versioned, self-only).
5. Frontend Settings modal + shared skills CRUD component + override badge.
6. `preferences` store + localStorage→DB one-time migration.
7. Frontend import/export UI (preview/confirm).
8. Tests: resolution precedence, override fork, import validation/self-only, quota, migration.
