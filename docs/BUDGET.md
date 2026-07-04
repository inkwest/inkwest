# Budget & Usage Tracking

> Per-user spend budgets are **enforced** when enabled: once a user reaches their
> limit for the period, a new turn is blocked. Enforcement is **off by default**
> (`budget_enabled=False`) — while off, usage is tracked for reporting only and
> nothing is blocked.

## TL;DR

- Every model turn records its real token usage and cost (`record_usage`) for
  dashboards and the per-user usage summary — **always**, regardless of whether
  enforcement is on.
- When enforcement is **on**, the chat WS turn-start path calls `check_budget`
  before starting a turn and blocks it when the user is over a `block` limit.
- Limits are expressed in **USD** (everything is priced in USD end-to-end).
- Admins manage it in **Admin → Budgets**: an enforcement toggle + a per-user
  default monthly cap, plus optional per-user / per-role / global budget rows.

## What is tracked

- **Per-turn usage**: input/output tokens and computed cost are written to
  `usage_records` after each model response, across every provider orchestrator
  via `record_usage`.
- **Prompt-cache tokens** (`cache_read_tokens` / `cache_write_tokens` columns):
  on the Anthropic/Bedrock wire, `input_tokens` is the FRESH (uncached) input
  only — cache tokens are reported separately and billed at their own rates
  (reads ~0.1×, 5m-TTL writes ~1.25×, 1h-TTL writes 2× the input rate; see
  `pricing.cost_for_pricing`). Cache is **input-side only** — output is never
  cached.
- **Source of truth** (`source` column): with the LLM auth broker enabled
  (`llm_broker_enabled`), the broker meters **each Bedrock call** from the exact
  wire data AWS bills (`invocationMetrics` / Converse `usage`) and writes one
  `source='broker'` row per call; the orchestrators then skip their own budget
  row (no double count) but keep session stats + the per-message token counts.
  Broker off ⇒ one `source='engine'` row per turn, from the engine-reported
  usage (cache-aware since the cutover).
- **Per-turn rollup** (`turn_id` column): the N broker rows of one turn share a
  `turn_id`, exposed via `GET /api/me/usage/turns` ("this turn cost $X across
  N calls"). Pre-cutover rows have `turn_id IS NULL` and degrade to per-call
  granularity.
- **Budgets** (`budgets` table): optional Global / Role / User limits. A budget
  may cap cost (`max_cost_usd`), tokens (`max_total_tokens`), or both — and both
  are evaluated (whichever is reached first blocks).

> **Cutover 2026-07-04**: usage rows recorded before this date predate
> cache-aware accounting — they under-count cache-heavy turns (observed ~10×
> on a real production bill). No backfill;
> comparisons across the cutover are apples-to-oranges.

## Enforcement

When enabled, `chat.py` runs `check_budget` before starting a turn (both the
`message` and `retry` paths) and refuses to start it when the resolved status is
`is_allowed=False` **and** the limit's `hard_limit_action == "block"`. The user
sees an error message in chat; nothing is spent.

- **Resolution order** for the applicable limit: user budget > role budget >
  global budget > the per-user default monthly cap (settings). The first match
  wins.
- **Post-hoc cost**: a turn's real cost is only known after it runs, so the cap
  blocks the *next* turn once cumulative spend has reached the limit. Overshoot
  is bounded to a single turn.
- **Actions**: `block` is the only action that bars a turn. `warn` / `notify`
  budgets compute the overage for reporting but never block.
- **Fail-open**: a budget-check error (e.g. transient DB fault) allows the turn
  rather than wedging chat.

### Settings

Two runtime knobs, admin-editable without a redeploy (app settings override the
env baselines):

| Setting | Env baseline | Meaning |
|---|---|---|
| `budget_enabled` | `BUDGET_ENABLED` (`False`) | Whether budgets are enforced at all. |
| `default_monthly_cost_limit_usd` | `DEFAULT_MONTHLY_COST_LIMIT_USD` (`0`) | Per-user default monthly USD cap for users with no budget row. `0` = no default cap. |

Managed via `PUT /api/admin/settings/budget` (and the Budgets admin page).

## Where this lives in the code

| Concern | Location |
|---|---|
| Compute budget status (`is_allowed`, action, warnings) | `backend/app/services/budget_service.py::check_budget` |
| Enforcement gate (request path) | `backend/app/api/routes/chat.py::_budget_block_reason` |
| Record real usage per turn | `budget_service.py::record_usage`, called by every orchestrator |
| Enforcement settings | `is_budget_enabled` / `default_monthly_cost_limit` in `budget_service.py`; keys in `app_settings.py` |
| Admin settings endpoint | `admin.py::get_budget_settings` / `update_budget_settings` |
| Budget CRUD | `admin.py` `/budgets` endpoints |
| Admin UI | `frontend/src/pages/admin/BudgetsPage.tsx` |

## Notes & limitations

- **Identity = scope.** A budget is identified by its scope, not a free-text name:
  the name is derived on create (user → the user's display name; role → the role;
  else `"Global default"`). The admin UI offers **one active budget per scope+target**
  (`POST /budgets` returns `409` on a duplicate). The guard is app-level (no DB
  unique index), so a re-activation or concurrent create could still leave two
  active — `get_applicable_budget` resolves that newest-wins.
- **Per-member drill-down.** Role/global budgets are per-member quotas, so their
  consumption is exposed per member via `GET /budgets/{id}/members` (the Budgets
  table's expandable rows), not as a single aggregate.
- **"Global" budget = per-user default**, not a shared org-wide pool: its usage
  is summed per-user, so each user gets a quota of that size. (A true org-wide
  pool would query usage with `user_id=None`.)
- **Concurrency**: the pre-turn check is one active turn *per session*, not per
  user. A user driving two sessions concurrently can have both pass the check and
  both spend — consistent with the single-replica caveats elsewhere; overshoot
  stays bounded.
- **Token caps ignore cache tokens.** `max_total_tokens` sums fresh input +
  output only — cache reads/writes don't count toward token budgets (their
  *cost* does, via `cost_usd`). Deliberate for now: pricing already weights
  cache correctly, and counting a 0.1×-priced cache read as a full token would
  distort token caps the other way.
