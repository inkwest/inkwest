"""Real-SQL tests for the shared usage aggregation (app.services.usage_reports).

The API-level tests mock ``db.execute`` and therefore never execute the grouping SQL;
these run the queries against aiosqlite so the ``by_turn`` contract (COALESCE grouping,
NULL-turn_id per-row fallback, user/session scoping, ordering) is actually pinned.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest_asyncio
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import app.models  # noqa: F401  -- registers all tables on Base.metadata
from app.models.base import Base
from app.models.budget import UsageRecord
from app.models.session import Session as ChatSession
from app.services import usage_reports

# Idempotent re-registration (same pattern as test_messages.py).


@compiles(JSONB, "sqlite")
def _render_jsonb_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


@compiles(UUID, "sqlite")
def _render_uuid_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(36)"


_T0 = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)

_USER = uuid.uuid4()
_OTHER_USER = uuid.uuid4()
_SESSION = uuid.uuid4()
_OTHER_SESSION = uuid.uuid4()
_TURN = uuid.uuid4()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    # sessions joins in for by_session's title lookup (no rows seeded ⇒ "Untitled");
    # sqlite doesn't enforce its FKs, so the referenced tables can stay absent.
    tables = cast("list[Table]", [UsageRecord.__table__, ChatSession.__table__])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def _record(minute: int, **overrides: Any) -> UsageRecord:
    base: dict[str, Any] = {
        "user_id": _USER,
        "session_id": _SESSION,
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "model": "anthropic.claude-opus-4-8",
        "provider": "claude-cli",
        "source": "broker",
        "cost_usd": Decimal("0.01"),
        "recorded_at": _T0 + timedelta(minutes=minute),
    }
    base.update(overrides)
    return UsageRecord(**base)


async def _seed(db: AsyncSession) -> None:
    db.add_all(
        [
            # Two broker calls sharing one turn — must aggregate into a single group.
            _record(0, turn_id=_TURN, input_tokens=10, cache_read_tokens=27082),
            _record(1, turn_id=_TURN, input_tokens=20, output_tokens=880, cache_write_tokens=3242),
            # Pre-cutover row (turn_id NULL) — must stay its own per-row group.
            _record(2, turn_id=None, input_tokens=365, output_tokens=17),
            # Another session for the same user — excluded by the session filter.
            _record(3, turn_id=uuid.uuid4(), session_id=_OTHER_SESSION),
            # Another user entirely — never visible to _USER.
            _record(4, turn_id=uuid.uuid4(), user_id=_OTHER_USER),
        ]
    )
    await db.commit()


async def test_by_turn_groups_by_turn_id_and_degrades_null(db: AsyncSession) -> None:
    await _seed(db)
    rows = await usage_reports.by_turn(db, _USER, _T0 - timedelta(days=1), None)

    # 3 groups for _USER: the shared turn, the NULL-turn per-row fallback, the other session.
    assert len(rows) == 3
    by_key = {r["turn_id"]: r for r in rows}
    turn = by_key[str(_TURN)]
    assert turn["call_count"] == 2
    assert turn["input_tokens"] == 30
    assert turn["output_tokens"] == 885
    assert turn["cache_read_tokens"] == 27082
    assert turn["cache_write_tokens"] == 3242
    assert turn["cost_usd"] == 0.02
    # NULL-turn_id row grouped under its own UsageRecord id (per-row fallback).
    solo = [r for r in rows if r["call_count"] == 1 and r["input_tokens"] == 365]
    assert len(solo) == 1
    # Newest-first ordering (other-session row recorded last among _USER rows).
    assert rows[0]["session_id"] == str(_OTHER_SESSION)


async def test_by_turn_session_filter_scopes_rows(db: AsyncSession) -> None:
    await _seed(db)
    rows = await usage_reports.by_turn(
        db, _USER, _T0 - timedelta(days=1), None, session_id=_SESSION
    )
    assert {r["session_id"] for r in rows} == {str(_SESSION)}
    assert len(rows) == 2  # shared turn + NULL-turn fallback


async def test_by_turn_other_user_invisible(db: AsyncSession) -> None:
    await _seed(db)
    rows = await usage_reports.by_turn(db, _OTHER_USER, _T0 - timedelta(days=1), None)
    assert len(rows) == 1
    assert rows[0]["session_id"] == str(_SESSION)


async def test_summary_sums_cache_columns(db: AsyncSession) -> None:
    await _seed(db)
    totals = await usage_reports.summary(db, _USER, _T0 - timedelta(days=1), None)
    assert totals["total_cache_read_tokens"] == 27082
    assert totals["total_cache_write_tokens"] == 3242
    # total_tokens keeps pre-cache semantics (fresh input + output only).
    assert totals["total_tokens"] == (10 + 20 + 365 + 10) + (5 + 880 + 17 + 5)


async def test_by_turn_limit_caps_groups(db: AsyncSession) -> None:
    await _seed(db)
    rows = await usage_reports.by_turn(db, _USER, _T0 - timedelta(days=1), None, limit=1)
    assert len(rows) == 1
    # newest-first: the other-session row was recorded last among _USER rows
    assert rows[0]["session_id"] == str(_OTHER_SESSION)


async def test_by_model_sums_cache_columns(db: AsyncSession) -> None:
    await _seed(db)
    rows = await usage_reports.by_model(db, _USER, _T0 - timedelta(days=1), None)
    assert len(rows) == 1  # one (model, provider) group across all _USER rows
    assert rows[0]["cache_read_tokens"] == 27082
    assert rows[0]["cache_write_tokens"] == 3242
    assert rows[0]["total_tokens"] == (10 + 20 + 365 + 10) + (5 + 880 + 17 + 5)


async def test_by_session_sums_cache_columns(db: AsyncSession) -> None:
    await _seed(db)
    rows = await usage_reports.by_session(db, _USER, _T0 - timedelta(days=1), None)
    by_id = {r["session_id"]: r for r in rows}
    assert by_id[str(_SESSION)]["cache_read_tokens"] == 27082
    assert by_id[str(_SESSION)]["cache_write_tokens"] == 3242
    assert by_id[str(_OTHER_SESSION)]["cache_read_tokens"] == 0


class _SpyDB:
    """Captures the statement instead of executing it — for dialect-level SQL pins on
    queries aiosqlite can't run (date_trunc) or can't falsify (max over uuid)."""

    def __init__(self) -> None:
        self.stmts: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        self.stmts.append(stmt)

        class _Empty:
            def all(self) -> list[Any]:
                return []

        return _Empty()


def _pg_sql(stmt: Any) -> str:
    from sqlalchemy.dialects import postgresql as pg

    return str(stmt.compile(dialect=pg.dialect())).lower()


async def test_by_turn_pg_sql_never_max_aggregates_uuid_columns() -> None:
    """Regression pin for d61cbb8: PostgreSQL has no max(uuid), but sqlite happily
    max()es CHAR(36) — so the aiosqlite suite above cannot catch a reintroduction.
    Pin the PostgreSQL-compiled SQL: uuid columns are grouped, never max-aggregated."""
    spy = _SpyDB()
    await usage_reports.by_turn(cast("AsyncSession", spy), _USER, _T0, None)
    sql = _pg_sql(spy.stmts[0])
    for uuid_col in ("session_id", "turn_id", "user_id", "id"):
        assert f"max(usage_records.{uuid_col})" not in sql
    assert "group by" in sql and "usage_records.session_id" in sql.split("group by")[1]


async def test_timeseries_pg_sql_sums_cache_columns() -> None:
    """sqlite has no date_trunc, so timeseries can't run on the aiosqlite fixture —
    pin the cache-column sums at the compiled-SQL level instead."""
    spy = _SpyDB()
    await usage_reports.timeseries(cast("AsyncSession", spy), None, "day", _T0, None)
    sql = _pg_sql(spy.stmts[0])
    assert "sum(usage_records.cache_read_tokens)" in sql
    assert "sum(usage_records.cache_write_tokens)" in sql
