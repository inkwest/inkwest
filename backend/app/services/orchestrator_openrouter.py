"""
OpenRouter orchestrator for Inkwest.

OpenRouter provides a unified API compatible with OpenAI's format,
allowing access to multiple AI providers (Anthropic, OpenAI, Google, etc.)
through a single endpoint.
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
from app.services.prompts import get_system_prompt
from app.tools import ToolExecContext, get_tool_definitions
from app.tools import execute_tools_parallel_ctx as execute_tools_parallel

logger = logging.getLogger(__name__)


def _is_anthropic_model(model: str) -> bool:
    """Check if the OpenRouter model routes to Anthropic (supports cache_control)."""
    return model.startswith("anthropic/") or "claude" in model.lower()


def _convert_tools_to_openai_format() -> list[dict[str, Any]]:
    """Convert Anthropic tool definitions to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in get_tool_definitions()
    ]


def _convert_messages_to_openai_format(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic-style stored messages to OpenAI chat format."""
    openai_messages: list[dict[str, Any]] = []

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            openai_messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            openai_messages.append({"role": role, "content": str(content)})
            continue

        # Tool results → tool messages
        if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
            for block in content:
                openai_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": block["content"],
                    }
                )
            continue

        # Assistant message with text + tool_use blocks
        if role == "assistant":
            text_parts = []
            tool_calls = []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block["input"]),
                            },
                        }
                    )

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            assistant_msg["content"] = "\n".join(text_parts) if text_parts else None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            openai_messages.append(assistant_msg)
            continue

        openai_messages.append({"role": role, "content": str(content)})

    return openai_messages


def get_openrouter_client() -> AsyncOpenAI:
    """Get configured OpenRouter client (OpenAI-compatible)."""
    settings = get_settings()

    if not settings.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY is required for OpenRouter provider")

    extra_headers = {
        "HTTP-Referer": settings.openrouter_site_url or settings.frontend_url,
        "X-Title": settings.openrouter_app_name,
    }

    return AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        default_headers=extra_headers,
    )


async def orchestrate_chat_openrouter(
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
    """Orchestrate a chat session using OpenRouter API (OpenAI-compatible streaming)."""
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    session_id_str = str(session_id)

    logger.info(f"Starting OpenRouter chat orchestration for session {session_id_str}")

    settings = get_settings()

    try:
        client = get_openrouter_client()
    except Exception as e:
        logger.error(f"Failed to get OpenRouter client: {e}")
        yield error_event(f"AI service unavailable: {e}")
        return

    # Save user message
    try:
        await save_message(db, session_id, "user", user_message)
    except Exception as e:
        logger.error(f"Failed to save user message: {e}")
        yield error_event(f"Failed to save message: {e}")
        return

    # Auto-generate session title for first message
    try:
        title = await ensure_session_title(db, session_id, user_message)
        if title:
            logger.debug(f"Generated session title: {title}")
    except Exception as e:
        logger.warning(f"Failed to set session title: {e}")

    # Load existing messages from DB (includes the just-saved user message)
    try:
        messages = await get_session_messages(db, session_id)
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
        yield error_event(f"Failed to load conversation history: {e}")
        return

    # Build OpenAI-format conversation
    system_prompt = get_system_prompt(cluster_name, namespaces)
    tools = _convert_tools_to_openai_format()
    converted_messages = _convert_messages_to_openai_format(messages)

    model = settings.openrouter_model
    use_cache_control = _is_anthropic_model(model)

    # Anthropic models via OpenRouter support cache_control on system + messages
    if use_cache_control:
        system_msg: dict[str, Any] = {
            "role": "system",
            "content": [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            ],
        }
    else:
        system_msg = {"role": "system", "content": system_prompt}

    conversation: list[dict[str, Any]] = [system_msg, *converted_messages]

    iteration = 0
    total_input_tokens = 0
    total_output_tokens = 0
    tools_supported = True

    # Collect content for DB persistence
    collected_assistant_content: list[dict[str, Any]] = []

    try:
        while iteration < MAX_ORCHESTRATION_ITERATIONS:
            iteration += 1
            logger.debug(f"OpenRouter iteration {iteration}/{MAX_ORCHESTRATION_ITERATIONS}")

            if cancel_event and cancel_event.is_set():
                logger.info(f"OpenRouter chat cancelled at iteration {iteration}")
                yield done_event(stopped=True)
                return

            # Truncate long conversations to avoid token limits
            if len(conversation) > MAX_CONVERSATION_MESSAGES + 1:
                logger.info(
                    f"Truncating conversation from {len(conversation)} "
                    f"to {MAX_CONVERSATION_MESSAGES + 1} messages"
                )
                conversation[:] = [conversation[0]] + truncate_messages(
                    conversation[1:], MAX_CONVERSATION_MESSAGES
                )

            # Mark last user/tool message for Anthropic prompt caching
            if use_cache_control and len(conversation) > 1:
                last_msg = conversation[-1]
                content = last_msg.get("content")
                if isinstance(content, str) and content:
                    conversation[-1] = {
                        **last_msg,
                        "content": [
                            {
                                "type": "text",
                                "text": content,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }

            ignored_providers: list[str] = []

            def _build_create_kwargs() -> dict[str, Any]:
                provider_opts: dict[str, Any] = {"allow_fallbacks": True}
                if tools_supported:
                    provider_opts["require_parameters"] = True
                if ignored_providers:
                    provider_opts["ignore"] = ignored_providers
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": conversation,
                    "stream": True,
                    "max_tokens": MAX_RESPONSE_TOKENS,
                    "extra_body": {"provider": provider_opts},
                }
                if tools_supported:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                return kwargs

            # Try with tools first, fall back to text-only if no provider supports them
            response = None
            last_error: APIStatusError | None = None
            for attempt in range(MAX_API_RETRIES):
                try:
                    response = await client.chat.completions.create(**_build_create_kwargs())
                    break
                except APIStatusError as e:
                    last_error = e
                    body = getattr(e, "body", None)
                    failed_provider = None
                    if isinstance(body, dict):
                        failed_provider = (
                            body.get("error", {}).get("metadata", {}).get("provider_name")
                        )
                    if failed_provider:
                        ignored_providers.append(failed_provider)
                    if attempt < MAX_API_RETRIES - 1 and e.status_code in (404, 429, 502, 503):
                        logger.warning(
                            f"OpenRouter error (attempt {attempt + 1}/{MAX_API_RETRIES}), "
                            f"status={e.status_code}, provider={failed_provider}: {e.message}"
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
                try:
                    response = await client.chat.completions.create(**_build_create_kwargs())
                except APIStatusError as e:
                    logger.error(f"Text-only fallback also failed: {e.status_code}: {e.message}")
                    yield error_event(f"AI provider error: {e.message}")
                    yield done_event()
                    return

            if response is None:
                err_msg = last_error.message if last_error else "Unknown API error"
                logger.error(f"All OpenRouter attempts failed: {err_msg}")
                yield error_event(f"AI provider error: {err_msg}")
                yield done_event()
                return

            prev_output_tokens = total_output_tokens
            assistant_text = ""
            tool_calls: list[dict[str, Any]] = []

            async for chunk in response:
                if cancel_event and cancel_event.is_set():
                    logger.info(f"OpenRouter chat cancelled mid-stream at iteration {iteration}")
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

            # Build assistant content for DB storage (Anthropic format)
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
                collected_assistant_content = assistant_content_blocks
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

            # Add assistant message to OpenAI conversation
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            assistant_msg["content"] = assistant_text if assistant_text else None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            conversation.append(assistant_msg)

            # No tool calls → done
            if not tool_calls:
                break

            # Execute tool calls
            parsed_calls = []
            for tc_data in tool_calls_data:
                parsed_calls.append({"name": tc_data["name"], "input": tc_data["input"]})

            results = await execute_tools_parallel(
                parsed_calls,
                ToolExecContext(
                    user_id=user_id,
                    session_id=session_id,
                    kubeconfig_path=kubeconfig_path,
                    context_name=context_name,
                ),
            )

            # Yield tool events + build tool results for conversation & DB
            tool_result_blocks: list[dict[str, Any]] = []
            for tc_data, result in zip(tool_calls_data, results):
                tool_name = tc_data["name"]
                tool_id = tc_data["id"]

                yield tool_start_event(tool_name, tc_data["input"], tool_id)
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

            # Save tool results to DB (stored as user message, Anthropic convention)
            if tool_result_blocks:
                try:
                    await save_message(db, session_id, "user", tool_result_blocks)
                except Exception as e:
                    logger.error(f"Failed to save tool results: {e}")

        # Persist session stats
        if total_input_tokens or total_output_tokens:
            try:
                tool_use_count = sum(
                    1 for b in collected_assistant_content if b.get("type") == "tool_use"
                )
                await update_session_stats(
                    db,
                    session_id,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    tool_calls=tool_use_count,
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

        yield done_event(
            metadata={
                "iterations": iteration,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
        )

    except Exception as e:
        logger.exception("OpenRouter orchestration error")
        yield error_event(str(e))
        yield done_event(
            metadata={
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
        )
