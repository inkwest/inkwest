"""
Bedrock Converse orchestrator for open-weight (non-Anthropic) models.

The Anthropic SDK (used by app.services.orchestrator, even on Bedrock) only
speaks the Claude Messages protocol. Open-weight Bedrock models — Llama,
Mistral, Nova, DeepSeek… — are invoked through the unified Bedrock **Converse**
API instead. This module implements the same tool-use loop and yields the same
``ChatEvent`` stream as the Anthropic orchestrator, so the WebSocket layer,
persistence, and frontend are unchanged.

Design notes:
- The DB stores conversation history in the Anthropic content-block shape used
  everywhere else; this module translates only at the API boundary (Anthropic
  blocks <-> Converse blocks), so a session can switch between Claude and open
  models without a schema fork.
- Converse is invoked non-streaming (one ``converse`` call per iteration),
  matching the default Anthropic orchestrator which also yields whole text
  blocks. boto3 is synchronous, so the blocking call runs in a worker thread —
  cancellation is honored at the loop boundaries (the same granularity as the
  non-streaming Anthropic path).
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import (
    MAX_CONVERSATION_MESSAGES,
    MAX_ORCHESTRATION_ITERATIONS,
    MAX_RESPONSE_TOKENS,
)
from app.services import model_registry
from app.services.ai_client import resolve_session_model
from app.services.events import ChatEvent, done_event, error_event, text_event
from app.services.message_window import truncate_messages
from app.services.messages import (
    ensure_session_title,
    get_session_messages,
    save_message,
)
from app.services.orchestrator_common import finalize_turn
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.services.tool_execution import ToolRunResult, execute_tool_calls
from app.tools.definitions import get_tool_definitions

logger = logging.getLogger(__name__)


def _tool_result_text(content: Any) -> str:
    """Flatten a stored tool_result's content into plain text for Converse."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content)


def _to_converse_message(message: dict[str, Any]) -> dict[str, Any]:
    """Translate one Anthropic-shaped history message into a Converse message."""
    content = message["content"]
    blocks: list[dict[str, Any]] = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"text": content})
    else:
        for block in content:
            btype = block.get("type")
            if btype == "text":
                if block.get("text", "").strip():
                    blocks.append({"text": block["text"]})
            elif btype == "tool_use":
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": block["id"],
                            "name": block["name"],
                            "input": block.get("input") or {},
                        }
                    }
                )
            elif btype == "tool_result":
                blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": block["tool_use_id"],
                            "content": [
                                {"text": _tool_result_text(block.get("content")) or "(no output)"}
                            ],
                            "status": "error" if block.get("is_error") else "success",
                        }
                    }
                )
            # 'thinking' blocks (Claude-only) are dropped — open models reject them.
    if not blocks:
        # Converse rejects empty content; keep alternation intact with a stub.
        blocks.append({"text": "(no content)"})
    return {"role": message["role"], "content": blocks}


def _to_converse_tools() -> dict[str, Any]:
    """Translate the Anthropic tool definitions into a Converse toolConfig."""
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "inputSchema": {"json": tool["input_schema"]},
                }
            }
            for tool in get_tool_definitions()
        ]
    }


# Bound the call so a hung/credential-stalled Bedrock endpoint can't pin a
# worker thread indefinitely (boto3 runs in asyncio.to_thread and cannot be
# cancelled mid-call). Read timeout covers generation latency; 2 attempts caps
# worst-case wall time, unlike botocore's unbounded default credential resolution.
_BEDROCK_CONFIG = Config(
    connect_timeout=10, read_timeout=120, retries={"max_attempts": 2, "mode": "standard"}
)


@lru_cache(maxsize=4)
def _cached_bedrock_client(
    region: str | None, access_key: str | None, secret_key: str | None
) -> Any:
    kwargs: dict[str, Any] = {"region_name": region, "config": _BEDROCK_CONFIG}
    # Explicit credentials when configured; otherwise the default chain applies.
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    return boto3.client("bedrock-runtime", **kwargs)


def _make_bedrock_client() -> Any:
    """Cached bedrock-runtime client (creation is blocking — call via to_thread).

    botocore clients are thread-safe for calls, so one client is reused across
    turns and to_thread workers — mirrors get_cached_ai_client for the SDK path.
    """
    settings = get_settings()
    return _cached_bedrock_client(
        settings.aws_region, settings.aws_access_key_id, settings.aws_secret_access_key
    )


