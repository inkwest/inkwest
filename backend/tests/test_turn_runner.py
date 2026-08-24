"""Tests for the detached turn runner (orchestration decoupled from the socket)."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import ANY, AsyncMock, patch

import pytest

from app.services.events import done_event, error_event, text_event, tool_start_event
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


async def test_slot_frees_on_done_before_finally(runner):
    """Regression: a client that starts its next turn the instant it receives `done`
    (e.g. flushing a client-side message queue) must not be rejected. The slot is
    freed when `done` is emitted, not in the deferred finally — so the next turn
    starts even while the previous turn's task is still winding down."""
    hold = asyncio.Event()

    async def orchestrate(db, cancel_event, **kwargs):
        yield done_event()
        # Keep the turn task alive past `done` so its finally (eviction) hasn't run yet.
        await hold.wait()

    assert await runner.start("s1", orchestrate, {}) is True
    sub = await runner.subscribe("s1")
    assert sub is not None
    snapshot, queue = sub
    seen = list(snapshot)
    while not any(e["type"] == "done" for e in seen):
        seen.append(await asyncio.wait_for(queue.get(), timeout=2))

    # Turn task is still alive (hold not set), yet the slot is already free.
    assert await runner.is_active("s1") is False

    async def orchestrate2(db, cancel_event, **kwargs):
        yield done_event()

    assert await runner.start("s1", orchestrate2, {}) is True
    hold.set()  # release the first turn so its task can finish cleanly


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


async def test_error_event_persists_abort_marker(runner):
    """A turn ending on a terminal error event leaves a 'turn aborted' session event
    carrying the error text — otherwise a reloaded/shared view ends on a dangling tool
    call with no cause and viewers blame the last tool."""

    async def orchestrate(db, cancel_event, **kwargs):
        yield text_event("partial")
        yield error_event("Request timed out: agent made no progress for 900s")

    marker = AsyncMock()
    with patch("app.services.turn_runner.save_session_event", marker):
        await runner.start("s1", orchestrate, {})
        sub = await runner.subscribe("s1")
        assert sub is not None
        await _drain(*sub)  # TURN_END arrives after the marker persist in the finally

    marker.assert_awaited_once_with(
        ANY,
        "s1",
        "turn",
        "aborted",
        [{"reason": "Request timed out: agent made no progress for 900s"}],
    )


async def test_orchestrator_crash_persists_abort_marker(runner):
    async def orchestrate(db, cancel_event, **kwargs):
        yield text_event("before boom")
        raise RuntimeError("boom")

    marker = AsyncMock()
    with patch("app.services.turn_runner.save_session_event", marker):
        await runner.start("s1", orchestrate, {})
        sub = await runner.subscribe("s1")
        assert sub is not None
        events = await _drain(*sub)

    assert events[-1]["type"] == "error"
    marker.assert_awaited_once_with(
        ANY, "s1", "turn", "aborted", [{"reason": "An unexpected server error occurred"}]
    )


async def test_done_then_error_persists_no_marker(runner):
    """The CLI orchestrator can yield an error AFTER done (post-stream process.wait()
    cap): a cleanup failure on a finished answer must not stamp the completed turn
    with a durable 'Turn aborted' marker."""

    async def orchestrate(db, cancel_event, **kwargs):
        yield text_event("full answer")
        yield done_event()
        yield error_event("Request timed out: process did not exit")

    marker = AsyncMock()
    with patch("app.services.turn_runner.save_session_event", marker):
        await runner.start("s1", orchestrate, {})
        sub = await runner.subscribe("s1")
        assert sub is not None
        await _drain(*sub)

    marker.assert_not_awaited()


async def test_stopped_done_persists_no_marker(runner):
    """A user Stop is a deliberate, legible end — no abort marker (same for clean done)."""

    async def orchestrate(db, cancel_event, **kwargs):
        yield text_event("fine")
        yield done_event(stopped=True)

    marker = AsyncMock()
    with patch("app.services.turn_runner.save_session_event", marker):
        await runner.start("s1", orchestrate, {})
        sub = await runner.subscribe("s1")
        assert sub is not None
        await _drain(*sub)

    marker.assert_not_awaited()


async def test_abort_marker_reason_truncated(runner):
    """The marker is a one-line summary — a stderr-tail-sized error is capped."""

    async def orchestrate(db, cancel_event, **kwargs):
        yield error_event("x" * 2000)

    marker = AsyncMock()
    with patch("app.services.turn_runner.save_session_event", marker):
        await runner.start("s1", orchestrate, {})
        sub = await runner.subscribe("s1")
        assert sub is not None
        await _drain(*sub)

    reason = marker.await_args.args[4][0]["reason"]
    assert len(reason) == 500
    assert reason.endswith("…")
