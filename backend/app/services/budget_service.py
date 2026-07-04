"""
Budget management service.

Handles budget enforcement, usage tracking, and limit checks.
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.models.budget import Budget, BudgetPeriod, BudgetType, UsageRecord
from app.services.app_settings import (
    BUDGET_ENABLED_KEY,
    DEFAULT_MONTHLY_COST_LIMIT_USD_KEY,
    get_bool_setting,
    get_float_setting,
)
from app.services.model_registry import calculate_cost, default_model

logger = logging.getLogger(__name__)

# Action applied to the zero-config default cap (no Budget row) — a true ceiling.
DEFAULT_LIMIT_ACTION = "block"
DEFAULT_SOFT_LIMIT_PERCENT = 80


class BudgetStatus(NamedTuple):
    """Current budget status."""

    is_allowed: bool
    remaining_tokens: int | None
    remaining_cost_usd: Decimal | None
    usage_percent: float
    warning_message: str | None
    budget_name: str | None
    # What to do at 100% (block | warn | notify) — drives request-path enforcement.
    # Trailing default keeps existing 6-arg constructions (and reporting) working.
    hard_limit_action: str | None = None


def is_budget_enabled() -> bool:
    """Whether spend budgets are enforced. Admin app-setting overrides the env default."""
    return get_bool_setting(BUDGET_ENABLED_KEY, get_settings().budget_enabled)


def default_monthly_cost_limit() -> Decimal | None:
    """Per-user default monthly USD cap, or None when unset (<= 0 = no default cap)."""
    value = get_float_setting(
        DEFAULT_MONTHLY_COST_LIMIT_USD_KEY,
        get_settings().default_monthly_cost_limit_usd,
    )
    return Decimal(str(value)) if value and value > 0 else None


class UsageSummary(NamedTuple):
    """Usage summary for a period."""

    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: Decimal
    request_count: int


def get_period_start(period: BudgetPeriod) -> datetime:
    """Get the start of the current budget period."""
    now = datetime.now(UTC)

    if period == BudgetPeriod.HOURLY:
        return now.replace(minute=0, second=0, microsecond=0)
    elif period == BudgetPeriod.DAILY:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == BudgetPeriod.WEEKLY:
        days_since_monday = now.weekday()
        start = now - timedelta(days=days_since_monday)
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == BudgetPeriod.MONTHLY:
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def get_usage_for_period(
    db: AsyncSession,
    user_id: UUID | None,
    period: BudgetPeriod,
) -> UsageSummary:
    """
    Get usage summary for a user in the current period.

    Args:
        db: Database session
        user_id: User ID (None for global usage)
        period: Budget period

    Returns:
        UsageSummary with token and cost totals
    """
    period_start = get_period_start(period)

    query = select(
        func.coalesce(func.sum(UsageRecord.input_tokens), 0),
        func.coalesce(func.sum(UsageRecord.output_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost_usd), Decimal("0")),
        func.count(UsageRecord.id),
    ).where(UsageRecord.recorded_at >= period_start)

    if user_id is not None:
        query = query.where(UsageRecord.user_id == user_id)

    result = await db.execute(query)
    row = result.one()

    input_tokens = int(row[0])
    output_tokens = int(row[1])

    return UsageSummary(
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        total_cost_usd=row[2],
        request_count=int(row[3]),
    )


async def get_applicable_budget(
    db: AsyncSession,
    user_id: UUID | None,
    user_roles: list[str] | None = None,
) -> Budget | None:
    """
    Get the most restrictive applicable budget for a user.

    Priority: User-specific > Role-specific > Global

    Args:
        db: Database session
        user_id: User ID
        user_roles: User's roles

    Returns:
        Most applicable Budget or None
    """
    # Try user-specific budget first. Tolerate duplicates (nothing stops an admin
    # creating two active user/global budgets): pick the newest deterministically
    # rather than letting scalar_one_or_none() raise — a raise here would be caught
    # upstream and fail open, silently disabling enforcement.
    if user_id:
        result = await db.execute(
            select(Budget)
            .where(
                Budget.budget_type == BudgetType.USER,
                Budget.target_id == user_id,
                Budget.is_active == True,  # noqa: E712
            )
            .order_by(Budget.created_at.desc())
        )
        user_budget = result.scalars().first()
        if user_budget:
            return user_budget

    # Try role-specific budget
    if user_roles:
        result = await db.execute(
            select(Budget)
            .where(
                Budget.budget_type == BudgetType.ROLE,
                Budget.target_role.in_(user_roles),
                Budget.is_active == True,  # noqa: E712
            )
            .order_by(Budget.max_total_tokens.asc().nullslast())
        )
        role_budget = result.scalars().first()
        if role_budget:
            return role_budget

    # Fall back to global budget (newest wins if duplicates exist — see note above).
    result = await db.execute(
        select(Budget)
        .where(
            Budget.budget_type == BudgetType.GLOBAL,
            Budget.is_active == True,  # noqa: E712
        )
        .order_by(Budget.created_at.desc())
    )
    return result.scalars().first()


_ALLOW_UNLIMITED = BudgetStatus(
    is_allowed=True,
    remaining_tokens=None,
    remaining_cost_usd=None,
    usage_percent=0.0,
    warning_message=None,
    budget_name=None,
    hard_limit_action=None,
)


def _evaluate_limits(
    *,
    name: str,
    action: str,
    soft_limit_percent: int,
    usage: UsageSummary,
    estimated_tokens: int,
    max_total_tokens: int | None,
    max_cost_usd: Decimal | None,
) -> BudgetStatus:
    """Evaluate BOTH token and cost limits, blocking on whichever is reached.

    The previous implementation returned at the first configured limit, so a budget
    that set both a token and a cost cap never enforced the cost one. Here both are
    checked and the most-consumed dimension drives the reported usage_percent.
    """
    remaining_tokens = (
        max(0, max_total_tokens - usage.total_tokens) if max_total_tokens is not None else None
    )
    remaining_cost = (
        max(Decimal("0"), max_cost_usd - usage.total_cost_usd) if max_cost_usd is not None else None
    )
    token_pct = (usage.total_tokens / max_total_tokens) * 100 if max_total_tokens else 0.0
    cost_pct = float((usage.total_cost_usd / max_cost_usd) * 100) if max_cost_usd else 0.0
    usage_percent = max(token_pct, cost_pct)

    # Tokens use `>` against an estimate (the next turn would cross the cap); cost uses
    # `>=` below because cost is only known after the turn, so we block once already at it.
    over_tokens = (
        max_total_tokens is not None and usage.total_tokens + estimated_tokens > max_total_tokens
    )
    # Cost is post-hoc (the real cost of a turn is only known after it runs), so the
    # cap blocks the *next* turn once cumulative spend has reached it — overshoot is
    # bounded to a single turn. See docs/BUDGET.md.
    over_cost = max_cost_usd is not None and usage.total_cost_usd >= max_cost_usd

    if over_tokens:
        return BudgetStatus(
            is_allowed=False,
            remaining_tokens=remaining_tokens,
            remaining_cost_usd=remaining_cost,
            usage_percent=usage_percent,
            warning_message=f"Token limit ({max_total_tokens:,}) exceeded for budget '{name}'",
            budget_name=name,
            hard_limit_action=action,
        )
    if over_cost:
        return BudgetStatus(
            is_allowed=False,
            remaining_tokens=remaining_tokens,
            remaining_cost_usd=Decimal("0"),
            usage_percent=max(usage_percent, 100.0),
            warning_message=f"Cost limit (${max_cost_usd}) exceeded for budget '{name}'",
            budget_name=name,
            hard_limit_action=action,
        )

    warning_message = None
    if usage_percent >= soft_limit_percent:
        warning_message = f"Warning: {usage_percent:.1f}% of budget '{name}' used"

    return BudgetStatus(
        is_allowed=True,
        remaining_tokens=remaining_tokens,
        remaining_cost_usd=remaining_cost,
        usage_percent=usage_percent,
        warning_message=warning_message,
        budget_name=name,
        hard_limit_action=action,
    )


async def check_budget(
    db: AsyncSession,
    user_id: UUID | None,
    user_roles: list[str] | None = None,
    estimated_tokens: int = 0,
) -> BudgetStatus:
    """
    Check if a request is within budget limits.

    Enforcement gate: when budgets are enabled, the request path
    (chat WS turn start) calls this and blocks a new turn when the returned
    status has ``is_allowed=False`` and ``hard_limit_action == "block"``.
    Resolution order for the applicable limit: user budget > role budget >
    global budget > the per-user default monthly cap from settings. See
    docs/BUDGET.md.

    Args:
        db: Database session
        user_id: User ID
        user_roles: User's roles
        estimated_tokens: Estimated tokens for the request

    Returns:
        BudgetStatus indicating if request is allowed
    """
    # If budget enforcement is disabled, always allow (no DB hit).
    if not is_budget_enabled():
        return _ALLOW_UNLIMITED

    budget = await get_applicable_budget(db, user_id, user_roles)

    # No budget row — fall back to the per-user default monthly USD cap (if any).
    if budget is None:
        default_cap = default_monthly_cost_limit()
        if default_cap is None:
            return _ALLOW_UNLIMITED
        usage = await get_usage_for_period(db, user_id, BudgetPeriod.MONTHLY)
        return _evaluate_limits(
            name="default",
            action=DEFAULT_LIMIT_ACTION,
            soft_limit_percent=DEFAULT_SOFT_LIMIT_PERCENT,
            usage=usage,
            estimated_tokens=estimated_tokens,
            max_total_tokens=None,
            max_cost_usd=default_cap,
        )

    # A budget with no limits configured at all imposes nothing.
    if budget.max_total_tokens is None and budget.max_cost_usd is None:
        return BudgetStatus(
            is_allowed=True,
            remaining_tokens=None,
            remaining_cost_usd=None,
            usage_percent=0.0,
            warning_message=None,
            budget_name=budget.name,
            hard_limit_action=budget.hard_limit_action,
        )

    usage = await get_usage_for_period(db, user_id, budget.period)
    return _evaluate_limits(
        name=budget.name,
        action=budget.hard_limit_action,
        soft_limit_percent=budget.soft_limit_percent,
        usage=usage,
        estimated_tokens=estimated_tokens,
        max_total_tokens=budget.max_total_tokens,
        max_cost_usd=budget.max_cost_usd,
    )


async def record_usage(
    db: AsyncSession,
    user_id: UUID | None,
    session_id: UUID,
    input_tokens: int,
    output_tokens: int,
    model: str | None = None,
    provider: str | None = None,
    cost_usd: Decimal | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    source: str = "engine",
    turn_id: UUID | None = None,
    record_id: UUID | None = None,
) -> UsageRecord:
    """
    Record token usage for budget tracking.

    Args:
        db: Database session
        user_id: User ID
        session_id: Chat session ID
        input_tokens: Fresh (uncached) input token count
        output_tokens: Output token count
        model: Model name (uses current config if not specified)
        provider: Engine that ran the turn (defaults to the current turn's engine)
        cost_usd: Engine-reported authoritative cost (wins over the estimate)
        cache_read_tokens: Tokens served from the prompt cache
        cache_write_tokens: Tokens written to the prompt cache (total, all TTLs)
        cache_write_1h_tokens: Portion of cache_write_tokens at 1h TTL (2x rate);
            feeds cost only — persisted inside cache_write_tokens
        source: Accounting authority — 'engine' (orchestrator) or 'broker' (LLM proxy)
        turn_id: Per-turn aggregation key (broker rows of one turn share it)
        record_id: Caller-fixed row PK. The broker persist retry reuses one id across
            attempts so an ambiguous first commit (landed server-side, ack lost) makes
            the re-INSERT collide on the PK instead of double-billing the call

    Returns:
        Created UsageRecord
    """
    # SECURITY: engine-reported counts arrive from the sandboxed process's stdout — a
    # compromised/prompt-injected agent could emit negative numbers, and a negative
    # cost_usd row would REDUCE the user's summed spend (budget-cap evasion broker-off).
    # Clamp at the sink; the broker path parses real wire bytes but shares the sink.
    input_tokens = max(0, int(input_tokens))
    output_tokens = max(0, int(output_tokens))
    cache_read_tokens = max(0, int(cache_read_tokens))
    cache_write_tokens = max(0, int(cache_write_tokens))
    cache_write_1h_tokens = max(0, int(cache_write_1h_tokens))

    if model is None:
        model = default_model()
    if provider is None:
        # Attribute usage to the engine that ran the turn — the same dimension the
        # token/cost metrics label by (the dispatcher sets it before orchestrating).
        provider = metrics.turn_engine()

    # Prefer an engine-reported authoritative cost (e.g. opencode's per-turn USD, which
    # accounts for cache-token pricing) over the token×rate estimate; fall back to the
    # estimate when the engine doesn't report one (Anthropic/Converse paths).
    # The token clamps above make the estimate non-negative, but an ENGINE-REPORTED cost
    # bypasses them — clamp it too, or the sink's stated budget-cap-evasion defense holds
    # only for callers that pre-validate (today just opencode's orchestrator).
    if cost_usd is not None and cost_usd < 0:
        cost_usd = Decimal(0)
    cost = (
        cost_usd
        if cost_usd is not None
        else calculate_cost(
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
        )
    )

    # Per-model token/cost rates for Prometheus: the resolved model is known here, and this
    # is the single sink every orchestrator funnels usage through. Best-effort — a metrics
    # hiccup must never block usage persistence below.
    try:
        engine = metrics.turn_engine()
        metrics.tokens_total.labels(model=model, direction="input", engine=engine).inc(input_tokens)
        metrics.tokens_total.labels(model=model, direction="output", engine=engine).inc(
            output_tokens
        )
        # Cache directions only when non-zero: most models/turns have no cache activity
        # and unconditional .inc(0) would mint empty series for every model.
        if cache_read_tokens:
            metrics.tokens_total.labels(model=model, direction="cache_read", engine=engine).inc(
                cache_read_tokens
            )
        if cache_write_tokens:
            metrics.tokens_total.labels(model=model, direction="cache_write", engine=engine).inc(
                cache_write_tokens
            )
        metrics.model_cost_usd_total.labels(model=model, engine=engine).inc(float(cost))
    except Exception:  # noqa: BLE001 — metrics are non-critical
        logger.warning("usage_metrics_emit_failed", exc_info=True)

    record = UsageRecord(
        **({"id": record_id} if record_id is not None else {}),
        user_id=user_id,
        session_id=session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        source=source,
        turn_id=turn_id,
        model=model,
        provider=provider,
        cost_usd=cost,
    )

    db.add(record)
    await db.flush()

    logger.debug(
        f"Recorded usage: user={user_id} tokens={input_tokens + output_tokens} cost=${cost:.6f}"
    )

    return record


async def get_user_usage_summary(
    db: AsyncSession,
    user_id: UUID,
    period: BudgetPeriod = BudgetPeriod.DAILY,
) -> dict[str, object]:
    """
    Get detailed usage summary for a user.

    Args:
        db: Database session
        user_id: User ID
        period: Budget period

    Returns:
        Dict with usage details
    """
    usage = await get_usage_for_period(db, user_id, period)
    # check_budget is used here purely for reporting (usage_percent, warnings).
    # is_allowed is surfaced for visibility, not acted upon — see docs/BUDGET.md.
    budget_status = await check_budget(db, user_id)

    return {
        "period": period.value,
        "period_start": get_period_start(period).isoformat(),
        "input_tokens": usage.total_input_tokens,
        "output_tokens": usage.total_output_tokens,
        "total_tokens": usage.total_tokens,
        "total_cost_usd": float(usage.total_cost_usd),
        "request_count": usage.request_count,
        "budget": {
            "name": budget_status.budget_name,
            "remaining_tokens": budget_status.remaining_tokens,
            "remaining_cost_usd": float(budget_status.remaining_cost_usd)
            if budget_status.remaining_cost_usd
            else None,
            "usage_percent": budget_status.usage_percent,
            "is_allowed": budget_status.is_allowed,
            "warning": budget_status.warning_message,
        },
    }
