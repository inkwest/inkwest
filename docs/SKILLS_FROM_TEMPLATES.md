# Skills

> Implemented.
> This doc describes the **shipped state**. The intermediate designs (`(...)` name encoding,
> `render_skill_md`, slug computed from the name) were replaced by the "single SKILL.md" model below.

## 1. Goal

Each **skill** (table `inkwest.skills`, formerly `prompt_templates`) is materialized as a Claude
skill in each conversation's sandbox, triggerable:

- **explicitly**: `/<slug>` (with arguments) typed in the chat;
- **automatically**: the model uses it when its `description` matches the task.

## 2. Model — one skill = one SKILL.md

A skill is edited as **a single `content` field** = a **complete SKILL.md** (frontmatter + body), in
native Claude Code format:

```markdown
---
name: summarize-gitlab-mr
description: Summarize a GitLab merge request for a reviewer…
arguments: [mr_url, --publish]
---
<instructions… $ARGUMENTS / $1 / $2>
```

- The backend parses the frontmatter (`app/core/skill_md.py`, `yaml.safe_load`) and derives:
  - `name` → normalized **slug** (`slugify`, `[a-z0-9-]`) = folder `skills/<slug>/` = trigger `/<slug>` =
    `name:` rewritten in the file (folder / trigger / file consistent — "Option B").
  - `description`, `arguments` → `argument_hint`.
- **Storage** (table `skills`, **no schema migration**): `name` (slug, **unique**) + `description`
  **denormalized** (fast list/search); `content` = the raw SKILL.md (normalized name). `slug` and
  `argument_hint` are **not** stored → exposed as **computed fields**.
- Entity renamed `prompt_template → skill` everywhere: table, API `/api/skills`, model `Skill`, types, store,
  components.

## 3. Backend

### 3.1 Parsing — `app/core/skill_md.py`
- `parse_skill_md(content) -> (slug, description, argument_hint, normalized_content)`: normalizes CRLF→LF,
  extracts the frontmatter, `yaml.safe_load`, **validates** (frontmatter + `name` required → otherwise `ValueError`),
  rewrites `name:` as the slug.
- `frontmatter_argument_hint(content)`: hint from `arguments` / `argument-hint` (None if absent/invalid).
- `legacy_to_skill_md(name, body, description, slug=None)`: rebuilds a SKILL.md from the old format
  (`name` with `(...)` + body), for the migration; explicit `slug` to resolve a collision.

### 3.2 API — `app/api/routes/skills.py`
- `POST` / `PATCH` receive **`content` only** → parse → **422** if the frontmatter is invalid, **409** if the slug
  collides. `name`(slug) / `description` / `content` are stored.
- `SkillResponse` (`app/models/schemas.py`): `name`, `content`, `description`, `is_active`, timestamps +
  **computed** `slug` (= `slugify(name)`) and `argument_hint` (parsed from the frontmatter).

### 3.3 Generation — `app/services/session_skills.py`
- `apply_session_skills(session_id, skills)`: writes each `content` **verbatim** to
  `<session_dir>/claude/skills/<slug>/SKILL.md` (mounted at `/home/sandbox/.claude/skills/…` via
  `CLAUDE_CONFIG_DIR`). Desired-state reconciliation (prunes orphan folders), atomic write,
  idempotent, no-op outside the sandbox — modeled on `mcp_config.py`.
- Called **on every turn** by the orchestrator (`get_active_skills` → `apply_session_skills`): the CLI reads its
  config at launch, so an edit propagates to each session's next turn; survives a sandbox
  rebuild.

### 3.4 Triggering — `app/services/orchestrator_cli.py`
- The user message goes **verbatim** to `claude -p` → a `/skill` expands **anywhere** in the
  message. The runtime context (cluster access + connectors + investigation instruction) is in
  `--append-system-prompt`, **not** in the user turn (otherwise it would pollute `$ARGUMENTS`).
- ⚠️ `$ARGUMENTS` = **the rest of the line** (Claude Code behavior). A literal `$` before a digit or
  `ARGUMENTS` in the body must be escaped `\$` (e.g. a price `\$5`).

### 3.5 Legacy migration — `ensure_skills_frontmatter(db)` (startup)
- Idempotent: converts rows whose `content` does not start with `---` (old body-only format)
  into SKILL.md (frontmatter rebuilt from `name (...)` / `description`). **Slug dedup** (suffix
  `-2`, `-3`…) so the uniqueness of `name` is never violated. Called in the lifespan (`app/main.py`) under
  `try/except` + `rollback` → **never blocks startup**.

## 4. Frontend

### 4.1 Admin — `pages/admin/SkillsPage.tsx`
- Form = **a single "SKILL.md" textarea** (modal `size="xl"`, `rows=22`). `name`/`description`/
  `arguments` come from the frontmatter (parsed server-side); `adminApi` Create/Update = `{ content }`. The `detail`
  of a 422 (invalid frontmatter) is surfaced to the form (`services/api.ts`).

### 4.2 Chat — `ChatInput.tsx` + `SkillAutocomplete.tsx`
- `/` autocompletion **mid-token** (token under the cursor, anywhere); source = the skills store
  (`useSkillsStore`), filtered on slug/name/description, loaded on mount. Keyboard ↑/↓, Enter (selects,
  **does not send**), Esc, Tab; selection inserts `/<slug> `.
- **Hint line** under the composer when a known `/<slug>` is typed:
  `/<slug> [arg1] [arg2] · <description>` (`formatArgs` puts the args in brackets; fallback
  "free arguments" without a hint).
- `SkillPalette.tsx` (formerly `PromptTemplatePalette`): selecting inserts `/<slug> ` at the cursor.

## 5. Security / robustness
- `yaml.safe_load` (never `load`) on admin content.
- `slug` ∈ `[a-z0-9-]` (`slugify`) → **no path traversal** on the folder; `name:` normalized.
- Migration: slug dedup **+** `try/except`/`rollback` → never crashes at boot.
- CRLF normalized at parser input.
- **Admin-curated** content (write = admin); executed in the isolated sandbox (read-only connectors).

## 6. Tests
- `backend/tests/test_session_skills.py`: parse (valid / CRLF / invalid / normalization / hint shapes),
  `legacy_to_skill_md`, verbatim generation + prune, migration (conversion + **collision dedup**),
  computed fields.
- `backend/tests/test_api_skills.py`: create/update (content), 422 invalid frontmatter, 409 collision, RBAC.
- `frontend/src/components/chat/ChatInput.test.tsx`: autocompletion (`/`, filter, keyboard, mid-message),
  hint line (generic + specific), insertion.

## 7. Code references

| Topic | Location |
|---|---|
| SKILL.md parsing / slug | `backend/app/core/skill_md.py`, `backend/app/core/slug.py` |
| Model / schemas | `backend/app/models/skill.py`, `backend/app/models/schemas.py` |
| API | `backend/app/api/routes/skills.py` (`/api/skills`) |
| Generation + migration | `backend/app/services/session_skills.py`; startup `backend/app/main.py` |
| Orchestrator | `backend/app/services/orchestrator_cli.py` (verbatim + `--append-system-prompt`) |
| Sandbox | `backend/app/services/sandbox.py` (`CLAUDE_CONFIG_DIR`, `session_dir`) |
| Front | `frontend/src/components/chat/{ChatInput,SkillAutocomplete,SkillPalette}.tsx`, `pages/admin/SkillsPage.tsx`, `store/skills.ts`, `store/admin.ts`, `types/api.ts`, `services/{api,adminApi}.ts` |