async def orchestrate_chat_converse(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    cluster_name: str | None,
    kubeconfig_path: str | None,
    context_name: str | None,
    user_message: str,
    namespaces: list[str] | None = None,
    user_id: uuid.UUID | None = None,
    cancel_event: asyncio.Event | None = None,
    persist_user_message: bool = True,
) -> AsyncIterator[ChatEvent]:
    """Tool-use loop over the Bedrock Converse API. Mirrors orchestrate_chat."""
    if isinstance(session_id, str):
        session_id_str = session_id
        session_id = uuid.UUID(session_id)
    else:
        session_id_str = str(session_id)

    logger.info(f"Starting Converse orchestration for session {session_id_str}")

    settings = get_settings()
    if not settings.aws_region:
        yield error_event("AWS region is not configured for Bedrock Converse.")
        return
    try:
        client = await asyncio.to_thread(_make_bedrock_client)
    except (BotoCoreError, ClientError) as e:
        logger.error(f"Failed to create Bedrock client: {e}")
        yield error_event(f"AI service unavailable: {str(e)}")
        return

    try:
        messages = await get_session_messages(db, session_id)
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
        yield error_event(f"Failed to load conversation history: {str(e)}")
        return

    if persist_user_message:
        messages.append({"role": "user", "content": user_message})
        try:
            await save_message(db, session_id, "user", user_message)
        except Exception as e:
            logger.error(f"Failed to save user message: {e}")
            yield error_event(f"Failed to save message: {str(e)}")
            return
        if len(messages) == 1:
            try:
                await ensure_session_title(db, session_id, user_message)
            except Exception as e:
                logger.warning(f"Failed to set session title: {e}")

    base_prompt = await resolve_session_system_prompt(db, session_id)
    system_prompt = get_system_prompt(cluster_name, namespaces, base_prompt=base_prompt)
    model = await resolve_session_model(db, session_id)

    # A custom row may pin an ARN (provisioned/imported model) as the invoke target.
    entry = model_registry.get_entry(model)
    model_target = entry.arn if entry and entry.arn else model
    max_tokens = MAX_RESPONSE_TOKENS
    if entry and isinstance(entry.options.get("max_tokens"), int):
        max_tokens = entry.options["max_tokens"]

    system_blocks = [{"text": system_prompt}]
    tool_config = _to_converse_tools()

    total_input_tokens = 0
    total_output_tokens = 0
    total_tool_calls = 0
    iteration = 0

    while iteration < MAX_ORCHESTRATION_ITERATIONS:
        iteration += 1

        if cancel_event and cancel_event.is_set():
            yield done_event(stopped=True)
            return

        if len(messages) > MAX_CONVERSATION_MESSAGES:
            messages = truncate_messages(messages, MAX_CONVERSATION_MESSAGES)

        iter_start = time.monotonic()
        converse_messages = [_to_converse_message(m) for m in messages]

        try:
            response = await asyncio.to_thread(
                lambda: client.converse(
                    modelId=model_target,
                    messages=converse_messages,
                    system=system_blocks,
                    toolConfig=tool_config,
                    inferenceConfig={"maxTokens": max_tokens},
                )
            )
            iter_latency_ms = int((time.monotonic() - iter_start) * 1000)
        except ClientError as e:
            logger.exception("Bedrock Converse API error")
            yield error_event(
                f"AI service error: {e.response.get('Error', {}).get('Message', str(e))}"
            )
            return
        except Exception as e:
            # Covers BotoCoreError (connection/credential) and any other failure —
            # converse() runs in a worker thread, so nothing else can cancel it.
            logger.exception("Unexpected error calling Bedrock Converse")
            yield error_event(f"AI error: {str(e)}")
            return

        if cancel_event and cancel_event.is_set():
            yield done_event(stopped=True)
            return

        usage = response.get("usage", {})
        iter_input_tokens = usage.get("inputTokens", 0)
        iter_output_tokens = usage.get("outputTokens", 0)
        total_input_tokens += iter_input_tokens
        total_output_tokens += iter_output_tokens

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

        try:
            await save_message(
                db,
                session_id,
                "assistant",
                assistant_content,
                tool_calls=tool_calls if tool_calls else None,
                input_tokens=iter_input_tokens,
                output_tokens=iter_output_tokens,
                latency_ms=iter_latency_ms,
                model=model,
            )
        except Exception as e:
            logger.error(f"Failed to save assistant message: {e}")

        # Converse stop reasons: tool_use drives another turn; everything else
        # (end_turn, max_tokens, stop_sequence, guardrail…) ends this turn.
        if stop_reason == "tool_use" and tool_calls:
            if cancel_event and cancel_event.is_set():
                yield done_event(stopped=True)
                return

            messages.append({"role": "assistant", "content": assistant_content})
            run = ToolRunResult()
            async for ev in execute_tool_calls(
                run,
                tool_calls=tool_calls,
                user_id=user_id,
                session_id=session_id,
                session_id_str=session_id_str,
                kubeconfig_path=kubeconfig_path,
                context_name=context_name,
                cancel_event=cancel_event,
            ):
                yield ev
            if run.cancelled:
                yield done_event(stopped=True)
                return

            messages.append({"role": "user", "content": run.tool_results})
            try:
                await save_message(db, session_id, "tool_result", run.tool_results)
            except Exception as e:
                logger.error(f"Failed to save tool results: {e}")
            continue

        # Terminal stop reason (end_turn, max_tokens, stop_sequence, guardrail…).
        meta = await finalize_turn(
            db,
            session_id=session_id,
            user_id=user_id,
            model=model,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            total_tool_calls=total_tool_calls,
            iteration=iteration,
            iter_latency_ms=iter_latency_ms,
        )
        yield done_event(metadata=meta)
        return

    logger.warning(f"Max iterations ({MAX_ORCHESTRATION_ITERATIONS}) reached for {session_id_str}")
    yield error_event(
        f"Reached maximum iterations ({MAX_ORCHESTRATION_ITERATIONS}). "
        "The conversation has become too complex. Please start a new session."
    )
