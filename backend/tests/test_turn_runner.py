"""Tests for the detached turn runner (orchestration decoupled from the socket)."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from app.services.events import done_event, text_event, tool_start_event
from app.services.turn_runner import TURN_END, TurnRunner


@asynccontextmanager
async def _fake_session():
    # The runner opens its own DB session; tests don't need a real one.
    yield object()


async def _drain(snapshot, queue):
    """Collect a subscriber's full event stream: replayed buffer + live, until END."""
    events = list(snapshot)
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=2)
        if item is TURN_END:
            return events
        events.append(item)


@pytest.fixture
def runner():
    with patch("app.services.turn_runner.async_session_maker", _fake_session):
        yield TurnRunner()


async def test_subscriber_sees_full_stream_without_loss_or_dup(runner):
    release = asyncio.Event()

    async def orchestrate(db, cancel_event, **kwargs):
        yield text_event("hello")
        await release.wait()
        yield tool_start_event("kubectl_get", {"resource": "pods"}, "t1")
        yield done_event()

    assert await runner.start("s1", orchestrate, {}) is True
    sub = await runner.subscribe("s1")
    assert sub is not None
    snapshot, queue = sub
    release.set()

    events = await _drain(snapshot, queue)
    assert [e["type"] for e in events] == ["text_delta", "tool_start", "done"]


async def test_turn_survives_with_no_subscriber_then_evicts(runner):
    """Core of the bug fix: the turn runs to completion even if nobody is listening."""
    ran = asyncio.Event()

    async def orchestrate(db, cancel_event, **kwargs):
        ran.set()
        yield done_event()

    assert await runner.start("s1", orchestrate, {}) is True
    await asyncio.wait_for(ran.wait(), timeout=2)
    # Let the task reach its finally (eviction).
    for _ in range(50):
        if not await runner.is_active("s1"):
            break
        await asyncio.sleep(0.01)
    assert await runner.is_active("s1") is False
    # No active turn -> subscribe returns None (client reloads via GET session).
    assert await runner.subscribe("s1") is None


async def test_one_active_turn_per_session(runner):
    release = asyncio.Event()

    async def orchestrate(db, cancel_event, **kwargs):
        await release.wait()
        yield done_event()

    assert await runner.start("s1", orchestrate, {}) is True
    # Second start while the first is active is rejected.
    assert await runner.start("s1", orchestrate, {}) is False
    release.set()


async def test_fan_out_to_multiple_subscribers(runner):
    release = asyncio.Event()

    async def orchestrate(db, cancel_event, **kwargs):
        yield text_event("a")
        await release.wait()
        yield done_event()

    await runner.start("s1", orchestrate, {})
    sub1 = await runner.subscribe("s1")
    sub2 = await runner.subscribe("s1")
    assert sub1 is not None and sub2 is not None
    release.set()

    events1 = await _drain(*sub1)
    events2 = await _drain(*sub2)
    assert [e["type"] for e in events1] == ["text_delta", "done"]
    assert [e["type"] for e in events2] == ["text_delta", "done"]


async def test_cancel_sets_event_and_stops_turn(runner):
    async def orchestrate(db, cancel_event, **kwargs):
        yield text_event("working")
        while not cancel_event.is_set():
            await asyncio.sleep(0.01)
        yield done_event(stopped=True)

    await runner.start("s1", orchestrate, {})
    sub = await runner.subscribe("s1")
    assert sub is not None
    await runner.cancel("s1")

    events = await _drain(*sub)
    assert events[-1]["type"] == "done"
    assert events[-1]["metadata"]["stopped"] is True
    assert await runner.is_active("s1") is False


async def test_orchestrator_error_emits_error_event(runner):
    async def orchestrate(db, cancel_event, **kwargs):
        yield text_event("before boom")
        raise RuntimeError("boom")

    await runner.start("s1", orchestrate, {})
    sub = await runner.subscribe("s1")
    assert sub is not None
    events = await _drain(*sub)
    assert events[-1]["type"] == "error"
    assert events[-1]["is_error"] is True
