"""
DB-free agentic loop for the standalone ``inkwest_agent`` (Converse-only).

Mirrors the tool-use loop of ``app.services.orchestrator_converse`` but holds no
DB session: history comes in via the turn-spec, and the loop emits the canonical
``ChatEvent`` stream (the broker persists from it). Aggregate usage rides on the
terminal ``done`` event's metadata instead of being written to the DB here.

Cancellation is **by-kill** (the broker terminates the process), so there is no
in-process ``cancel_event`` — the loop runs to a terminal stop or the broker
SIGKILLs it. Tool execution reuses ``orchestrator_common.execute_tool_calls``
(already DB-free; Profile A, env-enforced).
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from botocore.exceptions import ClientError

from app.services.events import ChatEvent, done_event, error_event, text_event
from app.services.messages import truncate_messages
from app.services.orchestrator_common import ToolRunResult, execute_tool_calls

from .converse import to_converse_message, to_converse_tools
from .protocol import TurnSpec


async def run_turn(spec: TurnSpec, client: Any) -> AsyncIterator[ChatEvent]:
    """Run one turn over Bedrock Converse, yielding the ChatEvent stream.

    ``client`` is a ready bedrock-runtime client (injected so tests can mock the
    blocking ``converse`` call). The loop is otherwise self-contained.
    """
    session_uuid = uuid.UUID(spec.session_id)
    user_id = uuid.UUID(spec.user_id) if spec.user_id else None

    # The new user turn is appended here (the broker keeps it out of history).
    messages: list[dict[str, Any]] = [*spec.history, {"role": "user", "content": spec.user_message}]

    system_blocks = [{"text": spec.system_prompt}]
    tool_config = to_converse_tools()

    total_input_tokens = 0
    total_output_tokens = 0
    total_tool_calls = 0
    iteration = 0
    iter_latency_ms = 0

    while iteration < spec.max_iterations:
        iteration += 1

        if len(messages) > spec.max_conversation_messages:
            messages = truncate_messages(messages, spec.max_conversation_messages)

        iter_start = time.monotonic()
        converse_messages = [to_converse_message(m) for m in messages]

        try:
            response = await asyncio.to_thread(
                lambda: client.converse(
                    modelId=spec.invoke_target,
                    messages=converse_messages,
                    system=system_blocks,
                    toolConfig=tool_config,
                    inferenceConfig={"maxTokens": spec.max_tokens},
                )
            )
            iter_latency_ms = int((time.monotonic() - iter_start) * 1000)
        except ClientError as e:
            yield error_event(
                f"AI service error: {e.response.get('Error', {}).get('Message', str(e))}"
            )
            return
        except Exception as e:
            # converse() runs in a worker thread; nothing else can cancel it.
            yield error_event(f"AI error: {str(e)}")
            return

        usage = response.get("usage", {})
        total_input_tokens += usage.get("inputTokens", 0)
        total_output_tokens += usage.get("outputTokens", 0)

        out_content = response.get("output", {}).get("message", {}).get("content", [])
        stop_reason = response.get("stopReason", "end_turn")

        assistant_content: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []

        for block in out_content:
            if "text" in block:
                yield text_event(block["text"])
                assistant_content.append({"type": "text", "text": block["text"]})
            elif "toolUse" in block:
                tu = block["toolUse"]
                tool_calls.append(
                    {"id": tu["toolUseId"], "name": tu["name"], "input": tu.get("input") or {}}
                )
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": tu["toolUseId"],
                        "name": tu["name"],
                        "input": tu.get("input") or {},
                    }
                )
                total_tool_calls += 1

        # tool_use drives another turn; every other stop reason ends this one.
        if stop_reason == "tool_use" and tool_calls:
            messages.append({"role": "assistant", "content": assistant_content})
            run = ToolRunResult()
            async for ev in execute_tool_calls(
                run,
                tool_calls=tool_calls,
                user_id=user_id,
                session_id=session_uuid,
                session_id_str=spec.session_id,
                kubeconfig_path=spec.kubeconfig_path,
                context_name=spec.context_name,
                cancel_event=None,  # cancel-by-kill: the broker terminates the process
            ):
                yield ev
            messages.append({"role": "user", "content": run.tool_results})
            continue

        # Terminal stop reason (end_turn, max_tokens, stop_sequence, guardrail…).
        yield done_event(
            metadata={
                "iterations": iteration,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "tool_calls": total_tool_calls,
                "latency_ms": iter_latency_ms,
            }
        )
        return

    yield error_event(
        f"Reached maximum iterations ({spec.max_iterations}). "
        "The conversation has become too complex. Please start a new session."
    )
