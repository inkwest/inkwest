"""
Tool-call execution — the DB-free half of the shared orchestration loop.

Running a batch of tool calls and yielding their ``ChatEvent``s needs no DB session
so it lives here, away from usage/stats persistence (sqlalchemy-bound).
The sandboxed-agent orchestrator (``orchestrator_inkwest_agent``) and the standalone
``inkwest_agent`` loop both share this — a fix in tool dispatch, the connector re-auth
affordance, or cancel handling lands once for both.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.core import metrics
from app.services.events import (
    ChatEvent,
    connector_required_event,
    tool_result_event,
    tool_start_event,
)
from app.tools.connector_exec import connector_for_tool, is_needs_auth
from app.tools.definitions import ALL_TOOL_NAMES
from app.tools.dispatch import execute_tool
from app.tools.exec_context import ToolExecContext

logger = logging.getLogger(__name__)


@dataclass
class ToolRunResult:
    """Out-param for execute_tool_calls: the tool_result blocks + cancel flag."""

    tool_results: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False


# Shown as the tool_result for any tool_use the user cancelled before it ran.
CANCELLED_TOOL_RESULT = "Tool execution cancelled by the user."


def _cancelled_result(tool_id: str) -> dict[str, Any]:
    """An Anthropic-shaped tool_result standing in for a cancelled tool_use."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": CANCELLED_TOOL_RESULT,
        "is_error": True,
    }


async def execute_tool_calls(
    result: ToolRunResult,
    *,
    tool_calls: list[dict[str, Any]],
    user_id: uuid.UUID | None,
    session_id: uuid.UUID,
    session_id_str: str,
    kubeconfig_path: str | None,
    context_name: str | None,
    cancel_event: asyncio.Event | None,
    projected_creds: bool = False,
) -> AsyncIterator[ChatEvent]:
    """Execute each tool call, yielding tool_start/result/connector events.

    The Anthropic-shaped tool_result blocks are appended to ``result``; the
    caller persists them (so the save runs in the caller's module). On a mid-loop
    cancel this sets ``result.cancelled`` and fills a synthetic "cancelled"
    result for every not-yet-executed tool_use before returning, so
    ``result.tool_results`` always pairs 1:1 with ``tool_calls``. The caller MUST
    persist them even when cancelled: the assistant turn carrying the tool_use
    blocks is already saved, and an orphaned tool_use (no matching tool_result)
    makes the next API call fail validation on both Anthropic and Bedrock.

    ``projected_creds`` is set by the sandboxed inkwest-agent: connector creds were
    pre-provided by the broker as env, so executors read them from the environment
    rather than the portal credential store (which the agent process can't reach).
    """
    for idx, tool_call in enumerate(tool_calls):
        if cancel_event and cancel_event.is_set():
            logger.info("Chat cancelled during tool execution")
            result.cancelled = True
            # Keep tool_use/tool_result pairing intact: stand in a cancelled
            # result for this call and every one after it (none have run).
            result.tool_results.extend(_cancelled_result(tc["id"]) for tc in tool_calls[idx:])
            return

        tool_id = tool_call["id"]
        tool_name = tool_call["name"]
        tool_input = tool_call["input"]

        logger.debug(f"Executing tool: {tool_name}")
        yield tool_start_event(tool_name, tool_input, tool_id)

        # Execute the tool (kubectl or a connector tool). Connector tools fetch
        # the user's temporary credential from the RAM store by
        # (user_id, session_id) — never via the model or tool input.
        tool_ctx = ToolExecContext(
            user_id=user_id,
            session_id=session_id,
            kubeconfig_path=kubeconfig_path,
            context_name=context_name,
            projected_creds=projected_creds,
        )
        started_at = time.monotonic()
        try:
            output, is_error = await execute_tool(tool_name, tool_input, tool_ctx)
        except Exception as e:
            logger.exception(f"Exception executing tool {tool_name}")
            output = f"Error executing tool: {str(e)}"
            is_error = True
        # Label with the RAW model-supplied tool name — including names outside our schema —
        # deliberately, to observe what tools the model attempts (hallucinations included).
        # Trade-off accepted: a flood of distinct bogus names would grow label cardinality;
        # revisit (clamp to a known set) if that shows up in practice.
        # The duration histogram is per-bucket per-label, so clamp the raw name to the known
        # set here (a hallucinated name → "unknown") to bound series. The counter
        # (record_tool_call) keeps the raw name deliberately.
        dur_tool = tool_name if tool_name in ALL_TOOL_NAMES else "unknown"
        metrics.tool_duration_seconds.labels(
            tool=dur_tool, engine=metrics.turn_engine(), model=metrics.turn_model()
        ).observe(time.monotonic() - started_at)
        metrics.record_tool_call(tool_name, is_error)

        yield tool_result_event(tool_name, output, tool_id, is_error)

        # Surface a first-class re-auth affordance when a connector is missing.
        connector = connector_for_tool(tool_name)
        if connector and is_needs_auth(output, is_error):
            yield connector_required_event(connector, session_id_str)

        result.tool_results.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": output,
                "is_error": is_error,
            }
        )
