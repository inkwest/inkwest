"""Idle-timeout guard for the shared agent/CLI stdout stream (_stream_stdout).

A silently-stalled subprocess (model never emits a first token, or a tool call wedges)
would otherwise block the read forever and hang the single chat process loop — the whole
session stuck in "thinking". The guard trips only on genuine silence for the full window,
and *resets on every emitted line* so a live-but-slow turn is never killed.
"""

import asyncio
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
