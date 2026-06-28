"""
DB-free agentic loop for the standalone ``inkwest_agent`` (Converse-only).

The sole Bedrock Converse tool-use loop: it holds no DB session — history comes in
via the turn-spec, and the loop emits the canonical ``ChatEvent`` stream (the broker
persists from it). Aggregate usage rides on the terminal ``done`` event's metadata
instead of being written to the DB here.

Cancellation is **by-kill** (the broker terminates the process), so there is no
in-process ``cancel_event`` — the loop runs to a terminal stop or the broker
SIGKILLs it. Tool execution reuses ``tool_execution.execute_tool_calls``
(DB-free; Profile A, env-enforced).
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from botocore.exceptions import ClientError

from app.services.events import (
    ChatEvent,
    done_event,
    error_event,
    status_update_event,
    text_event,
    tool_result_event,
    tool_start_event,
)
from app.services.message_window import truncate_messages
from app.services.tool_execution import ToolRunResult, execute_tool_calls

from .clock import current_datetime, is_datetime_tool
from .converse import image_blocks_for_attachments, to_converse_message, to_converse_tools
from .exec import BASH_DEFAULT_TIMEOUT, is_exec_tool, run_exec_tool
from .mcp import MCPManager, load_mcp_config
from .protocol import TurnSpec
from .skills import is_skill_tool, read_skill


def _result_block(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
    """An Anthropic-shaped tool_result block (matches execute_tool_calls' output)."""
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


async def run_turn(spec: TurnSpec, client: Any) -> AsyncIterator[ChatEvent]:
    """Run one turn over Bedrock Converse, yielding the ChatEvent stream.

    ``client`` is a ready bedrock-runtime client (injected so tests can mock the
    blocking ``converse`` call). The loop is otherwise self-contained.
    """
    # The new user turn is appended here (the broker keeps it out of history).
    # mode=replay is the only live mode: the broker seeds the full transcript every
    # turn (the custom agent keeps none of its own). resume/resume_token are reserved.
    user_turn: dict[str, Any] = {"role": "user", "content": spec.user_message}
    messages: list[dict[str, Any]] = [*spec.context.history, user_turn]
    # Image attachments (P1.8): translate to Converse image blocks ONCE, here, gated on the
    # model's IMAGE capability. The blocks are Converse-native (not Anthropic-shaped), so
    # they're merged onto the freshly-built user turn AFTER history translation in the loop
    # rather than stored in the canonical `messages` (keeps Converse-ism out of the history
    # the broker persists). A text-only model yields no blocks — every image is skipped.
    image_blocks, skipped = image_blocks_for_attachments(
        spec.attachments, image_capable=spec.image_capable
    )
    if skipped:
        # Surface why an image didn't make it (text-only model / unsupported / oversized) so
        # the user isn't left wondering — non-fatal, the turn proceeds text-only for those.
        yield status_update_event(
            iteration=0,
            total_tokens=0,
            total_cost_cents=0,
            tool_calls=0,
            message="Some attachments were not sent as images: " + "; ".join(skipped),
        )

    system_blocks = [{"text": spec.system_prompt}]

    # Connect the conversation's enabled MCP servers for the lifetime of this turn
    # (P1.7). The manager is empty/degraded when MCP is unconfigured or the SDK is
    # absent — never an error — so the loop runs regardless. Its tools merge into the
    # toolConfig alongside the synthetic read_skill tool (skills as progressive
    # disclosure). MCP-owned + skill tool calls are routed here; everything else goes
    # to the base executor.
    async with MCPManager(load_mcp_config(spec.mcp_config_path)) as mcp:
        tool_config = to_converse_tools(skills=spec.skills, mcp_tools=mcp.converse_tools)
        async for event in _run_loop(
            spec, client, messages, system_blocks, tool_config, mcp, image_blocks, user_turn
        ):
            yield event


async def _run_loop(
    spec: TurnSpec,
    client: Any,
    messages: list[dict[str, Any]],
    system_blocks: list[dict[str, Any]],
    tool_config: dict[str, Any],
    mcp: MCPManager,
    image_blocks: list[dict[str, Any]],
    image_turn: dict[str, Any],
) -> AsyncIterator[ChatEvent]:
    """The Converse tool-use loop proper, with MCP connected for the turn."""
    session_uuid = uuid.UUID(spec.session_id)
    user_id = uuid.UUID(spec.user_id) if spec.user_id else None

    total_input_tokens = 0
    total_output_tokens = 0
    total_tool_calls = 0
    iteration = 0
    iter_latency_ms = 0

    while iteration < spec.limits.max_iterations:
        iteration += 1

        if len(messages) > spec.limits.max_conversation_messages:
            messages = truncate_messages(messages, spec.limits.max_conversation_messages)

        iter_start = time.monotonic()
        converse_messages = [to_converse_message(m) for m in messages]
        # Merge the P1.8 image blocks onto the original user turn (matched by object
        # identity so truncation/duplicate text can't misplace them). Skipped when
        # truncation dropped that turn or there are no images. Converse wants images
        # before text in the block list.
        if image_blocks:
            pos = next((i for i, m in enumerate(messages) if m is image_turn), None)
            if pos is not None:
                converse_messages[pos]["content"] = [
                    *image_blocks,
                    *converse_messages[pos]["content"],
                ]

        try:
            response = await asyncio.to_thread(
                lambda: client.converse(
                    modelId=spec.invoke_target,
                    messages=converse_messages,
                    system=system_blocks,
                    toolConfig=tool_config,
                    inferenceConfig={"maxTokens": spec.limits.max_tokens},
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
            # Route per call so skill (read_skill) and MCP-owned tools — which the base
            # executor doesn't know — are handled here, while kubectl/connector calls keep
            # going through execute_tool_calls (env-enforced creds). Results are collected
            # in tool_use order so the next user turn's toolResult blocks stay aligned.
            tool_results: list[dict[str, Any]] = []
            for call in tool_calls:
                name = call["name"]
                if is_skill_tool(name):
                    yield tool_start_event(name, call["input"], call["id"])
                    output, is_error = read_skill(call["input"], spec.skills, spec.skills_root)
                    yield tool_result_event(name, output, call["id"], is_error)
                    tool_results.append(_result_block(call["id"], output, is_error))
                elif is_datetime_tool(name):
                    yield tool_start_event(name, call["input"], call["id"])
                    output, is_error = current_datetime(call["input"])
                    yield tool_result_event(name, output, call["id"], is_error)
                    tool_results.append(_result_block(call["id"], output, is_error))
                elif is_exec_tool(name):
                    # Free-form exec (bash/read_file/write_file): synthetic, sandbox-only —
                    # handled here so it never reaches the shared (unsandboxed) executor.
                    yield tool_start_event(name, call["input"], call["id"])
                    output, is_error = await run_exec_tool(
                        name,
                        call["input"],
                        timeout=spec.limits.bash_timeout_seconds or BASH_DEFAULT_TIMEOUT,
                    )
                    yield tool_result_event(name, output, call["id"], is_error)
                    tool_results.append(_result_block(call["id"], output, is_error))
                elif mcp.owns(name):
                    yield tool_start_event(name, call["input"], call["id"])
                    output, is_error = await mcp.call(name, call["input"])
                    yield tool_result_event(name, output, call["id"], is_error)
                    tool_results.append(_result_block(call["id"], output, is_error))
                else:
                    run = ToolRunResult()
                    async for ev in execute_tool_calls(
                        run,
                        tool_calls=[call],
                        user_id=user_id,
                        session_id=session_uuid,
                        session_id_str=spec.session_id,
                        kubeconfig_path=spec.kubeconfig_path,
                        context_name=spec.context_name,
                        cancel_event=None,  # cancel-by-kill: the broker terminates the process
                        # Creds were pre-provided by the broker as env (one-way handoff); the
                        # agent can't reach the portal credential store.
                        projected_creds=True,
                    ):
                        yield ev
                    tool_results.extend(run.tool_results)
            messages.append({"role": "user", "content": tool_results})
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
        f"Reached maximum iterations ({spec.limits.max_iterations}). "
        "The conversation has become too complex. Please start a new session."
    )
