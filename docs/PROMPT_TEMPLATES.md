# Prompt Templates

> A library of reusable prompt templates, offered as an editable
> palette in the chat. The user picks a template, its content lands in the chat
> input, stays fully editable, then is sent like any other message.

## TL;DR

- **Global** templates (shared by all users; no per-user ownership).
- Content is **plain text** with literal fill-in blanks (e.g. `__________`) the
  user edits inline before sending. **No** placeholder engine / substitution.
- CRUD is admin-only and mirrors the **clusters** vertical end-to-end.
- The chat **palette** only *inserts* content into the input — nothing is sent
  automatically.

## Data model

Table `inkwest.prompt_templates` (see [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md)):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `uuid_generate_v4()` |
| `name` | VARCHAR(255) | unique, indexed — shown in the palette |
| `content` | TEXT | the prompt body; may contain `__________` blanks |
| `description` | TEXT, nullable | optional, shown as a hint |
| `is_active` | BOOLEAN, default `true` | enable/disable flag (palette visibility) |
| `created_at` / `updated_at` | TIMESTAMPTZ | `TimestampMixin` |

SQLAlchemy model: `backend/app/models/prompt_template.py`.
Migration: `backend/migrations/20260608120000_prompt_templates.sql` (dbmate format).

## API

Router `backend/app/api/routes/prompt_templates.py`, mounted at `/api/prompt-templates`.
Reads require authentication; writes require admin (same dependency pattern as clusters).

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/prompt-templates?active_only=true` | user | List templates (active only by default) |
| `POST` | `/api/prompt-templates` | admin | Create (409 if `name` exists) |
| `GET` | `/api/prompt-templates/{id}` | user | Fetch one (404 if missing) |
| `PATCH` | `/api/prompt-templates/{id}` | admin | Update; renaming to a taken `name` → 409 |
| `DELETE` | `/api/prompt-templates/{id}` | admin | Hard delete (row removed, name freed) → 204 |

Schemas: `PromptTemplate{Base,Create,Update,Response,List}` in `backend/app/models/schemas.py`.

## Frontend

### Management screen (admin)

- Page: `frontend/src/pages/admin/PromptTemplatesPage.tsx`, route `/admin/prompt-templates`,
  nav entry in `AdminLayout.tsx`.
- Store: `usePromptTemplateStore` in `frontend/src/store/admin.ts` (CRUD), backed by
  `frontend/src/services/adminApi.ts`.
- **Search filter** over name / description / content, mirroring the AWS
  role-selection filter (`AwsAccountRoleModal`). Client-side, case-insensitive.
- Create/edit modal: `name` (Input), `content` (multiline textarea), `description` (Input).

### Chat palette

- Component: `frontend/src/components/chat/PromptTemplatePalette.tsx` — a button
  rendered next to the chat input (`ChatInput.tsx`). Opens a searchable popover
  listing active templates.
- Read-only store: `usePromptTemplatesStore` in `frontend/src/store/promptTemplates.ts`
  (fetches `/api/prompt-templates`). Refetched each time the palette opens.
- On select: the template content **replaces** the chat input value (matching the
  existing `initialPrompt` behavior), the textarea is focused, and the user edits
  the blanks before sending. Nothing is auto-sent.

## Definition of Done

From the chat, open the palette → choose a template → it lands in the input →
editable → send. A management screen provides full CRUD over the library. ✅

## Decisions (was "À cadrer")

| Question | Decision |
|---|---|
| Scope | **Global** (no per-user) |
| Search / filter | **Yes**, in the management page and the palette |
| Placeholders | **No** — literal `__________` blanks edited inline, no substitution |
