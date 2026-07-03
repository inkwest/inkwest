"""Unit tests for MCP per-tool policy resolution + writes (app.services.mcp_policy)."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import mcp_policy


def _result(rows: list[tuple[str, str]]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _db(*scope_results: list[tuple[str, str]]) -> AsyncMock:
    """A db whose successive execute() calls return the given (tool, verdict) row sets."""
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_result(rows) for rows in scope_results])
    return db


@pytest.mark.asyncio
async def test_resolve_conv_tool_wins_over_everything() -> None:
    db = _db([("query", "deny"), ("*", "allow")])  # session scope has the specific tool
    v = await mcp_policy.resolve_verdict(
        db, user_id=uuid4(), session_id=uuid4(), server_key="k", tool="query"
    )
    assert v == "deny"
    db.execute.assert_awaited_once()  # short-circuits before touching the user scope


@pytest.mark.asyncio
async def test_resolve_conv_server_default_when_no_conv_tool() -> None:
    db = _db([("*", "allow")])  # session scope: only the server default
    v = await mcp_policy.resolve_verdict(
        db, user_id=uuid4(), session_id=uuid4(), server_key="k", tool="query"
    )
    assert v == "allow"


@pytest.mark.asyncio
async def test_resolve_falls_through_to_global_tool() -> None:
    db = _db([], [("query", "deny"), ("*", "allow")])  # session empty, user has the tool
    v = await mcp_policy.resolve_verdict(
        db, user_id=uuid4(), session_id=uuid4(), server_key="k", tool="query"
    )
    assert v == "deny"


@pytest.mark.asyncio
async def test_resolve_falls_through_to_global_server_default() -> None:
    db = _db([], [("*", "deny")])  # session empty, user has only the server default
    v = await mcp_policy.resolve_verdict(
        db, user_id=uuid4(), session_id=uuid4(), server_key="k", tool="query"
    )
    assert v == "deny"


@pytest.mark.asyncio
async def test_resolve_system_default_ask_when_nothing_matches() -> None:
    db = _db([], [])
    v = await mcp_policy.resolve_verdict(
        db, user_id=uuid4(), session_id=uuid4(), server_key="k", tool="query"
    )
    assert v == mcp_policy.DEFAULT_VERDICT == "ask"


@pytest.mark.asyncio
async def test_resolve_skips_session_scope_when_no_session() -> None:
    # No session_id → only the user scope is queried (exactly one execute).
    db = _db([("*", "deny")])
    v = await mcp_policy.resolve_verdict(
        db, user_id=uuid4(), session_id=None, server_key="k", tool="query"
    )
    assert v == "deny"
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_user_server_policy_replaces_then_commits() -> None:
    db = AsyncMock()
    db.execute = AsyncMock()
    db.add_all = MagicMock()
    db.commit = AsyncMock()
    await mcp_policy.set_user_server_policy(
        db, user_id=uuid4(), server_key="k", entries=[("query", "allow"), ("*", "ask")]
    )
    db.execute.assert_awaited_once()  # the delete of the server's existing rows
    db.add_all.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_rejects_invalid_verdict_before_touching_db() -> None:
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    with pytest.raises(mcp_policy.InvalidVerdictError):
        await mcp_policy.set_user_server_policy(
            db, user_id=uuid4(), server_key="k", entries=[("query", "maybe")]
        )
    db.execute.assert_not_awaited()  # validation happens before any write
    db.commit.assert_not_awaited()
