"""
Self-service usage report routes (`/api/me/usage/*`).

Every endpoint is scoped to the authenticated user — a caller only ever sees
their own conversations and costs, admin or not. The cross-user admin reports
live separately under `/api/admin/usage/*`.
"""

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_db
from app.models.budget import BudgetPeriod
from app.services import usage_reports

router = APIRouter(prefix="/me/usage", tags=["usage"])


@router.get("/summary")
async def get_my_usage_summary(
    current_user: CurrentUser,
    period: BudgetPeriod = BudgetPeriod.MONTHLY,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Token/cost/request totals for the current user over the window."""
    start, end = usage_reports.resolve_window(period, start_date, end_date)
    totals = await usage_reports.summary(db, current_user.id, start, end)
    return {
        "period": "custom" if start_date else period.value,
        "period_start": start.isoformat(),
        **totals,
    }


@router.get("/by-model")
async def get_my_usage_by_model(
    current_user: CurrentUser,
    period: BudgetPeriod = BudgetPeriod.MONTHLY,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """The current user's usage broken down by model."""
    start, end = usage_reports.resolve_window(period, start_date, end_date)
    return await usage_reports.by_model(db, current_user.id, start, end)


@router.get("/sessions")
async def get_my_usage_sessions(
    current_user: CurrentUser,
    period: BudgetPeriod = BudgetPeriod.MONTHLY,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    limit: int = Query(default=1000, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Per-conversation usage breakdown for the current user (costliest first)."""
    start, end = usage_reports.resolve_window(period, start_date, end_date)
    return await usage_reports.by_session(db, current_user.id, start, end, limit=limit)


@router.get("/timeseries")
async def get_my_usage_timeseries(
    current_user: CurrentUser,
    granularity: str = Query(default="day", pattern="^(hour|day)$"),
    period: BudgetPeriod = BudgetPeriod.MONTHLY,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """The current user's usage bucketed by hour or day.

    `granularity` is constrained to hour|day by the Query pattern, so the
    service's own ValueError guard is unreachable here.
    """
    start, end = usage_reports.resolve_window(period, start_date, end_date)
    return await usage_reports.timeseries(db, current_user.id, granularity, start, end)
