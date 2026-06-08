"""
OpenAI orchestrator for Inkwest.

Direct OpenAI API integration using the official OpenAI Python SDK.
Supports any OpenAI-compatible endpoint via configurable base_url.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from openai import APIStatusError, AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import (
    MAX_API_RETRIES,
    MAX_CONVERSATION_MESSAGES,
    MAX_ORCHESTRATION_ITERATIONS,
    MAX_RESPONSE_TOKENS,
)
from app.services.budget_service import record_usage
from app.services.events import (
    ChatEvent,
    done_event,
    error_event,
    text_event,
    tool_result_event,
    tool_start_event,
)
from app.services.messages import (
    ensure_session_title,
    get_session_messages,
    save_message,
    truncate_messages,
    update_session_stats,
)
from app.services.orchestrator_openrouter import (
    _convert_messages_to_openai_format,
    _convert_tools_to_openai_format,
)
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.tools import ToolExecContext
from app.tools import execute_tools_parallel_ctx as execute_tools_parallel

logger = logging.getLogger(__name__)


def get_openai_client() -> AsyncOpenAI:
    """Get configured OpenAI client."""
    settings = get_settings()

    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for OpenAI provider")

    return AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )


_cached_openai_client: AsyncOpenAI | None = None
_openai_client_lock = asyncio.Lock()


async def get_cached_openai_client() -> AsyncOpenAI:
    """Get cached OpenAI client instance, reusing HTTP connection pool."""
    global _cached_openai_client
    if _cached_openai_client is not None:
        return _cached_openai_client
    async with _openai_client_lock:
        if _cached_openai_client is None:
            _cached_openai_client = get_openai_client()
        return _cached_openai_client


async def close_cached_openai_client() -> None:
    """Close the cached OpenAI client and release its connection pool."""
    global _cached_openai_client
    async with _openai_client_lock:
        if _cached_openai_client is not None:
            await _cached_openai_client.close()
            _cached_openai_client = None


async def orchestrate_chat_openai(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    cluster_name: str | None,
    kubeconfig_path: str | None,
    context_name: str | None,
    user_message: str,
    namespaces: list[str] | None = None,
    user_id: uuid.UUID | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[ChatEvent]:
    """Orchestrate a chat session using the OpenAI API with streaming."""
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    session_id_str = str(session_id)

    logger.info(f"Starting OpenAI chat orchestration for session {session_id_str}")

    settings = get_settings()

    try:
        client = await get_cached_openai_client()
    except Exception as e:
        logger.error(f"Failed to get OpenAI client: {e}")
        yield error_event(f"AI service unavailable: {e}")
        return

    try:
        await save_message(db, session_id, "user", user_message)
    except Exception as e:
        logger.error(f"Failed to save user message: {e}")
        yield error_event(f"Failed to save message: {e}")
        return

    try:
        title = await ensure_session_title(db, session_id, user_message)
        if title:
            logger.debug(f"Generated session title: {title}")
    except Exception as e:
        logger.warning(f"Failed to set session title: {e}")

    try:
        messages = await get_session_messages(db, session_id)
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
        yield error_event(f"Failed to load conversation history: {e}")
        return

    base_prompt = await resolve_session_system_prompt(db, session_id)
    system_prompt = get_system_prompt(cluster_name, namespaces, base_prompt=base_prompt)
    tools = _convert_tools_to_openai_format()
    converted_messages = _convert_messages_to_openai_format(messages)

    model = settings.openai_model

    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *converted_messages,
    ]

    iteration = 0
    total_input_tokens = 0
    total_output_tokens = 0
    tools_supported = True
    total_tool_calls = 0

    try:
        while iteration < MAX_ORCHESTRATION_ITERATIONS:
            iteration += 1
            logger.debug(f"OpenAI iteration {iteration}/{MAX_ORCHESTRATION_ITERATIONS}")

            if cancel_event and cancel_event.is_set():
                logger.info(f"OpenAI chat cancelled at iteration {iteration}")
                yield done_event(stopped=True)
                return

            if len(conversation) > MAX_CONVERSATION_MESSAGES + 1:
                logger.info(
                    f"Truncating conversation from {len(conversation)} "
                    f"to {MAX_CONVERSATION_MESSAGES + 1} messages"
                )
                conversation[:] = [conversation[0]] + truncate_messages(
                    conversation[1:], MAX_CONVERSATION_MESSAGES
                )

            create_kwargs: dict[str, Any] = {
                "model": model,
                "messages": conversation,
                "stream": True,
                "stream_options": {"include_usage": True},
                "max_tokens": MAX_RESPONSE_TOKENS,
            }
            if tools_supported:
                create_kwargs["tools"] = tools
                create_kwargs["tool_choice"] = "auto"

            response = None
            last_error: APIStatusError | None = None
            for attempt in range(MAX_API_RETRIES):
                try:
                    response = await client.chat.completions.create(**create_kwargs)
                    break
                except APIStatusError as e:
                    last_error = e
                    if attempt < MAX_API_RETRIES - 1 and e.status_code in (429, 500, 502, 503):
                        logger.warning(
                            f"OpenAI error (attempt {attempt + 1}/{MAX_API_RETRIES}), "
                            f"status={e.status_code}: {e.message}"
                        )
                        await asyncio.sleep(2**attempt)
                        continue
                    break

            if response is None and tools_supported:
                err_detail = (
                    f"{last_error.status_code}: {last_error.message}" if last_error else "unknown"
                )
                logger.warning(
                    f"All tool-enabled attempts failed ({err_detail}), retrying without tools"
                )
                tools_supported = False
                create_kwargs.pop("tools", None)
                create_kwargs.pop("tool_choice", None)
                try:
                    response = await client.chat.completions.create(**create_kwargs)
                except APIStatusError as e:
                    logger.error(f"Text-only fallback also failed: {e.status_code}: {e.message}")
                    yield error_event(f"AI provider error: {e.message}")
                    yield done_event()
                    return

            if response is None:
                err_msg = last_error.message if last_error else "Unknown API error"
                logger.error(f"All OpenAI attempts failed: {err_msg}")
                yield error_event(f"AI provider error: {err_msg}")
                yield done_event()
                return

            prev_output_tokens = total_output_tokens
            assistant_text = ""
            tool_calls: list[dict[str, Any]] = []

            async for chunk in response:
                if cancel_event and cancel_event.is_set():
                    logger.info(f"OpenAI chat cancelled mid-stream at iteration {iteration}")
                    yield done_event(stopped=True)
                    return

                if hasattr(chunk, "usage") and chunk.usage:
                    total_input_tokens += chunk.usage.prompt_tokens or 0
                    total_output_tokens += chunk.usage.completion_tokens or 0

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                if delta.content:
                    assistant_text += delta.content
                    yield text_event(delta.content)

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        if tc.index is not None:
                            while len(tool_calls) <= tc.index:
                                tool_calls.append(
                                    {
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                )
                            current = tool_calls[tc.index]
                            if tc.id:
                                current["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    current["function"]["name"] += tc.function.name
                                if tc.function.arguments:
                                    current["function"]["arguments"] += tc.function.arguments

            assistant_content_blocks: list[dict[str, Any]] = []
            if assistant_text:
                assistant_content_blocks.append({"type": "text", "text": assistant_text})

            tool_calls_data: list[dict[str, Any]] = []
            for tc in tool_calls:
                try:
                    tool_input = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    tool_input = {}
                tool_calls_data.append(
                    {
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": tool_input,
                    }
                )
                assistant_content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": tool_input,
                    }
                )

            if assistant_content_blocks:
                total_tool_calls += sum(
                    1 for b in assistant_content_blocks if b.get("type") == "tool_use"
                )
                iter_output = total_output_tokens - prev_output_tokens
                try:
                    await save_message(
                        db,
                        session_id,
                        "assistant",
                        assistant_content_blocks,
                        tool_calls=tool_calls_data if tool_calls_data else None,
                        token_count=iter_output or None,
                    )
                except Exception as e:
                    logger.error(f"Failed to save assistant message: {e}")

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            assistant_msg["content"] = assistant_text if assistant_text else None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            conversation.append(assistant_msg)

            if not tool_calls:
                break

            if cancel_event and cancel_event.is_set():
                logger.info(f"OpenAI chat cancelled at iteration {iteration} (before tools)")
                yield done_event(stopped=True)
                return

            # Yield tool_start events before execution
            for tc_data in tool_calls_data:
                yield tool_start_event(tc_data["name"], tc_data["input"], tc_data["id"])

            parsed_calls = [{"name": tc["name"], "input": tc["input"]} for tc in tool_calls_data]
            results = await execute_tools_parallel(
                parsed_calls,
                ToolExecContext(
                    user_id=user_id,
                    session_id=session_id,
                    kubeconfig_path=kubeconfig_path,
                    context_name=context_name,
                ),
            )

            tool_result_blocks: list[dict[str, Any]] = []
            for tc_data, result in zip(tool_calls_data, results):
                tool_name = tc_data["name"]
                tool_id = tc_data["id"]

                yield tool_result_event(
                    tool_name, result["content"], tool_id, result.get("is_error", False)
                )

                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": result["content"],
                    }
                )

                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result["content"],
                        "is_error": result.get("is_error", False),
                    }
                )

            if tool_result_blocks:
                try:
                    await save_message(db, session_id, "user", tool_result_blocks)
                except Exception as e:
                    logger.error(f"Failed to save tool results: {e}")

        if iteration >= MAX_ORCHESTRATION_ITERATIONS:
            logger.warning(
                f"Max iterations ({MAX_ORCHESTRATION_ITERATIONS}) reached "
                f"for session {session_id_str}"
            )
            yield error_event(
                f"Reached maximum iterations ({MAX_ORCHESTRATION_ITERATIONS}). "
                "The conversation has become too complex. Please start a new session."
            )

        usage_failed = False
        if total_input_tokens or total_output_tokens:
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

        meta: dict[str, Any] = {
            "iterations": iteration,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        }
        if usage_failed:
            meta["usage_tracking_failed"] = True
        yield done_event(metadata=meta)

    except Exception as e:
        logger.exception("OpenAI orchestration error")
        yield error_event(str(e))
        yield done_event(
            metadata={
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
        )
