# Budget & Usage Tracking

> budget/cost/token tracking is **informational only**. It is
> **never enforced**: no limit blocks, throttles, or interrupts a conversation.

## TL;DR

- Budget = **reporting & visibility**, not a quota. This is a deliberate design
  choice, not a missing feature or a bug.
- Every model turn records its real token usage and cost (`record_usage`) for
  dashboards and the per-user usage summary.
- `check_budget` exists and computes `is_allowed` / warnings, but its result is
  **only consumed by the admin stats endpoint** — nothing in the request path
  acts on it.
- No orchestrator and no `turn_runner.start` calls `check_budget` before
  invoking the model. A conversation always proceeds regardless of usage.

## What is tracked

- **Per-turn usage**: input/output tokens and computed cost are written to
  `usage_records` after each model response, across every provider orchestrator
  (`orchestrator.py`, `orchestrator_cli.py`, `orchestrator_azure.py`,
  `orchestrator_openai.py`, `orchestrator_openrouter.py`,
  `orchestrator_google.py`) via `record_usage`.
- **Budgets** (`budgets` table): optional Global / Role / User limits that admins
  can define in the **Admin → Budgets** UI. When `budget_enabled=True`, they
  drive the `usage_percent`, `remaining_*`, and `warning_message` fields of the
  usage summary.

> **`budget_enabled` gates the budget computation, not the usage tracking.**
> `budget_enabled` defaults to `False`. While it is off, `check_budget`
> short-circuits and returns empty budget fields (`is_allowed=True`,
> `usage_percent=0`, no warning, `budget_name=None`) — so defining a budget in
> the UI has **no effect at all**, not even on reporting. Raw token/cost usage
> (`record_usage` → `get_usage_for_period`) is recorded and reported
> **regardless** of `budget_enabled`. Either way, nothing blocks a conversation.

## What is NOT done

- No pre-flight check before a model call.
- No hard stop when a limit is reached: `is_allowed=False` is computed but the
  conversation is **not** interrupted.
- The `Add Budget` UI lets you record a target for visibility; it does **not**
  create an enforced quota.

## Where this lives in the code

| Concern | Location |
|---|---|
| Compute budget status (`is_allowed`, warnings) | `backend/app/services/budget_service.py::check_budget` |
| Only consumer of `check_budget` | `backend/app/services/budget_service.py::get_user_usage_summary` (admin stats) |
| Record real usage per turn | `budget_service.py::record_usage`, called by every orchestrator |
| Settings | `budget_enabled` (default `False`), `default_daily_token_limit`, `global_daily_token_limit` in `backend/app/core/config.py` |
| Admin UI | `frontend/src/pages/admin/BudgetsPage.tsx` |

## If enforcement is ever wanted

Enforcement would mean calling `check_budget` in the request path (e.g. in
`turn_runner.start` or at the top of each orchestrator's loop) and aborting the
turn when `is_allowed` is `False`. This is intentionally **not** implemented
today — document and review the product decision before adding it.
