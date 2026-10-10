# System Prompts

> The **system** prompt is the instruction prompt loaded *before any
> user interaction* of a session. It used to be hardcoded; it now lives in the
> database, managed by admins, with one default and an admin "custom" override.

## TL;DR

- **Global** system prompts (shared by all users; no per-user ownership).
- Exactly **one is the default** — the prompt every non-admin gets automatically.
- At **session creation**, an **admin** picks one from the list **or** types a
  **custom** free-text prompt for that session. **Non-admins always get the default.**
- CRUD is admin-only and mirrors the **prompt templates** / **clusters** verticals.
- The previously hardcoded prompt is **seeded** into the DB as the default on
  first startup, from `app/services/prompts.py::SYSTEM_PROMPT`.

> Distinct from **user** prompt templates (the chat palette — see
> [PROMPT_TEMPLATES.md](PROMPT_TEMPLATES.md)) and the correction prompt.
> This is the **system** prompt of the session.

## Data model

Table `inkwest.system_prompts` (see [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `uuid_generate_v4()` |
| `name` | VARCHAR(255) | unique, indexed |
| `content` | TEXT | prompt body; may contain `{cluster_name}` / `{namespaces}` blanks |
| `description` | TEXT, nullable | optional |
| `is_active` | BOOLEAN, default `true` | enable/disable flag (selector visibility) |
| `is_default` | BOOLEAN, default `false` | the prompt non-admins get |
| `created_at` / `updated_at` | TIMESTAMPTZ | `TimestampMixin` |

**Single-default invariant** — enforced by a partial unique index:

```sql
CREATE UNIQUE INDEX idx_system_prompts_single_default
    ON inkwest.system_prompts(is_default) WHERE is_default = true;
```

The session's choice is persisted on `inkwest.sessions`:

| Column | Type | Notes |
|---|---|---|
| `system_prompt_id` | UUID FK → `system_prompts(id)` `ON DELETE SET NULL`, nullable | admin-selected prompt |
| `system_prompt_custom` | TEXT, nullable | admin free-text for this session |

Both null → the session resolves the **global default** at run time.

SQLAlchemy model: `backend/app/models/system_prompt.py`.
Migrations (dbmate): `backend/migrations/20260608130000_system_prompts.sql`,
`backend/migrations/20260608130100_sessions_system_prompt.sql`.

## Seeding the default

There is no SQL seed (the `prompt_templates` table ships empty too). Instead the
default is seeded **idempotently at startup** by
`ensure_default_system_prompt()` (`app/services/prompts.py`), called from the
FastAPI lifespan in `app/main.py`. It inserts a `Default` row from the in-code
`SYSTEM_PROMPT` **only when the table is empty**, so admin edits/renames are
preserved and a default always exists out of the box. `SYSTEM_PROMPT` stays in
code as the ultimate fallback.

## API

Router `backend/app/api/routes/system_prompts.py`, mounted at `/api/system-prompts`.
Reads require authentication; writes require admin (same dependency pattern as clusters).

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/system-prompts?active_only=true` | user | List system prompts (active only by default) |
| `POST` | `/api/system-prompts` | admin | Create (409 if `name` exists); `is_default=true` demotes the previous default |
| `GET` | `/api/system-prompts/{id}` | user | Fetch one (404 if missing) |
| `PATCH` | `/api/system-prompts/{id}` | admin | Update; promote default; rename collision → 409 |
| `DELETE` | `/api/system-prompts/{id}` | admin | Hard delete (row removed, name freed; sessions fall back via `ON DELETE SET NULL`) → 204 |

Default-invariant rules enforced by the API:

- Setting `is_default=true` (create or update) clears the previous default in the
  same transaction.
- You **cannot** unset the default directly (`is_default=false` on the current
  default → 409) — promote another prompt instead.
- You **cannot** deactivate (`is_active=false`) or delete the default → 409.

Schemas: `SystemPrompt{Base,Create,Update,Response,List}` in `backend/app/models/schemas.py`.

### Session creation

`POST /api/sessions` accepts optional `system_prompt_id` and `system_prompt_custom`
(`SessionCreate`). Guard in `backend/app/api/routes/sessions.py`:

- **Non-admin** sending either field → **403** (they always get the default).
- Admin sending **both** → **400** (choose one).
- Admin sending an unknown / inactive `system_prompt_id` → **404**.

## Resolution at run time

`resolve_session_system_prompt(db, session_id)` (`app/services/prompts.py`)
resolves the base prompt body with this precedence:

1. `session.system_prompt_custom` (admin free-text), else
2. the active prompt referenced by `session.system_prompt_id`, else
3. the global default (`is_default = true`), else
4. `None` → caller falls back to the hardcoded `SYSTEM_PROMPT`.

Every orchestrator calls it and passes the result to
`get_system_prompt(cluster_name, namespaces, base_prompt=...)`, so the choice is
honored across **all** AI providers:

- API providers (`orchestrator.py`, `orchestrator_{azure,openai,google,openrouter}.py`)
  send it as the `system` parameter of the model request.
- The **Claude CLI** provider (`orchestrator_cli.py`) layers it onto Claude Code
  via `--append-system-prompt` (see `execute_claude_cli`); the user turn keeps
  only the runtime operational context (kubeconfig / EKS / connector hints) and
  the question. Note: with `--resume`, the system prompt is fixed by the first
  turn of a CLI session — changing it requires a new session.

`get_system_prompt` fills the `{cluster_name}` / `{namespaces}` blanks with
**literal token replacement** (not `str.format`) so an admin custom prompt
containing stray braces (e.g. JSON `{"k": "v"}`) never raises.

## Frontend

### Management screen (admin)

- Page: `frontend/src/pages/admin/SystemPromptsPage.tsx`, route `/admin/system-prompts`,
  nav entry in `AdminLayout.tsx`.
- Store: `useSystemPromptStore` in `frontend/src/store/admin.ts` (CRUD), backed by
  `frontend/src/services/adminApi.ts`.
- Client-side **search** over name / description / content.
- Create/edit modal: `name`, `content` (multiline), `description`, and a
  **"Default for non-admins"** toggle. Promoting a default refetches the list to
  keep the single-default view consistent.

### Session creation selector

- `frontend/src/components/session/NewSessionDialog.tsx`: when the current user is
  an **admin** (`useAuthStore`), a **System Prompt** `<select>` lists active prompts
  plus a **"Custom…"** option that reveals a free-text `<textarea>`. Non-admins see
  no selector and get the default.
- Threaded through `useSessionsStore.createSession(clusterId, opts)` →
  `api.createSession({ system_prompt_id | system_prompt_custom })`.

## Definition of Done

An admin manages a list of system prompts; at session start the admin picks from
the list or supplies a custom prompt, while non-admins get the default; the
prompt that used to be hardcoded now lives in the database. ✅

## Decisions

| Question | Decision |
|---|---|
| Scope | **Global** (no per-user) |
| Who manages | **Admins** (CRUD + choose the default) |
| Non-admin behavior | Always the **default**, no selector shown |
| Admin at session start | Pick from list **or** **custom** free-text |
| Custom persistence | Stored on the **session** only, not added to the library |
| Default fidelity | Seeded at startup from code; editable thereafter in DB |
