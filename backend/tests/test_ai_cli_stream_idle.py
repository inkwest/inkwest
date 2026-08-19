"""Idle-timeout guard for the shared agent/CLI stdout stream (_stream_stdout).

A silently-stalled subprocess (model never emits a first token, or a tool call wedges)
would otherwise block the read forever and hang the single chat process loop — the whole
session stuck in "thinking". The guard trips only on genuine silence for the full window,
and *resets on every emitted line* so a live-but-slow turn is never killed.
"""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import ai_cli_client


class _ScriptedStdout:
    """Fake StreamReader: each readline() waits ``delay`` then returns ``data``.

    ``data == b""`` signals EOF (end of stream). An empty script also yields EOF.
    """

    def __init__(self, script: list[tuple[float, bytes]]):
        self._script = list(script)

    async def readline(self) -> bytes:
        if not self._script:
            return b""
        delay, data = self._script.pop(0)
        await asyncio.sleep(delay)
        return data


def _proc(script: list[tuple[float, bytes]]) -> SimpleNamespace:
    return SimpleNamespace(stdout=_ScriptedStdout(script))


async def _drain(process, **kwargs) -> list[str]:
    return [
        line
        async for line in ai_cli_client._stream_stdout(
            process,
            cancel_event=kwargs.get("cancel_event"),
            container=None,
            docker_path="docker",
            pid_file=None,
            idle_timeout=kwargs.get("idle_timeout"),
            progress=kwargs.get("progress"),
            progress_timeout=kwargs.get("progress_timeout"),
        )
    ]


@pytest.mark.asyncio
async def test_stalled_stream_trips_idle_timeout_and_kills_agent() -> None:
    # First read never lands within the window (10s > 0.05s idle) → stall.
    proc = _proc([(10.0, b"line1\n")])
    with patch.object(ai_cli_client, "_stop_agent", AsyncMock()) as stop:
        with pytest.raises(TimeoutError, match="no output"):
            await _drain(proc, idle_timeout=0.05)
    stop.assert_awaited_once()  # the stalled subprocess is killed, not left dangling


@pytest.mark.asyncio
async def test_active_stream_resets_the_window() -> None:
    # Gaps (0.04s) each stay under the window (0.1s), but total (>0.1s) exceeds it —
    # proves the deadline resets per line rather than being an absolute wall-clock.
    proc = _proc([(0.04, b"a\n"), (0.04, b"b\n"), (0.04, b"c\n")])
    with patch.object(ai_cli_client, "_stop_agent", AsyncMock()) as stop:
        lines = await _drain(proc, idle_timeout=0.1)
    assert lines == ["a", "b", "c"]
    stop.assert_not_awaited()  # a live stream is never treated as stalled


@pytest.mark.asyncio
async def test_none_disables_the_guard() -> None:
    # A gap longer than any window is fine when the guard is off (backward compatible).
    proc = _proc([(0.2, b"slow\n")])
    lines = await _drain(proc, idle_timeout=None)
    assert lines == ["slow"]


@pytest.mark.asyncio
async def test_cancel_takes_precedence_over_idle_timeout() -> None:
    # A pending stop must end the stream cleanly (kill + return), never surface as a
    # timeout error, even with the guard armed.
    ev = asyncio.Event()
    ev.set()
    proc = _proc([(10.0, b"x\n")])
    with patch.object(ai_cli_client, "_stop_agent", AsyncMock()) as stop:
        lines = await _drain(proc, cancel_event=ev, idle_timeout=0.05)
    assert lines == []
    stop.assert_awaited_once()


# --- progress guard ------------------------------------------------------------------
# The idle guard above resets on ANY line, so an agent that keeps emitting while advancing
# nothing renews it forever (production: 55 min of no model call and no tool call, with the
# 600 s idle guard never tripping). The progress guard is bumped by the CONSUMER, only on
# client-visible events, so output alone can no longer keep a wedged turn alive.


class _ChattyStdout:
    """Never silent: yields a keepalive line every ``interval``, forever."""

    def __init__(self, interval: float = 0.01):
        self._interval = interval

    async def readline(self) -> bytes:
        await asyncio.sleep(self._interval)
        return b'{"type":"stream_event"}\n'


@pytest.mark.asyncio
async def test_chatty_stream_with_no_progress_trips_progress_guard() -> None:
    """The regression case: loud output, zero advancement, idle guard nowhere near firing."""
    proc = SimpleNamespace(stdout=_ChattyStdout(0.01))
    tracker = ai_cli_client.ProgressTracker()  # never marked → no progress
    with patch.object(ai_cli_client, "_stop_agent", AsyncMock()) as stop:
        with pytest.raises(TimeoutError, match="no progress"):
            await _drain(
                proc,
                idle_timeout=30.0,  # would NEVER fire: a line lands every 10ms
                progress=tracker,
                progress_timeout=0.1,
            )
    stop.assert_awaited_once()  # the wedged agent is killed, not left orphaned


