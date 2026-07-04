"""Unit tests for the shared end-of-turn usage persistence (app.services.turn_usage).

The orchestrator suites patch persist_turn_usage itself, so its internal contracts —
zero-gate incl. cache, broker skip, retry-once with rollback, the ambiguous-commit
guard, the stats→row→commit→stamp ORDER, stamp isolation — are pinned here and
nowhere else.
"""

import contextlib
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

import app.services.turn_usage as tu

_SESSION = uuid.uuid4()
_USER = uuid.uuid4()
_TURN = uuid.uuid4()


def _db(scalar_result=None):
    db = AsyncMock()
    db.scalar.return_value = scalar_result
    return db


@contextlib.contextmanager
def _patched(db):
    """Patch the three persisted-through functions and zero the retry delay.

    Yields (stats, record, stamp, manager): the mocks plus a parent manager that also
    tracks `db`, so tests can assert the ORDER of stats → record → db.commit → stamp —
    the sequence the module's contracts (budget row inside the stats txn, stamp strictly
    after the budget commit) depend on.
    """
    stats, record, stamp = AsyncMock(), AsyncMock(), AsyncMock()
    manager = Mock()
    manager.attach_mock(stats, "stats")
    manager.attach_mock(record, "record")
    manager.attach_mock(stamp, "stamp")
    manager.attach_mock(db, "db")
    with (
        patch.object(tu, "update_session_stats", stats),
        patch.object(tu, "record_usage", record),
        patch.object(tu, "stamp_message_usage", stamp),
        patch.object(tu, "_RETRY_DELAY_SECONDS", 0),
    ):
        yield stats, record, stamp, manager


def _call_order(manager: Mock) -> list[str]:
    tracked = {"stats", "record", "stamp", "db.commit", "db.rollback", "db.scalar"}
    return [name for (name, _args, _kwargs) in manager.mock_calls if name in tracked]


async def _persist(db, **overrides):
    kwargs = {
        "session_id": str(_SESSION),
        "session_uuid": _SESSION,
        "user_id": _USER,
        "model": "anthropic.claude-opus-4-8",
        "turn_id": _TURN,
        "input_tokens": 10,
        "output_tokens": 5,
    }
    kwargs.update(overrides)
    await tu.persist_turn_usage(db, **kwargs)


@pytest.mark.asyncio
async def test_happy_path_order_stats_row_commit() -> None:
    db = _db()
    with _patched(db) as (stats, record, stamp, manager):
        await _persist(db)
    stats.assert_awaited_once()
    record.assert_awaited_once()
    stamp.assert_not_awaited()
    # The contract, not just the participants: budget row joins the stats txn, then commit.
    assert _call_order(manager) == ["stats", "record", "db.commit"]
    # commit=False: the stats increment must NOT self-commit — it shares the budget
    # row's transaction so the retry guard / unique-index backstop covers both.
    assert stats.await_args.kwargs["commit"] is False


@pytest.mark.asyncio
async def test_all_zero_gate_skips_everything() -> None:
    db = _db()
    with _patched(db) as (stats, record, _stamp, _manager):
        await _persist(db, input_tokens=0, output_tokens=0)
    stats.assert_not_awaited()
    record.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_only_turn_still_persists() -> None:
    """A fully-cached turn reports 0 fresh in/out yet burned billable tokens."""
    db = _db()
    with _patched(db) as (stats, record, _stamp, _manager):
        await _persist(db, input_tokens=0, output_tokens=0, cache_read_tokens=8_000)
    stats.assert_awaited_once()
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_budget_row_keeps_stats_and_stamp() -> None:
    """Broker-on: the engine row would double-count the budget, but session stats and
    the Message stamp stay engine-fed (they feed the UI, not the budget)."""
    db = _db()
    with _patched(db) as (stats, record, stamp, manager):
        await _persist(db, skip_budget_row=True, stamp_message_id=uuid.uuid4())
    stats.assert_awaited_once()
    record.assert_not_awaited()
    stamp.assert_awaited_once()
    # Stamp strictly AFTER the budget commit (its failure must not cost the usage row).
    assert _call_order(manager) == ["stats", "db.commit", "stamp"]


@pytest.mark.asyncio
async def test_transient_commit_failure_retries_once_with_rollback() -> None:
    """The blip class this exists for: first commit fails, rollback restores the
    session, the second attempt re-applies and commits (no row landed the first time)."""
    db = _db(scalar_result=None)  # ambiguity probe finds no row
    db.commit.side_effect = [RuntimeError("connection reset"), None]
    with _patched(db) as (stats, record, _stamp, _manager):
        await _persist(db)
    assert stats.await_count == 2
    assert record.await_count == 2
    assert db.commit.await_count == 2
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_commit_is_not_reapplied() -> None:
    """Commit succeeded server-side but the ack was lost: the retry finds the turn's
    budget row already landed and must NOT double the stats or insert a second row."""
    db = _db(scalar_result=uuid.uuid4())  # ambiguity probe FINDS the row
    db.commit.side_effect = [RuntimeError("connection reset"), None]
    with _patched(db) as (stats, record, _stamp, _manager):
        await _persist(db)
    stats.assert_awaited_once()
    record.assert_awaited_once()
    db.commit.assert_awaited_once()  # only the failed one — the retry broke out


@pytest.mark.asyncio
async def test_probe_error_falls_through_to_normal_retry() -> None:
    """A raising probe (DB still down) must degrade to the normal retry, never mask it
    or escape: `_engine_row_exists` swallows, rolls back, returns False."""
    db = _db()
    db.scalar.side_effect = RuntimeError("connection still down")
    db.commit.side_effect = [RuntimeError("connection reset"), None]
    with _patched(db) as (stats, record, _stamp, _manager):
        await _persist(db)  # must not raise
    # Probe errored → False → attempt 2 re-applied and committed.
    assert stats.await_count == 2
    assert record.await_count == 2
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_engine_row_exists_error_returns_false() -> None:
    db = _db()
    db.scalar.side_effect = RuntimeError("boom")
    assert await tu._engine_row_exists(db, _TURN) is False
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_skip_budget_row_retry_skips_probe_and_reapplies() -> None:
    """Broker-on retry has no engine-row marker: the probe is skipped and the retry
    re-applies — the documented, accepted stats-only double-apply (never the budget)."""
    db = _db()
    db.commit.side_effect = [RuntimeError("connection reset"), None]
    with _patched(db) as (stats, record, _stamp, _manager):
        await _persist(db, skip_budget_row=True)
    db.scalar.assert_not_awaited()  # no probe on the broker-on path
    assert stats.await_count == 2
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_double_failure_never_raises() -> None:
    """Persistence is best-effort: it must not mask the turn's own outcome."""
    db = _db(scalar_result=None)
    db.commit.side_effect = RuntimeError("db down")
    with _patched(db):
        await _persist(db)  # must not raise
    assert db.commit.await_count == 2
    assert db.rollback.await_count == 2


@pytest.mark.asyncio
async def test_stamp_failure_never_costs_the_budget_row() -> None:
    db = _db()
    with _patched(db) as (_stats, record, stamp, manager):
        stamp.side_effect = RuntimeError("stamp boom")
        await _persist(db, stamp_message_id=uuid.uuid4())  # must not raise
    record.assert_awaited_once()
    db.rollback.assert_awaited_once()  # stamp hygiene rollback
    # The budget commit landed BEFORE the stamp attempt.
    assert _call_order(manager) == ["stats", "record", "db.commit", "stamp", "db.rollback"]
