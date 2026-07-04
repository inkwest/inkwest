"""
Usage report queries.

Shared aggregation over UsageRecord, used by both the admin reports
(`/api/admin/usage/*`, all users) and the self-service reports
(`/api/me/usage/*`, scoped to the current user). Passing `user_id=None`
aggregates across every user; a concrete id scopes to that user.
"""

import datetime as dt
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.budget import BudgetPeriod, UsageRecord
from app.models.session import Session
from app.services.budget_service import get_period_start


def resolve_window(
    period: BudgetPeriod,
    start_date: dt.date | None,
    end_date: dt.date | None,
) -> tuple[datetime, datetime | None]:
    """Resolve a [start, end] window from explicit dates or a budget period."""
    if start_date:
        start = datetime.combine(start_date, dt.time.min, tzinfo=UTC)
        end = datetime.combine(end_date, dt.time.max, tzinfo=UTC) if end_date else None
        return start, end
    return get_period_start(period), None


def _conditions(
    user_id: UUID | None,
    start: datetime,
    end: datetime | None,
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [UsageRecord.recorded_at >= start]
    if end:
        conditions.append(UsageRecord.recorded_at <= end)
    if user_id is not None:
        conditions.append(UsageRecord.user_id == user_id)
    return conditions


async def summary(
    db: AsyncSession,
    user_id: UUID | None,
    start: datetime,
    end: datetime | None,
) -> dict[str, Any]:
    """Total tokens/cost/requests over the window."""
    # Labeled columns (not positional indexes): inserting a column can never silently
    # renumber its neighbours.
    query = select(
        func.coalesce(func.sum(UsageRecord.input_tokens), 0).label("input_tokens"),
        func.coalesce(func.sum(UsageRecord.output_tokens), 0).label("output_tokens"),
        func.coalesce(func.sum(UsageRecord.cache_read_tokens), 0).label("cache_read_tokens"),
        func.coalesce(func.sum(UsageRecord.cache_write_tokens), 0).label("cache_write_tokens"),
        func.coalesce(func.sum(UsageRecord.cost_usd), 0).label("cost_usd"),
        func.count(UsageRecord.id).label("request_count"),
    ).where(*_conditions(user_id, start, end))

    row = (await db.execute(query)).one()
    input_tokens = int(row.input_tokens)
    output_tokens = int(row.output_tokens)
    # total_tokens stays fresh-input + output (pre-cache semantics); the cache splits are
    # additive fields so no consumer's math silently changes.
    return {
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "total_cache_read_tokens": int(row.cache_read_tokens),
        "total_cache_write_tokens": int(row.cache_write_tokens),
        "total_tokens": input_tokens + output_tokens,
        "total_cost_usd": float(row.cost_usd),
        "request_count": int(row.request_count),
    }


async def by_model(
    db: AsyncSession,
    user_id: UUID | None,
    start: datetime,
    end: datetime | None,
) -> list[dict[str, Any]]:
    """Usage broken down by model, costliest first."""
    # `UsageRecord.provider` stores the engine that ran the turn, so grouping by
    # (model, provider) IS the model×engine breakdown — same model can appear once per
    # engine. Exposed as `engine` to match the admin report and the ModelUsage type.
    query = (
        select(
            UsageRecord.model,
            UsageRecord.provider.label("engine"),
            func.sum(UsageRecord.input_tokens).label("input_tokens"),
            func.sum(UsageRecord.output_tokens).label("output_tokens"),
            func.sum(UsageRecord.cache_read_tokens).label("cache_read_tokens"),
            func.sum(UsageRecord.cache_write_tokens).label("cache_write_tokens"),
            func.sum(UsageRecord.cost_usd).label("cost_usd"),
            func.count(UsageRecord.id).label("request_count"),
        )
        .where(*_conditions(user_id, start, end))
        .group_by(UsageRecord.model, UsageRecord.provider)
        .order_by(func.sum(UsageRecord.cost_usd).desc())
    )

    rows = (await db.execute(query)).all()
    return [
        {
            "model": row.model,
            "engine": row.engine,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_write_tokens": row.cache_write_tokens,
            "total_tokens": row.input_tokens + row.output_tokens,
            "cost_usd": float(row.cost_usd),
            "request_count": row.request_count,
        }
        for row in rows
    ]


async def by_session(
    db: AsyncSession,
    user_id: UUID | None,
    start: datetime,
    end: datetime | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Per-session usage breakdown (titles resolved), costliest first.

    `limit` caps the number of (costliest) sessions returned — keeps the
    payload and the title lookup bounded for accounts with many conversations.
    """
    query = (
        select(
            UsageRecord.session_id,
            func.sum(UsageRecord.input_tokens).label("input_tokens"),
            func.sum(UsageRecord.output_tokens).label("output_tokens"),
            func.sum(UsageRecord.cache_read_tokens).label("cache_read_tokens"),
            func.sum(UsageRecord.cache_write_tokens).label("cache_write_tokens"),
            func.sum(UsageRecord.cost_usd).label("cost_usd"),
            func.count(UsageRecord.id).label("request_count"),
            func.min(UsageRecord.recorded_at).label("first_request"),
            func.max(UsageRecord.recorded_at).label("last_request"),
        )
        .where(*_conditions(user_id, start, end))
        .group_by(UsageRecord.session_id)
        .order_by(func.sum(UsageRecord.cost_usd).desc())
    )
    if limit is not None:
        query = query.limit(limit)

    rows = (await db.execute(query)).all()

    session_ids = [row.session_id for row in rows]
    titles: dict[str, str] = {}
    if session_ids:
        result = await db.execute(
            select(Session.id, Session.title).where(Session.id.in_(session_ids))
        )
        titles = {str(s.id): s.title or "Untitled" for s in result.all()}

    return [
        {
            "session_id": str(row.session_id),
            "title": titles.get(str(row.session_id), "Untitled"),
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_write_tokens": row.cache_write_tokens,
            "total_tokens": row.input_tokens + row.output_tokens,
            "cost_usd": float(row.cost_usd),
            "request_count": row.request_count,
            "first_request": row.first_request.isoformat() if row.first_request else None,
            "last_request": row.last_request.isoformat() if row.last_request else None,
        }
        for row in rows
    ]


async def timeseries(
    db: AsyncSession,
    user_id: UUID | None,
    granularity: str,
    start: datetime,
    end: datetime | None,
) -> list[dict[str, Any]]:
    """Usage bucketed by hour or day."""
    if granularity not in {"hour", "day"}:
        raise ValueError(f"Invalid granularity: {granularity}")
    bucket = func.date_trunc(granularity, UsageRecord.recorded_at).label("bucket")

    query = (
        select(
            bucket,
            func.sum(UsageRecord.input_tokens).label("input_tokens"),
            func.sum(UsageRecord.output_tokens).label("output_tokens"),
            func.sum(UsageRecord.cache_read_tokens).label("cache_read_tokens"),
            func.sum(UsageRecord.cache_write_tokens).label("cache_write_tokens"),
            func.sum(UsageRecord.cost_usd).label("cost_usd"),
            func.count(UsageRecord.id).label("request_count"),
        )
        .where(*_conditions(user_id, start, end))
        .group_by(bucket)
        .order_by(bucket)
    )

    return [
        {
            "timestamp": row.bucket.isoformat(),
            "input_tokens": int(row.input_tokens),
            "output_tokens": int(row.output_tokens),
            "cache_read_tokens": int(row.cache_read_tokens),
            "cache_write_tokens": int(row.cache_write_tokens),
            "cost_usd": float(row.cost_usd),
            "request_count": int(row.request_count),
        }
        for row in (await db.execute(query)).all()
    ]


async def by_turn(
    db: AsyncSession,
    user_id: UUID | None,
    start: datetime,
    end: datetime | None,
    session_id: UUID | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Per-turn usage rollup: the N broker rows of one turn grouped by ``turn_id``.

    Pre-cutover rows (``turn_id IS NULL``) fall back to per-row granularity by grouping
    on the row's own id — they simply don't aggregate (see
    ``migrations/20260704000000_usage_cache_tokens.sql``). The returned ``turn_id`` is the
    grouping key, so for those legacy rows it carries the UsageRecord id. Newest turns
    first. No frontend consumer yet — groundwork for a per-turn cost drill-down
    (documented in docs/BUDGET.md).
    """
    turn_key = func.coalesce(UsageRecord.turn_id, UsageRecord.id).label("turn_key")
    conditions = _conditions(user_id, start, end)
    if session_id is not None:
        conditions.append(UsageRecord.session_id == session_id)

    # session_id joins the GROUP BY instead of being aggregated: a turn never spans
    # sessions (so the grouping is unchanged), and PostgreSQL has no max(uuid).
    query = (
        select(
            turn_key,
            UsageRecord.session_id,
            func.max(UsageRecord.model).label("model"),
            func.max(UsageRecord.provider).label("engine"),
            func.max(UsageRecord.source).label("source"),
            func.sum(UsageRecord.input_tokens).label("input_tokens"),
            func.sum(UsageRecord.output_tokens).label("output_tokens"),
            func.sum(UsageRecord.cache_read_tokens).label("cache_read_tokens"),
            func.sum(UsageRecord.cache_write_tokens).label("cache_write_tokens"),
            func.sum(UsageRecord.cost_usd).label("cost_usd"),
            func.count(UsageRecord.id).label("call_count"),
            func.max(UsageRecord.recorded_at).label("last_call"),
        )
        .where(*conditions)
        .group_by(turn_key, UsageRecord.session_id)
        .order_by(func.max(UsageRecord.recorded_at).desc())
    )
    if limit is not None:
        query = query.limit(limit)

    return [
        {
            "turn_id": str(row.turn_key),
            "session_id": str(row.session_id),
            "model": row.model,
            "engine": row.engine,
            "source": row.source,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_write_tokens": row.cache_write_tokens,
            "cost_usd": float(row.cost_usd),
            "call_count": row.call_count,
            "last_call": row.last_call.isoformat() if row.last_call else None,
        }
        for row in (await db.execute(query)).all()
    ]
