"""Behavioral tests for the message/session persistence helpers.

Like ``test_reconcile``, these run the real queries against an in-memory SQLite
database so we exercise the actual SQL — not mocks. The ``@compiles`` shims teach
SQLite to render the Postgres column types (UUID, JSONB) the models declare.

``asyncio_mode = "auto"`` (pyproject) auto-collects ``async def test_*``.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import app.models  # noqa: F401  -- registers all tables on Base.metadata
from app.models.base import Base
from app.models.message import Message
from app.models.session import Session
from app.services import messages as messages_mod
from app.services.messages import (
    clear_cli_session_id,
    ensure_session_title,
    get_cli_session_id,
    get_message_count,
    get_session_messages,
    get_session_stats,
    save_message,
    save_session_event,
    set_cli_session_id,
    truncate_messages,
    update_session_stats,
    update_session_summary,
    update_session_title,
)

# Re-registering these for the same (type, dialect) is idempotent — it replaces an
# identical handler — so it's harmless even when test_reconcile also registers them.


@compiles(JSONB, "sqlite")
def _render_jsonb_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


@compiles(UUID, "sqlite")
def _render_uuid_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "CHAR(36)"


_BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = cast("list[Table]", [Session.__table__, Message.__table__])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _make_session(db: AsyncSession, **fields: Any) -> uuid.UUID:
    session_id = uuid.uuid4()
    db.add(Session(id=session_id, **fields))
    await db.commit()
    return session_id


async def _add_message(
    db: AsyncSession, session_id: uuid.UUID, role: str, content: Any, minute: int = 0
) -> None:
    db.add(
        Message(
            id=uuid.uuid4(),
            session_id=session_id,
            role=role,
            content=content,
            created_at=_BASE_TIME + timedelta(minutes=minute),
        )
    )
    await db.commit()


# --- truncate_messages (pure) ---------------------------------------------------


def test_truncate_under_limit_returns_unchanged() -> None:
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert truncate_messages(msgs, 5) is msgs


def test_truncate_keeps_last_n() -> None:
    msgs = [{"role": "user", "content": str(i)} for i in range(10)]
    out = truncate_messages(msgs, 3)
    assert [m["content"] for m in out] == ["7", "8", "9"]


def test_truncate_drops_orphaned_leading_tool_result() -> None:
    # Cutting at a tool_result would orphan it from its tool_use → must be dropped.
    msgs = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1"}]},
        {"role": "tool", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        {"role": "assistant", "content": "answer"},
    ]
    out = truncate_messages(msgs, 2)
    # last 2 would be [tool_result, assistant]; the leading tool_result is stripped.
    assert out == [{"role": "assistant", "content": "answer"}]


# --- get_session_messages -------------------------------------------------------


async def test_get_messages_empty(db: AsyncSession) -> None:
    sid = await _make_session(db)
    assert await get_session_messages(db, sid) == []


async def test_get_messages_string_content(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await _add_message(db, sid, "user", "hello")
    out = await get_session_messages(db, sid)
    assert out == [{"role": "user", "content": "hello"}]


async def test_get_messages_list_content(db: AsyncSession) -> None:
    sid = await _make_session(db)
    blocks = [{"type": "text", "text": "hi"}]
    await _add_message(db, sid, "assistant", blocks)
    out = await get_session_messages(db, sid)
    assert out == [{"role": "assistant", "content": blocks}]


async def test_get_messages_dict_typed_block_is_wrapped(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await _add_message(db, sid, "assistant", {"type": "text", "text": "hi"})
    out = await get_session_messages(db, sid)
    assert out == [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]


async def test_get_messages_dict_untyped_falls_back_to_text(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await _add_message(db, sid, "assistant", {"text": "plain"})
    out = await get_session_messages(db, sid)
    assert out == [{"role": "assistant", "content": "plain"}]


async def test_get_messages_tool_result_role_mapped_to_user(db: AsyncSession) -> None:
    # tool_result rows are stored for the UI but must be sent to the API as role=user.
    sid = await _make_session(db)
    await _add_message(db, sid, "tool_result", [{"type": "tool_result", "content": "out"}])
    out = await get_session_messages(db, sid)
    assert out[0]["role"] == "user"


async def test_get_messages_ordered_by_created_at(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await _add_message(db, sid, "user", "second", minute=2)
    await _add_message(db, sid, "user", "first", minute=1)
    out = await get_session_messages(db, sid)
    assert [m["content"] for m in out] == ["first", "second"]


async def test_get_messages_accepts_str_session_id(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await _add_message(db, sid, "user", "hi")
    out = await get_session_messages(db, str(sid))
    assert out[0]["content"] == "hi"


async def test_get_messages_excludes_system_events(db: AsyncSession) -> None:
    # 'system' messages are UI-only event markers and must never reach the model's
    # payload (no 'system' role in the Claude messages array).
    sid = await _make_session(db)
    await _add_message(db, sid, "user", "before", minute=0)
    await _add_message(
        db, sid, "system", {"type": "session_event", "subject": "aws", "action": "granted"}, 1
    )
    await _add_message(db, sid, "assistant", "after", minute=2)
    out = await get_session_messages(db, sid)
    assert [m["content"] for m in out] == ["before", "after"]
    assert all(m["role"] != "system" for m in out)


# --- save_message ---------------------------------------------------------------


async def test_save_message_persists_and_returns(db: AsyncSession) -> None:
    sid = await _make_session(db)
    msg = await save_message(db, sid, "user", "investigate", input_tokens=10, output_tokens=20)
    assert msg.id is not None
    assert msg.role == "user"
    assert msg.input_tokens == 10
    # token_count is not passed → falls back to output_tokens.
    assert msg.token_count == 20
    assert await get_message_count(db, sid) == 1


async def test_save_session_event_persists_structured_system_message(db: AsyncSession) -> None:
    sid = await _make_session(db)
    targets = [{"account_id": "111", "account_name": "acme", "role": "ReadOnly"}]
    await save_session_event(db, sid, "aws", "granted", targets=targets)
    result = await db.execute(Message.__table__.select())
    row = result.mappings().one()
    assert row["role"] == "system"
    assert row["content"] == {
        "type": "session_event",
        "subject": "aws",
        "action": "granted",
        "targets": targets,
    }
    # Excluded from the model payload — it's a UI-only event.
    assert await get_session_messages(db, sid) == []


async def test_message_count_excludes_system_events(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await _add_message(db, sid, "user", "hi", minute=0)
    await _add_message(db, sid, "assistant", "hello", minute=1)
    await save_session_event(db, sid, "aws", "granted")  # 'system' — must not be counted
    assert await get_message_count(db, sid) == 2


async def test_save_session_event_omits_targets_when_absent(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await save_session_event(db, sid, "gitlab", "renewed")
    result = await db.execute(Message.__table__.select())
    row = result.mappings().one()
    assert row["content"] == {"type": "session_event", "subject": "gitlab", "action": "renewed"}


async def test_messages_role_check_rejects_unknown_role(db: AsyncSession) -> None:
    # Guards against ORM/migration drift: the role CHECK must be on the model so the
    # SQLite test schema enforces it (the 'system' role insert failed in prod against
    # a Postgres-only CHECK that the model lacked — this test would have caught it).
    import pytest
    from sqlalchemy.exc import IntegrityError

    sid = await _make_session(db)
    db.add(Message(id=uuid.uuid4(), session_id=sid, role="bogus", content="x"))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_set_session_mcp_emits_batched_events_against_real_schema(db: AsyncSession) -> None:
    """Integration: the MCP route writes batched session events end-to-end against the
    REAL schema (incl. the role CHECK). Route tests elsewhere mock the DB, so the
    save→commit→constraint path is otherwise untested — exactly the gap the original
    'system'-role production bug slipped through."""
    from app.api.routes.sessions import SessionMcpUpdate, set_session_mcp

    sid = await _make_session(db, enabled_mcp_servers=["coralogix"])
    session_obj = await db.get(Session, sid)

    with (
        # Ownership lookup pulls a User relationship not in this minimal schema; stub it.
        # The point under test is the event save → real INSERT against the role CHECK.
        patch("app.api.routes.sessions._load_owned_session", AsyncMock(return_value=session_obj)),
        patch("app.services.mcp_catalog.catalog_keys", return_value=["coralogix", "sentry"]),
        patch("app.services.mcp_config.apply_session_mcp_selection"),
    ):
        # coralogix -> sentry: one "connected" (sentry) + one "disconnected" (coralogix).
        await set_session_mcp(sid, SessionMcpUpdate(enabled=["sentry"]), MagicMock(), db)

    rows = (
        (
            await db.execute(
                Message.__table__.select()
                .where(Message.role == "system")
                .order_by(Message.created_at)
            )
        )
        .mappings()
        .all()
    )
    events = {(r["content"]["action"], r["content"]["targets"][0]["server"]) for r in rows}
    assert events == {("connected", "sentry"), ("disconnected", "coralogix")}
    # And they are excluded from the model payload.
    assert await get_session_messages(db, sid) == []


async def test_save_message_explicit_token_count_wins(db: AsyncSession) -> None:
    sid = await _make_session(db)
    msg = await save_message(db, sid, "assistant", "done", token_count=7, output_tokens=20)
    assert msg.token_count == 7


async def test_save_message_explicit_zero_token_count_preserved(db: AsyncSession) -> None:
    # An explicit 0 must not fall back to output_tokens (the falsy-`or` trap).
    sid = await _make_session(db)
    msg = await save_message(db, sid, "assistant", "done", token_count=0, output_tokens=20)
    assert msg.token_count == 0


# --- update_session_title / ensure_session_title --------------------------------


async def test_update_session_title_sets_and_notifies(db: AsyncSession) -> None:
    sid = await _make_session(db)
    # notify() opens a real pubsub engine connection — patch it out.
    with patch.object(messages_mod, "notify", new=AsyncMock()) as mock_notify:
        await update_session_title(db, sid, "My Title")
    session = await db.get(Session, sid)
    assert session is not None and session.title == "My Title"
    mock_notify.assert_awaited_once()


async def test_ensure_title_missing_session_returns_none(db: AsyncSession) -> None:
    assert await ensure_session_title(db, uuid.uuid4(), "anything") is None


async def test_ensure_title_skips_when_already_set(db: AsyncSession) -> None:
    sid = await _make_session(db, title="Existing")
    with patch.object(messages_mod, "notify", new=AsyncMock()):
        assert await ensure_session_title(db, sid, "new message") is None
    session = await db.get(Session, sid)
    assert session is not None and session.title == "Existing"


async def test_ensure_title_short_message_used_verbatim(db: AsyncSession) -> None:
    sid = await _make_session(db)
    with patch.object(messages_mod, "notify", new=AsyncMock()):
        title = await ensure_session_title(db, sid, "  short question  ")
    assert title == "short question"


async def test_ensure_title_long_message_breaks_on_word_boundary(db: AsyncSession) -> None:
    sid = await _make_session(db)
    msg = "investigate the failing pod in the production namespace right away please"
    with patch.object(messages_mod, "notify", new=AsyncMock()):
        title = await ensure_session_title(db, sid, msg)
    assert title is not None
    assert title.endswith("...")
    assert " " not in title[-4:]  # cut at a space, not mid-word
    assert len(title) <= 63  # 60 + "..."


async def test_ensure_title_long_message_no_boundary_hard_truncates(db: AsyncSession) -> None:
    sid = await _make_session(db)
    # A space exists but only early (index 2), so no word boundary past char 40 → hard cut.
    msg = "hi " + "y" * 64
    with patch.object(messages_mod, "notify", new=AsyncMock()):
        title = await ensure_session_title(db, sid, msg)
    assert title == msg[:60] + "..."


# --- get_message_count ----------------------------------------------------------


async def test_get_message_count(db: AsyncSession) -> None:
    sid = await _make_session(db)
    for i in range(3):
        await _add_message(db, sid, "user", f"m{i}", minute=i)
    assert await get_message_count(db, sid) == 3


# --- update_session_summary -----------------------------------------------------


async def test_update_session_summary(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await update_session_summary(db, sid, "a concise summary")
    session = await db.get(Session, sid)
    assert session is not None and session.summary == "a concise summary"


# --- update_session_stats / get_session_stats -----------------------------------


async def test_update_stats_increments_atomically(db: AsyncSession) -> None:
    sid = await _make_session(db)
    await update_session_stats(
        db, sid, input_tokens=10, output_tokens=5, tool_calls=1, iterations=1
    )
    await update_session_stats(db, sid, input_tokens=3, output_tokens=2, tool_calls=2, iterations=1)
    stats = await get_session_stats(db, sid)
    assert stats == {
        "total_input_tokens": 13,
        "total_output_tokens": 7,
        "total_tool_calls": 3,
        "total_iterations": 2,
    }


async def test_get_stats_missing_session_returns_zeros(db: AsyncSession) -> None:
    stats = await get_session_stats(db, uuid.uuid4())
    assert stats == {
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tool_calls": 0,
        "total_iterations": 0,
    }


# --- CLI session id roundtrip ---------------------------------------------------


async def test_cli_session_id_set_get_clear(db: AsyncSession) -> None:
    sid = await _make_session(db)
    assert await get_cli_session_id(db, sid) is None

    await set_cli_session_id(db, sid, "cli-abc-123")
    assert await get_cli_session_id(db, sid) == "cli-abc-123"

    # A stale --resume id is cleared so the next turn starts fresh.
    await clear_cli_session_id(db, sid)
    assert await get_cli_session_id(db, sid) is None


async def test_get_stats_returns_persisted_totals(db: AsyncSession) -> None:
    sid = await _make_session(
        db, total_input_tokens=100, total_output_tokens=40, total_tool_calls=5, total_iterations=3
    )
    stats = await get_session_stats(db, sid)
    assert stats["total_input_tokens"] == 100
    assert stats["total_tool_calls"] == 5
