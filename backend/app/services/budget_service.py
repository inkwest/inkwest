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

from app.core.config import get_settings
from app.models.budget import Budget, BudgetPeriod, BudgetType, UsageRecord
from app.services.model_registry import calculate_cost, default_model

logger = logging.getLogger(__name__)


class BudgetStatus(NamedTuple):
    """Current budget status."""

    is_allowed: bool
    remaining_tokens: int | None
    remaining_cost_usd: Decimal | None
    usage_percent: float
    warning_message: str | None
    budget_name: str | None


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
    # Try user-specific budget first
    if user_id:
        result = await db.execute(
            select(Budget).where(
                Budget.budget_type == BudgetType.USER,
                Budget.target_id == user_id,
                Budget.is_active == True,  # noqa: E712
            )
        )
        user_budget = result.scalar_one_or_none()
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

    # Fall back to global budget
    result = await db.execute(
        select(Budget).where(
            Budget.budget_type == BudgetType.GLOBAL,
            Budget.is_active == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def check_budget(
    db: AsyncSession,
    user_id: UUID | None,
    user_roles: list[str] | None = None,
    estimated_tokens: int = 0,
) -> BudgetStatus:
    """
    Check if a request is within budget limits.

    NOTE: Budget is informational only and is NOT enforced. The
    returned `is_allowed` is consumed solely by `get_user_usage_summary` for
    admin reporting — no orchestrator nor `turn_runner.start` calls this before
    invoking the model, so a conversation is never blocked or interrupted by a
    limit. This is a deliberate product choice (visibility, not quota). See
    docs/BUDGET.md before wiring this into the request path.

    Args:
        db: Database session
        user_id: User ID
        user_roles: User's roles
        estimated_tokens: Estimated tokens for the request

    Returns:
        BudgetStatus indicating if request is allowed
    """
    settings = get_settings()

    # If budget enforcement is disabled, always allow
    if not settings.budget_enabled:
        return BudgetStatus(
            is_allowed=True,
            remaining_tokens=None,
            remaining_cost_usd=None,
            usage_percent=0.0,
            warning_message=None,
            budget_name=None,
        )

    budget = await get_applicable_budget(db, user_id, user_roles)

    # No budget configured - use default limits from settings
    if budget is None:
        # Use global defaults
        max_tokens = settings.default_daily_token_limit
        usage = await get_usage_for_period(db, user_id, BudgetPeriod.DAILY)

        if usage.total_tokens + estimated_tokens > max_tokens:
            return BudgetStatus(
                is_allowed=False,
                remaining_tokens=max(0, max_tokens - usage.total_tokens),
                remaining_cost_usd=None,
                usage_percent=(usage.total_tokens / max_tokens) * 100,
                warning_message=f"Daily token limit ({max_tokens:,}) exceeded",
                budget_name="default",
            )

        return BudgetStatus(
            is_allowed=True,
            remaining_tokens=max_tokens - usage.total_tokens,
            remaining_cost_usd=None,
            usage_percent=(usage.total_tokens / max_tokens) * 100,
            warning_message=None,
            budget_name="default",
        )

    # Get current usage for the budget period
    usage = await get_usage_for_period(db, user_id, budget.period)

    # Check token limit
    if budget.max_total_tokens is not None:
        if usage.total_tokens + estimated_tokens > budget.max_total_tokens:
            return BudgetStatus(
                is_allowed=False,
                remaining_tokens=max(0, budget.max_total_tokens - usage.total_tokens),
                remaining_cost_usd=None,
                usage_percent=(usage.total_tokens / budget.max_total_tokens) * 100,
                warning_message=(
                    f"Token limit ({budget.max_total_tokens:,}) exceeded for budget '{budget.name}'"
                ),
                budget_name=budget.name,
            )

        usage_percent = (usage.total_tokens / budget.max_total_tokens) * 100
        remaining_tokens = budget.max_total_tokens - usage.total_tokens

        # Check soft limit warning
        warning_message = None
        if usage_percent >= budget.soft_limit_percent:
            warning_message = (
                f"Warning: {usage_percent:.1f}% of token budget used "
                f"({usage.total_tokens:,} / {budget.max_total_tokens:,})"
            )

        return BudgetStatus(
            is_allowed=True,
            remaining_tokens=remaining_tokens,
            remaining_cost_usd=None,
            usage_percent=usage_percent,
            warning_message=warning_message,
            budget_name=budget.name,
        )

    # Check cost limit
    if budget.max_cost_usd is not None:
        if usage.total_cost_usd >= budget.max_cost_usd:
            return BudgetStatus(
                is_allowed=False,
                remaining_tokens=None,
                remaining_cost_usd=Decimal("0"),
                usage_percent=100.0,
                warning_message=(
                    f"Cost limit (${budget.max_cost_usd}) exceeded for budget '{budget.name}'"
                ),
                budget_name=budget.name,
            )

        remaining_cost = budget.max_cost_usd - usage.total_cost_usd
        usage_percent = float((usage.total_cost_usd / budget.max_cost_usd) * 100)

        warning_message = None
        if usage_percent >= budget.soft_limit_percent:
            warning_message = (
                f"Warning: {usage_percent:.1f}% of cost budget used "
                f"(${usage.total_cost_usd:.4f} / ${budget.max_cost_usd})"
            )

        return BudgetStatus(
            is_allowed=True,
            remaining_tokens=None,
            remaining_cost_usd=remaining_cost,
            usage_percent=usage_percent,
            warning_message=warning_message,
            budget_name=budget.name,
        )

    # No limits configured
    return BudgetStatus(
        is_allowed=True,
        remaining_tokens=None,
        remaining_cost_usd=None,
        usage_percent=0.0,
        warning_message=None,
        budget_name=budget.name,
    )


async def record_usage(
    db: AsyncSession,
    user_id: UUID | None,
    session_id: UUID,
    input_tokens: int,
    output_tokens: int,
    model: str | None = None,
    provider: str | None = None,
) -> UsageRecord:
    """
    Record token usage for budget tracking.

    Args:
        db: Database session
        user_id: User ID
        session_id: Chat session ID
        input_tokens: Input token count
        output_tokens: Output token count
        model: Model name (uses current config if not specified)
        provider: Provider name

    Returns:
        Created UsageRecord
    """
    settings = get_settings()

    if model is None:
        model = default_model()
    if provider is None:
        provider = settings.ai_provider

    cost = calculate_cost(model, input_tokens, output_tokens)

    record = UsageRecord(
        user_id=user_id,
        session_id=session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