@pytest.mark.asyncio
async def test_marked_progress_keeps_a_chatty_stream_alive() -> None:
    """A live turn marks progress as it streams, so the guard must not fire."""
    proc = SimpleNamespace(stdout=_ChattyStdout(0.01))
    tracker = ai_cli_client.ProgressTracker()
    seen = 0
    with patch.object(ai_cli_client, "_stop_agent", AsyncMock()) as stop:
        async for _line in ai_cli_client._stream_stdout(
            proc,
            cancel_event=None,
            container=None,
            docker_path="docker",
            pid_file=None,
            idle_timeout=30.0,
            progress=tracker,
            progress_timeout=0.1,
        ):
            tracker.mark()  # what the orchestrator does on a client-visible event
            seen += 1
            if seen == 25:  # ~250ms, well past the 100ms window
                break
    assert seen == 25
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_wait_slice_does_not_trip_the_idle_guard() -> None:
    """Waits are sliced to the shortest deadline; a slice ending empty is NOT a stall.

    Guards against the accounting bug where ``wait_slice`` (min of both deadlines) was
    mistaken for the idle deadline, killing healthy turns early.
    """
    # Silent for 0.3s — longer than the 0.2s slice, shorter than the 1.0s idle window.
    proc = _proc([(0.3, b"late-but-fine\n")])
    tracker = ai_cli_client.ProgressTracker()
    stop_marking = asyncio.Event()

    async def keep_marking() -> None:
        while not stop_marking.is_set():
            tracker.mark()
            await asyncio.sleep(0.02)

    marker = asyncio.ensure_future(keep_marking())
    try:
        with patch.object(ai_cli_client, "_stop_agent", AsyncMock()) as stop:
            lines = await _drain(proc, idle_timeout=1.0, progress=tracker, progress_timeout=0.2)
    finally:
        stop_marking.set()
        await marker
    assert lines == ["late-but-fine"]
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_progress_guard_off_when_unconfigured() -> None:
    """No tracker / no window → only the legacy idle behaviour, unchanged."""
    proc = _proc([(0.0, b"a\n"), (0.0, b"b\n")])
    with patch.object(ai_cli_client, "_stop_agent", AsyncMock()) as stop:
        assert await _drain(proc, idle_timeout=1.0) == ["a", "b"]
    stop.assert_not_awaited()


# --- kill escalation -----------------------------------------------------------------
# SIGTERM is CAUGHT by the agent runtime and dispatched on its own event loop, so a wedged
# agent accepts it and never acts. SIGKILL is the only step that ends it — and turn teardown
# routinely cancels the stop mid-grace, which used to skip the escalation and orphan the
# agent (observed: four abandoned agents, the oldest running two days).


@pytest.mark.asyncio
async def test_stop_agent_escalates_to_sigkill_when_cancelled_mid_grace() -> None:
    sent: list[str] = []

    async def fake_signal(_docker, _container, _pid_file, sig):
        sent.append(sig)
        return True

    async def always_alive(*_a, **_k):
        return True  # wedged: never exits on SIGTERM

    proc = SimpleNamespace(returncode=0, terminate=lambda: None, kill=lambda: None)
    with (
        patch.object(ai_cli_client, "_container_signal", fake_signal),
        patch.object(ai_cli_client, "_container_alive", always_alive),
    ):
        task = asyncio.ensure_future(
            ai_cli_client._stop_agent(
                proc, container="c1", docker_path="docker", pid_file="/tmp/a.pid"
            )
        )
        await asyncio.sleep(0.05)  # let it send TERM and enter the grace loop
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for _ in range(10):  # let the detached kill land
            if "KILL" in sent:
                break
            await asyncio.sleep(0.01)
    assert sent == ["TERM", "KILL"], "cancellation must not skip the SIGKILL escalation"


@pytest.mark.asyncio
async def test_stop_agent_escalates_when_cancelled_during_term_signal() -> None:
    """The try/finally must cover the TERM docker-exec itself, not just the grace loop:
    a cancellation landing mid-TERM (docker exec slow under dind load) used to skip the
    SIGKILL entirely and re-orphan the agent."""
    sent: list[str] = []
    term_started = asyncio.Event()

    async def fake_signal(_docker, _container, _pid_file, sig):
        sent.append(sig)
        if sig == "TERM":
            term_started.set()
            await asyncio.sleep(30)  # docker exec hanging under dind load
        return True

    async def always_alive(*_a, **_k):
        return True

    proc = SimpleNamespace(returncode=0, terminate=lambda: None, kill=lambda: None)
    with (
        patch.object(ai_cli_client, "_container_signal", fake_signal),
        patch.object(ai_cli_client, "_container_alive", always_alive),
    ):
        task = asyncio.ensure_future(
            ai_cli_client._stop_agent(
                proc, container="c1", docker_path="docker", pid_file="/tmp/a.pid"
            )
        )
        await term_started.wait()  # cancel lands MID-TERM, before the grace loop
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        for _ in range(20):  # let the detached kill land
            if "KILL" in sent:
                break
            await asyncio.sleep(0.01)
    assert "KILL" in sent, f"cancellation during TERM must not skip SIGKILL; sent={sent}"


@pytest.mark.asyncio
async def test_stop_agent_skips_sigkill_when_agent_exits_on_sigterm() -> None:
    """A well-behaved agent that honours SIGTERM must not be SIGKILLed anyway."""
    sent: list[str] = []

    async def fake_signal(_docker, _container, _pid_file, sig):
        sent.append(sig)
        return True

    async def already_dead(*_a, **_k):
        return False

    # `wait` is reached here (unlike the cancelled case) via the trailing local-client reap.
    proc = SimpleNamespace(
        returncode=0, terminate=lambda: None, kill=lambda: None, wait=AsyncMock(return_value=0)
    )
    with (
        patch.object(ai_cli_client, "_container_signal", fake_signal),
        patch.object(ai_cli_client, "_container_alive", already_dead),
    ):
        await ai_cli_client._stop_agent(
            proc, container="c1", docker_path="docker", pid_file="/tmp/a.pid"
        )
    assert sent == ["TERM"]
