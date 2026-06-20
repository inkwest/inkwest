"""
Provider-agnostic pieces shared by the two API-based, non-streaming chat
orchestrators: ``orchestrator.orchestrate_chat`` (Anthropic SDK) and
``orchestrator_converse`` (Bedrock Converse).

The per-provider API call and its response parsing differ; everything
downstream of "we have the assistant content + tool calls" is identical, so it
lives here — a bug fix in tool dispatch, the connector re-auth affordance, or
usage accounting lands once for both.

Out of scope by design: ``orchestrator_cli`` drives an external CLI that runs
its own tool loop, so it has nothing to share here.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.budget_service import record_usage
from app.services.events import (
    ChatEvent,
    connector_required_event,
    tool_result_event,
    tool_start_event,
)
from app.services.messages import update_session_stats
from app.tools.connector_exec import connector_for_tool, is_needs_auth
from app.tools.dispatch import execute_tool
from app.tools.exec_context import ToolExecContext

logger = logging.getLogger(__name__)


@dataclass
class ToolRunResult:
    """Out-param for execute_tool_calls: the tool_result blocks + cancel flag."""

    tool_results: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False


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
    caller persists them (so the save runs in the caller's module, and a
    mid-loop cancel — which sets ``result.cancelled`` and returns early —
    discards the partial results, matching the prior per-orchestrator behavior).

    ``projected_creds`` is set by the sandboxed inkwest-agent: connector creds were
    pre-provided by the broker as env, so executors read them from the environment
    rather than the portal credential store (which the agent process can't reach).
    """
    for tool_call in tool_calls:
        if cancel_event and cancel_event.is_set():
            logger.info("Chat cancelled during tool execution")
            result.cancelled = True
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
        try:
            output, is_error = await execute_tool(tool_name, tool_input, tool_ctx)
        except Exception as e:
            logger.exception(f"Exception executing tool {tool_name}")
            output = f"Error executing tool: {str(e)}"
            is_error = True

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


async def finalize_turn(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID | None,
    model: str,
    total_input_tokens: int,
    total_output_tokens: int,
    total_tool_calls: int,
    iteration: int,
    iter_latency_ms: int,
) -> dict[str, Any]:
    """Persist session stats + usage and return the done-event metadata.

    Persistence is best-effort: on failure the turn still completes, with
    ``usage_tracking_failed`` flagged in the metadata for the client.
    """
    usage_failed = False
    try:
        await update_session_stats(
            db,
            session_id,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            tool_calls=total_tool_calls,
            iterations=iteration,
        )
        await record_usage(
            db,
            user_id=user_id,
            session_id=session_id,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            model=model,
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to persist session stats/usage: {e}")
        usage_failed = True
        # Leave the session usable: a failed commit aborts the transaction, so
        # roll back before the turn ends (defensive — the per-turn session is
        # normally discarded right after).
        try:
            await db.rollback()
        except Exception:
            logger.exception("Rollback after usage-persist failure also failed")

    meta: dict[str, Any] = {
        "iterations": iteration,
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "tool_calls": total_tool_calls,
        "latency_ms": iter_latency_ms,
    }
    if usage_failed:
        meta["usage_tracking_failed"] = True
    return meta
