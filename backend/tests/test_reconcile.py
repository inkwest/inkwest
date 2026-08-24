"""Behavioral tests for startup reconciliation of interrupted turns.

These run the real query against an in-memory SQLite database. The models use
Postgres column types (UUID, JSONB); the ``@compiles`` shims below let those
render on SQLite so we exercise the actual SQL — not just assert its text.

``asyncio_mode = "auto"`` (pyproject) auto-collects ``async def test_*``; no
per-test marker is needed.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import app.main as app_main
import app.models  # noqa: F401  -- registers all tables on Base.metadata
from app.models.base import Base
from app.models.message import Message
from app.models.session import Session
from app.services.messages import (
    INTERRUPTED_TURN_ERROR,
    clear_retry_state,
    get_retryable_message,
    purge_empty_sessions,
    reconcile_interrupted_turns,
)

# Process-global registration (a @compiles side effect of importing this module).
# Harmless: only this test module spins up a real SQLite engine, and these merely
# teach the SQLite dialect to render the Postgres column types as their generic
# equivalents so create_all/round-trips work.


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


async def _make_session(db: AsyncSession, *turns: tuple[Any, ...]) -> uuid.UUID:
    """Create a session whose messages are ``turns`` in order, oldest first.

    Each turn is ``(role, content)`` or ``(role, content, status)``;
    ``created_at`` increments per message so the last tuple is the newest.
    """
    session_id = uuid.uuid4()
    db.add(Session(id=session_id))
    for i, turn in enumerate(turns):
        role, content = turn[0], turn[1]
        status = turn[2] if len(turn) > 2 else "success"
        db.add(
            Message(
                id=uuid.uuid4(),
                session_id=session_id,
                role=role,
                content=content,
                status=status,
                created_at=_BASE_TIME + timedelta(minutes=i),
            )
        )
    await db.commit()
    return session_id


async def _newest(db: AsyncSession, session_id: uuid.UUID) -> tuple[Any, ...]:
    """(status, is_retryable, retry_count, error_message) of the newest message."""
    db.expire_all()
    result = await db.execute(
        select(Message.status, Message.is_retryable, Message.retry_count, Message.error_message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return tuple(result.one())


async def test_flags_trailing_user_message(db: AsyncSession) -> None:
    sid = await _make_session(db, ("user", "analyze the cluster"))

    assert await reconcile_interrupted_turns(db) == 1

    status, retryable, retry_count, err = await _newest(db, sid)
    assert status == "error"
    assert retryable is True
    assert retry_count == 0  # preserved, not bumped
    assert err == INTERRUPTED_TURN_ERROR


async def test_ignores_completed_turn(db: AsyncSession) -> None:
    # user -> assistant: the turn finished; newest row is the assistant reply.
    sid = await _make_session(
        db, ("user", "hi"), ("assistant", [{"type": "text", "text": "hello"}])
    )

    assert await reconcile_interrupted_turns(db) == 0

    rows = (
        (await db.execute(select(Message.status).where(Message.session_id == sid))).scalars().all()
    )
    assert all(status == "success" for status in rows)


async def test_empty_session_is_noop(db: AsyncSession) -> None:
    await _make_session(db)  # session with no messages

    assert await reconcile_interrupted_turns(db) == 0


async def test_idempotent(db: AsyncSession) -> None:
    await _make_session(db, ("user", "once more"))

    assert await reconcile_interrupted_turns(db) == 1
    assert await reconcile_interrupted_turns(db) == 0  # already error → skipped


async def test_reflags_interrupted_retry_without_resetting_count(db: AsyncSession) -> None:
    # A turn that was retried (status pending, retry_count bumped) then interrupted
    # again must be re-flagged as error, keeping the accumulated retry_count.
    sid = await _make_session(db, ("user", "again"))
    await db.execute(
        update(Message)
        .where(Message.session_id == sid)
        .values(status="pending", is_retryable=True, retry_count=2)
    )
    await db.commit()

    assert await reconcile_interrupted_turns(db) == 1

    status, retryable, retry_count, _ = await _newest(db, sid)
    assert status == "error"
    assert retryable is True
    assert retry_count == 2  # preserved


async def test_only_interrupted_session_is_flagged(db: AsyncSession) -> None:
    interrupted = await _make_session(db, ("user", "interrupted question"))
    completed = await _make_session(
        db, ("user", "answered"), ("assistant", [{"type": "text", "text": "done"}])
    )

    assert await reconcile_interrupted_turns(db) == 1

    assert (await _newest(db, interrupted))[0] == "error"
    assert (await _newest(db, completed))[0] == "success"


async def test_only_latest_user_message_is_flagged(db: AsyncSession) -> None:
    # A continued conversation interrupted on a later turn: only the newest user
    # message is flagged; an earlier "talked-past" user turn stays untouched.
    sid = await _make_session(
        db,
        ("user", "first question"),
        ("assistant", [{"type": "text", "text": "first answer"}]),
        ("user", "second question"),
    )

    assert await reconcile_interrupted_turns(db) == 1

    users = (
        await db.execute(
            select(Message.content, Message.status)
            .where(Message.session_id == sid, Message.role == "user")
            .order_by(Message.created_at)
        )
    ).all()
    first, second = users
    assert first.content == "first question" and first.status == "success"
    assert second.content == "second question" and second.status == "error"


async def test_trailing_abort_marker_is_not_flagged(db: AsyncSession) -> None:
    # An in-process errored turn ends on the 'turn aborted' system row turn_runner
    # persists (the abort marker), so a later unrelated restart must not re-flag its
    # user message as interrupted (see the reconcile docstring's false-positives note).
    sid = await _make_session(
        db,
        ("user", "investigate"),
        (
            "system",
            {
                "type": "session_event",
                "subject": "turn",
                "action": "aborted",
                "targets": [{"reason": "agent made no progress for 900s"}],
            },
        ),
    )

    assert await reconcile_interrupted_turns(db) == 0

    db.expire_all()
    user_status = (
        await db.execute(
            select(Message.status).where(Message.session_id == sid, Message.role == "user")
        )
    ).scalar_one()
    assert user_status == "success"


async def test_trailing_tool_result_is_not_flagged(db: AsyncSession) -> None:
    # Documents the known limitation: a multi-iteration turn interrupted after a
    # tool ran ends on a tool_result row, which this sweep does NOT detect.
    sid = await _make_session(
        db,
        ("user", "investigate"),
        ("assistant", [{"type": "tool_use", "name": "kubectl_get"}]),
        ("tool_result", [{"type": "tool_result", "content": "..."}]),
    )

    assert await reconcile_interrupted_turns(db) == 0

    rows = (
        (await db.execute(select(Message.status).where(Message.session_id == sid))).scalars().all()
    )
    assert all(status == "success" for status in rows)


# --- retry helpers: get_retryable_message (read-only peek) + clear_retry_state. ---


async def test_get_retryable_message_is_read_only(db: AsyncSession) -> None:
    # The peek must NOT mutate: the flag is cleared only after the turn starts, so
    # a turn that never starts leaves the message retryable (regression guard).
    sid = await _make_session(db, ("user", "analyze the cluster"))
    assert await reconcile_interrupted_turns(db) == 1  # flag it as interrupted

    assert await get_retryable_message(db, sid) == "analyze the cluster"

    status, retryable, retry_count, err = await _newest(db, sid)
    assert status == "error"  # untouched by the read
    assert retryable is True
    assert retry_count == 0
    assert err == INTERRUPTED_TURN_ERROR


async def test_clear_retry_state_clears_flag_and_bumps_count(db: AsyncSession) -> None:
    sid = await _make_session(db, ("user", "analyze the cluster"))
    assert await reconcile_interrupted_turns(db) == 1

    await clear_retry_state(db, sid)

    status, retryable, retry_count, err = await _newest(db, sid)
    assert status == "success"
    assert retryable is False
    assert retry_count == 1  # bumped
    assert err is None


async def test_get_retryable_message_none_when_nothing_retryable(db: AsyncSession) -> None:
    # A completed turn has no retryable row.
    sid = await _make_session(
        db, ("user", "hi"), ("assistant", [{"type": "text", "text": "hello"}])
    )
    assert await get_retryable_message(db, sid) is None
    await clear_retry_state(db, sid)  # no-op, must not raise


async def test_get_retryable_message_skips_non_string_content(db: AsyncSession) -> None:
    # A structured (non-string) user payload would replay as a Python repr, so the
    # peek refuses it rather than feeding garbage to the model.
    sid = await _make_session(db, ("user", [{"type": "text", "text": "structured"}]))
    await db.execute(
        update(Message)
        .where(Message.session_id == sid)
        .values(status="error", is_retryable=True, error_message=INTERRUPTED_TURN_ERROR)
    )
    await db.commit()

    assert await get_retryable_message(db, sid) is None


async def test_retry_helpers_target_newest_retryable(db: AsyncSession) -> None:
    # Two stale retryable user rows (the known multi-row limitation): both helpers
    # act on the newest one only and leave the older one untouched.
    sid = await _make_session(db, ("user", "older"), ("user", "newer"))
    await db.execute(
        update(Message)
        .where(Message.session_id == sid)
        .values(status="error", is_retryable=True, error_message=INTERRUPTED_TURN_ERROR)
    )
    await db.commit()

    assert await get_retryable_message(db, sid) == "newer"
    await clear_retry_state(db, sid)

    rows = (
        await db.execute(
            select(Message.content, Message.status, Message.is_retryable)
            .where(Message.session_id == sid)
            .order_by(Message.created_at)
        )
    ).all()
    older, newer = rows
    assert older.content == "older" and older.status == "error" and older.is_retryable is True
    assert newer.content == "newer" and newer.status == "success" and newer.is_retryable is False


# --- Lifespan integration: the startup reconcile is best-effort and must not
#     be able to abort boot (the load-bearing resilience guard in main.py). ---


class _DummySession:
    async def __aenter__(self) -> AsyncMock:
        return AsyncMock()

    async def __aexit__(self, *exc: object) -> bool:
        return False


async def test_lifespan_survives_reconcile_failure(monkeypatch: Any) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(app_main, "logger", fake_logger)
    monkeypatch.setattr(
        app_main,
        "settings",
        SimpleNamespace(
            oidc_enabled=False,
            connectors_enabled=False,
            sandbox_enabled=False,
            llm_broker_enabled=False,
            metrics_enabled=False,
            version="test",
            commit_hash="test",
            build_timestamp="test",
        ),
    )
    monkeypatch.setattr(app_main, "init_db", AsyncMock())
    monkeypatch.setattr(app_main, "async_session_maker", lambda: _DummySession())
    monkeypatch.setattr(app_main, "ensure_default_system_prompt", AsyncMock())
    monkeypatch.setattr(app_main, "ensure_skills_frontmatter", AsyncMock())
    monkeypatch.setattr(app_main, "load_settings", AsyncMock())
    monkeypatch.setattr(app_main, "load_registry", AsyncMock())
    monkeypatch.setattr(app_main, "load_oauth_catalog", AsyncMock())
    monkeypatch.setattr(app_main, "refresh_catalog", AsyncMock())
    monkeypatch.setattr(app_main, "validate_settings", lambda: None)
    reconcile = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(app_main, "reconcile_interrupted_turns", reconcile)
    monkeypatch.setattr(app_main, "purge_empty_sessions", AsyncMock(return_value=0))
    monkeypatch.setattr(app_main, "start_listener", AsyncMock())
    monkeypatch.setattr(app_main, "stop_listener", AsyncMock())
    monkeypatch.setattr(app_main, "close_db", AsyncMock())
    monkeypatch.setattr(app_main, "shutdown_telemetry", MagicMock())

    # Entering (startup) and exiting (shutdown) must not propagate the reconcile error.
    async with app_main.lifespan(app_main.app):
        pass

    reconcile.assert_awaited_once()
    fake_logger.exception.assert_called_once_with("reconcile_interrupted_turns_failed")


async def test_lifespan_survives_purge_failure(monkeypatch: Any) -> None:
    fake_logger = MagicMock()
    monkeypatch.setattr(app_main, "logger", fake_logger)
    monkeypatch.setattr(
        app_main,
        "settings",
        SimpleNamespace(
            oidc_enabled=False,
            connectors_enabled=False,
            sandbox_enabled=False,
            llm_broker_enabled=False,
            metrics_enabled=False,
            version="test",
            commit_hash="test",
            build_timestamp="test",
        ),
    )
    monkeypatch.setattr(app_main, "init_db", AsyncMock())
    monkeypatch.setattr(app_main, "async_session_maker", lambda: _DummySession())
    monkeypatch.setattr(app_main, "ensure_default_system_prompt", AsyncMock())
    monkeypatch.setattr(app_main, "ensure_skills_frontmatter", AsyncMock())
    monkeypatch.setattr(app_main, "load_settings", AsyncMock())
    monkeypatch.setattr(app_main, "load_registry", AsyncMock())
    monkeypatch.setattr(app_main, "load_oauth_catalog", AsyncMock())
    monkeypatch.setattr(app_main, "refresh_catalog", AsyncMock())
    monkeypatch.setattr(app_main, "validate_settings", lambda: None)
    monkeypatch.setattr(app_main, "reconcile_interrupted_turns", AsyncMock(return_value=0))
    purge = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(app_main, "purge_empty_sessions", purge)
    monkeypatch.setattr(app_main, "start_listener", AsyncMock())
    monkeypatch.setattr(app_main, "stop_listener", AsyncMock())
    monkeypatch.setattr(app_main, "close_db", AsyncMock())
    monkeypatch.setattr(app_main, "shutdown_telemetry", MagicMock())

    async with app_main.lifespan(app_main.app):
        pass

    purge.assert_awaited_once()
    fake_logger.exception.assert_called_once_with("purge_empty_sessions_failed")


def _broker_lifespan_settings(*, llm_broker_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        oidc_enabled=False,
        connectors_enabled=False,
        sandbox_enabled=False,
        llm_broker_enabled=llm_broker_enabled,
        metrics_enabled=False,
        version="test",
        commit_hash="test",
        build_timestamp="test",
    )


def _patch_lifespan_startup(monkeypatch: Any) -> None:
    monkeypatch.setattr(app_main, "validate_settings", lambda: None)
    monkeypatch.setattr(app_main, "init_db", AsyncMock())
    monkeypatch.setattr(app_main, "async_session_maker", lambda: _DummySession())
    monkeypatch.setattr(app_main, "ensure_default_system_prompt", AsyncMock())
    monkeypatch.setattr(app_main, "ensure_skills_frontmatter", AsyncMock())
    monkeypatch.setattr(app_main, "load_settings", AsyncMock())
    monkeypatch.setattr(app_main, "load_registry", AsyncMock())
    monkeypatch.setattr(app_main, "load_oauth_catalog", AsyncMock())
    monkeypatch.setattr(app_main, "refresh_catalog", AsyncMock())
    monkeypatch.setattr(app_main, "reconcile_interrupted_turns", AsyncMock(return_value=0))
    monkeypatch.setattr(app_main, "purge_empty_sessions", AsyncMock(return_value=0))
    monkeypatch.setattr(app_main, "oauth_catalog_keys", lambda: [])
    monkeypatch.setattr(app_main, "start_listener", AsyncMock())
    monkeypatch.setattr(app_main, "stop_listener", AsyncMock())
    monkeypatch.setattr(app_main, "close_db", AsyncMock())
    monkeypatch.setattr(app_main, "shutdown_telemetry", MagicMock())


async def test_lifespan_fails_fast_when_broker_enabled_multi_worker(monkeypatch: Any) -> None:
    """The in-process grant store assumes single-worker; enabling the broker under
    WEB_CONCURRENCY>1 must refuse to start rather than 401 every model call at runtime."""
    monkeypatch.setattr(app_main, "logger", MagicMock())
    monkeypatch.setattr(app_main, "settings", _broker_lifespan_settings(llm_broker_enabled=True))
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    _patch_lifespan_startup(monkeypatch)

    with pytest.raises(RuntimeError, match="single worker"):
        async with app_main.lifespan(app_main.app):
            pass


async def test_lifespan_broker_enabled_single_worker_starts(monkeypatch: Any) -> None:
    """Broker enabled with the default single worker boots cleanly (guard must not false-fire)."""
    monkeypatch.setattr(app_main, "logger", MagicMock())
    monkeypatch.setattr(app_main, "settings", _broker_lifespan_settings(llm_broker_enabled=True))
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    _patch_lifespan_startup(monkeypatch)

    async with app_main.lifespan(app_main.app):
        pass


async def _session_ids(db: AsyncSession) -> set[uuid.UUID]:
    db.expire_all()
    return set((await db.execute(select(Session.id))).scalars().all())


def _naive_utc_now() -> datetime:
    """Match the column semantics: created_at is naive (timezone-less) UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


async def test_purge_deletes_stale_empty_session(db: AsyncSession) -> None:
    stale = uuid.uuid4()
    db.add(Session(id=stale, created_at=_naive_utc_now() - timedelta(days=2)))
    await db.commit()

    assert await purge_empty_sessions(db) == 1
    assert await _session_ids(db) == set()


async def test_purge_keeps_recent_empty_session(db: AsyncSession) -> None:
    recent = uuid.uuid4()
    db.add(Session(id=recent, created_at=_naive_utc_now() - timedelta(hours=1)))
    await db.commit()

    assert await purge_empty_sessions(db) == 0
    assert await _session_ids(db) == {recent}


async def test_purge_keeps_stale_session_with_messages(db: AsyncSession) -> None:
    old = _naive_utc_now() - timedelta(days=2)
    sid = uuid.uuid4()
    db.add(Session(id=sid, created_at=old))
    db.add(
        Message(
            id=uuid.uuid4(),
            session_id=sid,
            role="user",
            content="hi",
            status="success",
            created_at=old,
        )
    )
    await db.commit()

    assert await purge_empty_sessions(db) == 0
    assert await _session_ids(db) == {sid}
