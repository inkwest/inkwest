"""Tests for the in-process pending-ask registry (app.services.mcp_ask_store)."""

import asyncio
from collections.abc import Iterator

import pytest

from app.services import mcp_ask_store

_UID = "00000000-0000-4000-8000-000000000001"
_SID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _clean_store() -> Iterator[None]:
    mcp_ask_store._asks.clear()
    yield
    mcp_ask_store._asks.clear()


def _create(**overrides: str) -> mcp_ask_store.PendingAsk:
    kwargs = {"user_id": _UID, "session_id": _SID, "server_key": "notion", "tool": "search"}
    kwargs.update(overrides)
    return mcp_ask_store.create(**kwargs)


@pytest.mark.asyncio
async def test_create_and_resolve_allow() -> None:
    ask = _create()
    assert mcp_ask_store.resolve(ask.ask_id, "allow", user_id=_UID) is True
    assert await ask.future == "allow"


@pytest.mark.asyncio
async def test_resolve_unknown_or_wrong_user_is_false() -> None:
    ask = _create()
    assert mcp_ask_store.resolve("nope", "allow", user_id=_UID) is False
    assert mcp_ask_store.resolve(ask.ask_id, "allow", user_id="someone-else") is False
    assert not ask.future.done()  # the wrong-user attempt must not touch the future


@pytest.mark.asyncio
async def test_double_resolve_is_false() -> None:
    ask = _create()
    assert mcp_ask_store.resolve(ask.ask_id, "deny", user_id=_UID) is True
    assert mcp_ask_store.resolve(ask.ask_id, "allow", user_id=_UID) is False
    assert await ask.future == "deny"  # first decision wins


@pytest.mark.asyncio
async def test_resolve_after_waiter_timeout_is_false() -> None:
    """wait_for cancels the future on timeout; a late decision reports unresolvable."""
    ask = _create()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ask.future, 0.01)
    assert mcp_ask_store.resolve(ask.ask_id, "allow", user_id=_UID) is False


@pytest.mark.asyncio
async def test_discard_is_idempotent() -> None:
    ask = _create()
    mcp_ask_store.discard(ask.ask_id)
    mcp_ask_store.discard(ask.ask_id)
    assert ask.ask_id not in mcp_ask_store._asks


@pytest.mark.asyncio
async def test_pending_for_filters_user_session_and_done() -> None:
    live = _create()
    _create(user_id="other-user")
    _create(session_id="other-session")
    decided = _create(tool="write")
    mcp_ask_store.resolve(decided.ask_id, "allow", user_id=_UID)

    pending = mcp_ask_store.pending_for(user_id=_UID, session_id=_SID)
    assert [a.ask_id for a in pending] == [live.ask_id]


@pytest.mark.asyncio
async def test_pending_for_excludes_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    _create()
    monkeypatch.setattr(mcp_ask_store, "ASK_TIMEOUT_SECONDS", -1)
    assert mcp_ask_store.pending_for(user_id=_UID, session_id=_SID) == []


@pytest.mark.asyncio
async def test_cap_evicts_oldest_as_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_ask_store, "_MAX_PENDING", 2)
    first = _create(tool="a")
    second = _create(tool="b")
    third = _create(tool="c")
    # The oldest waiter was evicted and resolved to deny (fail-closed), the rest survive.
    assert await first.future == "deny"
    assert first.ask_id not in mcp_ask_store._asks
    assert not second.future.done()
    assert third.ask_id in mcp_ask_store._asks


@pytest.mark.asyncio
async def test_create_sweeps_done_entries() -> None:
    stale = _create()
    mcp_ask_store.resolve(stale.ask_id, "deny", user_id=_UID)
    fresh = _create(tool="other")
    assert stale.ask_id not in mcp_ask_store._asks
    assert fresh.ask_id in mcp_ask_store._asks
