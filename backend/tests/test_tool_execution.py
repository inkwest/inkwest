"""Tests for the shared tool-call executor, focused on cancel handling.

A user stop mid-tool must leave ``result.tool_results`` paired 1:1 with the
tool_use blocks of the already-persisted assistant turn — an orphaned tool_use
(no matching tool_result) makes the next API call fail on Anthropic and Bedrock.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.tool_execution import (
    CANCELLED_TOOL_RESULT,
    ToolRunResult,
    execute_tool_calls,
)


class _CancelOnNth:
    """is_set() returns True only from the (n+1)-th call onward."""

    def __init__(self, n: int) -> None:
        self.calls = 0
        self.n = n

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls > self.n


def _call(tool_id: str) -> dict:
    return {"id": tool_id, "name": "kubectl_get", "input": {"resource": "pods"}}


async def _drain(run: ToolRunResult, tool_calls: list[dict], cancel_event) -> None:
    async for _ in execute_tool_calls(
        run,
        tool_calls=tool_calls,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        session_id_str="sess",
        kubeconfig_path=None,
        context_name=None,
        cancel_event=cancel_event,
    ):
        pass


@pytest.mark.asyncio
async def test_partial_cancel_keeps_pairing():
    """First tool runs; cancel before the second → both tool_use ids get a result,
    the cancelled one synthetic."""
    run = ToolRunResult()
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pods listed", False)),
    ):
        await _drain(run, [_call("toolu_a"), _call("toolu_b")], _CancelOnNth(1))

    assert run.cancelled is True
    assert [b["tool_use_id"] for b in run.tool_results] == ["toolu_a", "toolu_b"]
    # First is the real result, second the synthetic cancellation.
    assert run.tool_results[0]["content"] == "pods listed"
    assert run.tool_results[0]["is_error"] is False
    assert run.tool_results[1]["content"] == CANCELLED_TOOL_RESULT
    assert run.tool_results[1]["is_error"] is True


@pytest.mark.asyncio
async def test_immediate_cancel_synthesizes_all():
    """Cancel before the first tool runs → every tool_use gets a synthetic result
    and execute_tool is never called."""
    run = ToolRunResult()
    exec_mock = AsyncMock(return_value=("unused", False))
    with patch("app.services.tool_execution.execute_tool", new=exec_mock):
        await _drain(run, [_call("toolu_a"), _call("toolu_b")], _CancelOnNth(0))

    assert run.cancelled is True
    exec_mock.assert_not_awaited()
    assert [b["tool_use_id"] for b in run.tool_results] == ["toolu_a", "toolu_b"]
    assert all(b["content"] == CANCELLED_TOOL_RESULT for b in run.tool_results)


@pytest.mark.asyncio
async def test_tool_name_recorded_raw_for_metrics():
    """The raw model-supplied tool name becomes the metric label as-is — kept deliberately
    (even for names outside our schema) to observe what tools the model attempts."""
    from prometheus_client import REGISTRY

    name = "probe_" + uuid.uuid4().hex
    run = ToolRunResult()
    with patch(
        "app.services.tool_execution.execute_tool", new=AsyncMock(return_value=("nope", True))
    ):
        await _drain(run, [{"id": "t1", "name": name, "input": {}}], None)

    assert (
        REGISTRY.get_sample_value("inkwest_tool_calls_total", {"tool": name, "is_error": "True"})
        or 0
    ) >= 1
