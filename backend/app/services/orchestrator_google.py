"""
Google Gemini orchestrator for Inkwest.

Uses the google-genai SDK for direct Gemini API access with streaming,
function calling, and implicit prompt caching.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import (
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


def get_google_client() -> genai.Client:
    """Get configured Google GenAI client."""
    settings = get_settings()
    if not settings.google_api_key:
        raise ValueError("GOOGLE_API_KEY is required for Google provider")
    return genai.Client(api_key=settings.google_api_key)


def _build_gemini_tools() -> Sequence[types.Tool]:
    """Convert KUBECTL_TOOLS to Gemini function declarations."""
    declarations = []
    for tool in get_tool_definitions():
        schema = tool["input_schema"].copy()
        # Gemini doesn't accept top-level 'required' alongside 'properties' in the same way;
        # pass it through as-is since google-genai SDK handles conversion
        declarations.append(
            types.FunctionDeclaration(
                name=tool["name"],
                description=tool["description"],
                parameters=schema,
            )
        )
    return [types.Tool(function_declarations=declarations)]


def _convert_messages_to_gemini(messages: list[dict[str, Any]]) -> list[types.Content]:
    """Convert Anthropic-style stored messages to Gemini Content objects."""
    contents: list[types.Content] = []

    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        # Gemini uses "user" and "model" roles
        gemini_role = "model" if role == "assistant" else "user"

        if isinstance(content, str):
            contents.append(
                types.Content(role=gemini_role, parts=[types.Part.from_text(text=content)])
            )
            continue

        if not isinstance(content, list):
            contents.append(
                types.Content(role=gemini_role, parts=[types.Part.from_text(text=str(content))])
            )
            continue

        parts: list[types.Part] = []

        # Tool results → function_response parts (always role=user in Gemini)
        if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
            for block in content:
                parts.append(
                    types.Part.from_function_response(
                        name=block.get("tool_name", "unknown"),
                        response={
                            "content": block["content"],
                            "is_error": block.get("is_error", False),
                        },
                    )
                )
            contents.append(types.Content(role="user", parts=parts))
            continue

        # Assistant messages with text + tool_use blocks
        if role == "assistant":
            for block in content:
                if block.get("type") == "text" and block.get("text"):
                    parts.append(types.Part.from_text(text=block["text"]))
                elif block.get("type") == "tool_use":
                    parts.append(
                        types.Part.from_function_call(
                            name=block["name"],
                            args=block.get("input", {}),
                        )
                    )
            if parts:
                contents.append(types.Content(role="model", parts=parts))
            continue

        # Fallback
        contents.append(
            types.Content(role=gemini_role, parts=[types.Part.from_text(text=str(content))])
        )

    return contents


async def orchestrate_chat_google(
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
    """Orchestrate a chat session using Google Gemini API with function calling."""
    if isinstance(session_id, str):
        session_id = uuid.UUID(session_id)
    session_id_str = str(session_id)

    logger.info(f"Starting Google Gemini chat orchestration for session {session_id_str}")

    settings = get_settings()

    try:
        client = get_google_client()
    except Exception as e:
        logger.error(f"Failed to get Google client: {e}")
        yield error_event(f"AI service unavailable: {e}")
        return

    # Save user message
    try:
        await save_message(db, session_id, "user", user_message)
    except Exception as e:
        logger.error(f"Failed to save user message: {e}")
        yield error_event(f"Failed to save message: {e}")
        return

    # Auto-generate session title
    try:
        title = await ensure_session_title(db, session_id, user_message)
        if title:
            logger.debug(f"Generated session title: {title}")
    except Exception as e:
        logger.warning(f"Failed to set session title: {e}")

    # Load existing messages (includes just-saved user message)
    try:
        messages = await get_session_messages(db, session_id)
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
        yield error_event(f"Failed to load conversation history: {e}")
        return

    system_prompt = get_system_prompt(cluster_name, namespaces)
    gemini_tools = _build_gemini_tools()
    model = settings.google_model

    iteration = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_tool_calls = 0

    try:
        while iteration < MAX_ORCHESTRATION_ITERATIONS:
            iteration += 1
            logger.debug(f"Google Gemini iteration {iteration}/{MAX_ORCHESTRATION_ITERATIONS}")

            if cancel_event and cancel_event.is_set():
                logger.info(f"Google chat cancelled at iteration {iteration}")
                yield done_event(stopped=True)
                return

            if len(messages) > MAX_CONVERSATION_MESSAGES:
                logger.info(
                    f"Truncating conversation from {len(messages)} "
                    f"to {MAX_CONVERSATION_MESSAGES} messages"
                )
                messages = truncate_messages(messages, MAX_CONVERSATION_MESSAGES)
            conversation = _convert_messages_to_gemini(messages)

            iter_start = time.monotonic()

            try:
                # Synchronous streaming call — run in thread to avoid blocking
                response = await asyncio.to_thread(
                    client.models.generate_content,
                    model=model,
                    contents=conversation,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=gemini_tools,
                        temperature=0,
                        max_output_tokens=MAX_RESPONSE_TOKENS,
                    ),
                )
            except Exception as e:
                logger.exception("Error calling Google Gemini API")
                yield error_event(f"AI error: {e}")
                return

            iter_latency_ms = int((time.monotonic() - iter_start) * 1000)

            if cancel_event and cancel_event.is_set():
                logger.info(f"Google chat cancelled at iteration {iteration} (after API call)")
                yield done_event(stopped=True)
                return

            # Track token usage
            if response.usage_metadata:
                iter_input = response.usage_metadata.prompt_token_count or 0
                iter_output = response.usage_metadata.candidates_token_count or 0
                total_input_tokens += iter_input
                total_output_tokens += iter_output

            # Process response parts
            assistant_text = ""
            tool_calls_data: list[dict[str, Any]] = []
            assistant_content_blocks: list[dict[str, Any]] = []

            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts or []:
                    if part.text:
                        assistant_text += part.text
                        yield text_event(part.text)
                        assistant_content_blocks.append({"type": "text", "text": part.text})

                    elif part.function_call:
                        fc = part.function_call
                        tool_id = f"google_{uuid.uuid4().hex[:12]}"
                        args = dict(fc.args) if fc.args else {}
                        tool_calls_data.append(
                            {
                                "id": tool_id,
                                "name": fc.name,
                                "input": args,
                            }
                        )
                        assistant_content_blocks.append(
                            {
                                "type": "tool_use",
                                "id": tool_id,
                                "name": fc.name,
                                "input": args,
                            }
                        )
                        total_tool_calls += 1

            # Save assistant message
            if assistant_content_blocks:
                try:
                    await save_message(
                        db,
                        session_id,
                        "assistant",
                        assistant_content_blocks,
                        tool_calls=tool_calls_data if tool_calls_data else None,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        latency_ms=iter_latency_ms,
                    )
                except Exception as e:
                    logger.error(f"Failed to save assistant message: {e}")

            # No tool calls → done
            if not tool_calls_data:
                break

            # Add assistant message to stored conversation
            messages.append({"role": "assistant", "content": assistant_content_blocks})

            # Execute tool calls
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

            # Yield tool events and build tool result blocks
            tool_result_blocks: list[dict[str, Any]] = []
            for tc_data, result in zip(tool_calls_data, results):
                tool_name = tc_data["name"]
                tool_id = tc_data["id"]

                yield tool_start_event(tool_name, tc_data["input"], tool_id)
                yield tool_result_event(
                    tool_name, result["content"], tool_id, result.get("is_error", False)
                )

                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "tool_name": tool_name,
                        "content": result["content"],
                        "is_error": result.get("is_error", False),
                    }
                )

            # Add tool results to stored conversation
            messages.append({"role": "user", "content": tool_result_blocks})

            # Save tool results to DB
            try:
                await save_message(db, session_id, "user", tool_result_blocks)
            except Exception as e:
                logger.error(f"Failed to save tool results: {e}")

        # Persist session stats
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

        yield done_event(
            metadata={
                "iterations": iteration,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "tool_calls": total_tool_calls,
            }
        )

    except Exception as e:
        logger.exception("Google Gemini orchestration error")
        yield error_event(str(e))
        yield done_event(
            metadata={
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
        )
