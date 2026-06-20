"""Real-engine tests for model_registry persistence.

The unit tests in test_model_registry.py drive the writes against an AsyncMock
DB. Here we exercise the actual SQL two ways:

- portable paths (add_custom_model via ORM insert, delete_model via core delete,
  load_registry via select) run against a real in-memory SQLite engine;
- the Postgres-specific paths (set_model_settings / update_model use
  ``pg_insert`` ON CONFLICT and a partial-index default dance that SQLite can't
  run) are compiled under the Postgres dialect so a malformed statement fails
  here rather than only in production.

The partial unique index on ``is_default`` lives in the migration, not the ORM
model, so SQLite create_all does not reproduce it — that constraint still needs
a real Postgres to exercise.
"""

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import app.models  # noqa: F401  -- registers all tables on Base.metadata
from app.models.base import Base
from app.models.model_registry import ModelRegistry
from app.services import model_catalog, model_registry


@compiles(JSONB, "sqlite")
def _render_jsonb_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


@compiles(UUID, "sqlite")
def _render_uuid_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(36)"


@pytest.fixture(autouse=True)
def _reset_registry():
    model_registry._rows = {}
    yield
    model_registry._rows = {}
    model_catalog._live_models = None


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = cast("list[Table]", [ModelRegistry.__table__])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_load_registry_roundtrip(db: AsyncSession):
    db.add(
        ModelRegistry(
            model_id="custom-x",
            provider="bedrock-open",
            display_name="Custom X",
            options={"max_tokens": 512},
            input_price=Decimal("2.0000"),
            output_price=Decimal("8.0000"),
            enabled=True,
            is_default=True,
            source="custom",
        )
    )
    await db.commit()

    await model_registry.load_registry(db)

    row = model_registry._rows["custom-x"]
    assert row["provider"] == "bedrock-open"
    assert row["display_name"] == "Custom X"
    assert row["options"] == {"max_tokens": 512}
    assert row["input_price"] == Decimal("2.0000")
    assert row["enabled"] is True
    assert row["is_default"] is True
    assert row["source"] == "custom"


@pytest.mark.asyncio
async def test_set_model_settings_persists_engine_availability(db: AsyncSession):
    # per-(engine, model) availability rides options["engines"]; a fresh load
    # proves it round-trips through the DB, not just the in-memory mirror.
    model_catalog._live_models = ["claude-opus-4-8", "claude-haiku-4-5"]
    await model_registry.set_model_settings(
        db,
        default="claude-opus-4-8",
        enabled=["claude-opus-4-8", "claude-haiku-4-5"],
        availability={"claude-opus-4-8": ["inkwest-agent"]},
    )
    await model_registry.load_registry(db)
    assert model_registry._rows["claude-opus-4-8"]["options"].get("engines") == ["inkwest-agent"]
    # A model absent from the availability map keeps no engines key (= all candidates).
    assert "engines" not in (model_registry._rows["claude-haiku-4-5"]["options"] or {})


@pytest.mark.asyncio
async def test_add_then_delete_custom_model_real_engine(db: AsyncSession):
    await model_registry.add_custom_model(
        db,
        model_id="meta.llama3",
        provider="bedrock-open",
        input_price=Decimal("1.0000"),
        output_price=Decimal("3.0000"),
        enabled=True,
    )
    # Persisted to the DB (fresh load proves it, not just the in-memory mirror).
    await model_registry.load_registry(db)
    assert "meta.llama3" in model_registry._rows
    assert model_registry._rows["meta.llama3"]["source"] == "custom"

    await model_registry.delete_model(db, "meta.llama3")
    await model_registry.load_registry(db)
    assert "meta.llama3" not in model_registry._rows


@pytest.mark.asyncio
async def test_delete_discovered_rejected_real_engine(db: AsyncSession):
    db.add(ModelRegistry(model_id="claude-opus-4-8", provider="bedrock", source="discovered"))
    await db.commit()
    await model_registry.load_registry(db)

    with pytest.raises(ValueError, match="cannot be deleted"):
        await model_registry.delete_model(db, "claude-opus-4-8")


def _capturing_db() -> tuple[AsyncMock, list[Any]]:
    executed: list[Any] = []
    db = AsyncMock()

    async def _exec(stmt, *a, **k):
        executed.append(stmt)
        return MagicMock()

    db.execute = AsyncMock(side_effect=_exec)
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db, executed


def _pg_sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_set_model_settings_statements_compile_for_postgres():
    """The bulk-save upsert, disable-update, and default-flag dance must render
    as valid Postgres (catches a malformed pg_insert / two-arg WHERE construct)."""
    model_catalog._live_models = ["claude-opus-4-8", "claude-haiku-4-5"]
    db, executed = _capturing_db()

    await model_registry.set_model_settings(
        db, default="claude-opus-4-8", enabled=["claude-opus-4-8", "claude-haiku-4-5"]
    )

    sqls = [_pg_sql(s) for s in executed]
    joined = "\n".join(sqls)
    assert any("ON CONFLICT" in s and "model_registry" in s for s in sqls)  # the upserts
    assert "is_default" in joined  # the clear/set default updates
    # The disable-others update uses a two-arg .where(enabled, NOT IN (...)).
    assert any("NOT IN" in s.upper() for s in sqls)


@pytest.mark.asyncio
async def test_update_model_statement_compiles_for_postgres():
    model_catalog._live_models = ["claude-opus-4-8"]
    db, executed = _capturing_db()

    await model_registry.update_model(db, "claude-opus-4-8", display_name="X", enabled=True)

    assert executed, "expected an upsert statement"
    sql = _pg_sql(executed[0])
    assert "INSERT INTO model_registry" in sql
    assert "ON CONFLICT" in sql
