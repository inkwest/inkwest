"""
AI Chat Orchestrator for Inkwest.

This module implements the core tool-use loop that:
1. Loads conversation history from the database
2. Calls Claude API with kubectl tools
3. Handles tool use requests (stop_reason: "tool_use")
4. Executes tools and feeds results back to Claude
5. Continues until Claude finishes (stop_reason: "end_turn")
6. Yields ChatEvent objects for streaming to frontend
7. Saves all messages to the database
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic, RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    MAX_API_RETRIES,
    MAX_CONVERSATION_MESSAGES,
    MAX_ORCHESTRATION_ITERATIONS,
    MAX_RESPONSE_TOKENS,
)
from app.core.retry import retry_with_backoff
from app.services.ai_client import get_ai_client, get_model_name
from app.services.budget_service import record_usage
from app.services.events import (
    ChatEvent,
    connector_required_event,
    done_event,
    error_event,
    text_event,
    thinking_event,
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
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.tools.connector_exec import connector_for_tool, is_needs_auth
from app.tools.definitions import get_tool_definitions
from app.tools.dispatch import execute_tool
from app.tools.exec_context import ToolExecContext

logger = logging.getLogger(__name__)


def _is_non_retryable_api_error(e: Exception) -> bool:
    """Check if an API error should not be retried (4xx errors except rate limits)."""
    # RateLimitError is a subclass of APIStatusError with 429, but should be retried
    if isinstance(e, RateLimitError):
        return False
    if isinstance(e, APIStatusError) and e.status_code < 500:
        return True
    return False


async def call_claude_with_retry(
    client: AsyncAnthropic, max_retries: int = MAX_API_RETRIES, **kwargs: Any
) -> Any:
    """
    Call Claude API with exponential backoff retry.

    Handles rate limits and connection errors gracefully.

    Args:
        client: AsyncAnthropic client
        max_retries: Maximum number of retry attempts
        **kwargs: Arguments to pass to client.messages.create

    Returns:
        Claude API response

    Raises:
        RateLimitError: If rate limit exceeded after all retries
        APIConnectionError: If connection failed after all retries
        APIStatusError: For other API errors
    """

    async def _call() -> Any:
        return await client.messages.create(**kwargs)

    return await retry_with_backoff(
        operation=_call,
        max_retries=max_retries,
        retryable_exceptions=(RateLimitError, APIConnectionError, APIStatusError),
        non_retryable_check=_is_non_retryable_api_error,
        operation_name="Claude API call",
    )


async def orchestrate_chat(
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
    """
    Main orchestration loop.

    Yields events as they occur for streaming to frontend.

    Args:
        db: Database session
        session_id: UUID of the chat session
        cluster_name: Name of the Kubernetes cluster
        kubeconfig_path: Path to kubeconfig file
        context_name: Optional kubectl context name
        user_message: The user's message to process
        namespaces: Optional list of namespaces for context
        cancel_event: Optional event to signal cancellation

    Yields:
        ChatEvent objects for each step of the orchestration
    """
    if isinstance(session_id, str):
        session_id_str = session_id
        session_id = uuid.UUID(session_id)
    else:
        session_id_str = str(session_id)

    logger.info(f"Starting chat orchestration for session {session_id_str}")

    try:
        client = get_ai_client()
    except Exception as e:
        logger.error(f"Failed to get AI client: {e}")
        yield error_event(f"AI service unavailable: {str(e)}")
        return

    # Load existing messages
    try:
        messages = await get_session_messages(db, session_id)
        logger.debug(f"Loaded {len(messages)} existing messages")
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
        yield error_event(f"Failed to load conversation history: {str(e)}")
        return

    # Add new user message
    messages.append({"role": "user", "content": user_message})

    # Save user message to database
    try:
        await save_message(db, session_id, "user", user_message)
    except Exception as e:
        logger.error(f"Failed to save user message: {e}")
        yield error_event(f"Failed to save message: {str(e)}")
        return

    # Auto-generate session title from first message
    if len(messages) == 1:
        try:
            title = await ensure_session_title(db, session_id, user_message)
            if title:
                logger.debug(f"Generated session title: {title}")
        except Exception as e:
            logger.warning(f"Failed to set session title: {e}")
            # Non-critical, continue

    base_prompt = await resolve_session_system_prompt(db, session_id)
    system_prompt = get_system_prompt(cluster_name, namespaces, base_prompt=base_prompt)
    model = get_model_name()

    # Prompt caching: cache system prompt and tool definitions across iterations
    cached_system = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]
    cached_tools = list(get_tool_definitions())
    cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

    # Session stats tracking
    total_input_tokens = 0
    total_output_tokens = 0
    total_tool_calls = 0
    iteration = 0

    while iteration < MAX_ORCHESTRATION_ITERATIONS:
        iteration += 1
        logger.debug(f"Orchestration iteration {iteration}")

        if cancel_event and cancel_event.is_set():
            logger.info(f"Chat cancelled at iteration {iteration} (before API call)")
            yield done_event(stopped=True)
            return

        if len(messages) > MAX_CONVERSATION_MESSAGES:
            logger.info(
                f"Truncating conversation from {len(messages)} "
                f"to {MAX_CONVERSATION_MESSAGES} messages"
            )
            messages = truncate_messages(messages, MAX_CONVERSATION_MESSAGES)

        # Track timing for this iteration
        iter_start = time.monotonic()

        # Mark last message content block for caching (caches conversation prefix)
        if messages and isinstance(messages[-1].get("content"), list):
            blocks = messages[-1]["content"]
            if blocks:
                blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}

        try:
            # Build API call kwargs
            api_kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": MAX_RESPONSE_TOKENS,
                "system": cached_system,
                "messages": messages,
                "tools": cached_tools,
            }

            # Enable extended thinking for supported models (Claude 3.5+)
            # Extended thinking provides visibility into AI reasoning
            if "claude-3" in model or "claude-sonnet" in model or "claude-opus" in model:
                api_kwargs["metadata"] = {"user_id": session_id_str}

            # Call Claude with retry logic
            response = await call_claude_with_retry(client, **api_kwargs)

            # Track timing
            iter_latency_ms = int((time.monotonic() - iter_start) * 1000)

        except RateLimitError:
            yield error_event("AI service is currently rate limited. Please try again in a moment.")
            return
        except APIConnectionError:
            yield error_event("Unable to connect to AI service. Please check your connection.")
            return
        except APIStatusError as e:
            yield error_event(f"AI service error: {e.message}")
            return
        except Exception as e:
            logger.exception("Unexpected error calling Claude API")
            yield error_event(f"AI error: {str(e)}")
            return

        if cancel_event and cancel_event.is_set():
            logger.info(f"Chat cancelled at iteration {iteration} (after API call)")
            yield done_event(stopped=True)
            return

        # Track token usage for this iteration
        iter_input_tokens = response.usage.input_tokens if response.usage else 0
        iter_output_tokens = response.usage.output_tokens if response.usage else 0
        total_input_tokens += iter_input_tokens
        total_output_tokens += iter_output_tokens

        # Process response content
        assistant_content = []
        tool_calls = []
        text_content = []
        thinking_content = None

        for block in response.content:
            # Handle extended thinking blocks (Claude 3.5+)
            if block.type == "thinking":
                thinking_content = block.thinking
                yield thinking_event(block.thinking)
                assistant_content.append({"type": "thinking", "thinking": block.thinking})

            elif block.type == "text":
                # Yield text to frontend immediately
                yield text_event(block.text)
                text_content.append(block.text)
                assistant_content.append({"type": "text", "text": block.text})

            elif block.type == "tool_use":
                tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
                assistant_content.append(
                    {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
                )
                total_tool_calls += 1

        # Save assistant message with full stats
        try:
            await save_message(
                db,
                session_id,
                "assistant",
                assistant_content,
                tool_calls=tool_calls if tool_calls else None,
                input_tokens=iter_input_tokens,
                output_tokens=iter_output_tokens,
                thinking_content=thinking_content,
                latency_ms=iter_latency_ms,
            )
        except Exception as e:
            logger.error(f"Failed to save assistant message: {e}")
            # Continue anyway - message was already streamed

        # Check if done
        if response.stop_reason == "end_turn":
            logger.info(
                f"Chat completed after {iteration} iterations, "
                f"tokens: {total_input_tokens}/{total_output_tokens}, "
                f"tool_calls: {total_tool_calls}"
            )

            # Update session stats and record usage
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

            meta: dict[str, Any] = {
                "iterations": iteration,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "tool_calls": total_tool_calls,
                "latency_ms": iter_latency_ms,
            }
            if usage_failed:
                meta["usage_tracking_failed"] = True
            yield done_event(metadata=meta)
            return

        # Execute tools if requested
        if response.stop_reason == "tool_use" and tool_calls:
            if cancel_event and cancel_event.is_set():
                logger.info(f"Chat cancelled at iteration {iteration} (before tool execution)")
                yield done_event(stopped=True)
                return

            # Add assistant message to conversation
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []

            for tool_call in tool_calls:
                if cancel_event and cancel_event.is_set():
                    logger.info(f"Chat cancelled during tool execution at iteration {iteration}")
                    yield done_event(stopped=True)
                    return

                tool_id = tool_call["id"]
                tool_name = tool_call["name"]
                tool_input = tool_call["input"]

                logger.debug(f"Executing tool: {tool_name}")

                # Notify frontend that tool is starting
                yield tool_start_event(tool_name, tool_input, tool_id)

                # Execute the tool (kubectl or a connector tool). Connector tools
                # fetch the user's temporary credential from the RAM store by
                # (user_id, session_id) — never via the model or tool input.
                tool_ctx = ToolExecContext(
                    user_id=user_id,
                    session_id=session_id,
                    kubeconfig_path=kubeconfig_path,
                    context_name=context_name,
                )
                try:
                    output, is_error = await execute_tool(tool_name, tool_input, tool_ctx)
                except Exception as e:
                    logger.exception(f"Exception executing tool {tool_name}")
                    output = f"Error executing tool: {str(e)}"
                    is_error = True

                # Notify frontend of result
                yield tool_result_event(tool_name, output, tool_id, is_error)

                # Surface a first-class re-auth affordance when a connector is missing
                connector = connector_for_tool(tool_name)
                if connector and is_needs_auth(output, is_error):
                    yield connector_required_event(connector, session_id_str)

                # Format for Claude API
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": output,
                        "is_error": is_error,
                    }
                )

            # Add tool results to conversation (role="user" per Anthropic API convention)
            messages.append({"role": "user", "content": tool_results})

            try:
                await save_message(db, session_id, "tool_result", tool_results)
            except Exception as e:
                logger.error(f"Failed to save tool results: {e}")
                # Continue anyway

            # Continue loop for next Claude response
            continue

        # Unexpected stop reason
        logger.warning(f"Unexpected stop_reason: {response.stop_reason}")
        yield done_event()
        return

    # Max iterations reached
    logger.warning(
        f"Max iterations ({MAX_ORCHESTRATION_ITERATIONS}) reached for session {session_id_str}"
    )
    yield error_event(
        f"Reached maximum iterations ({MAX_ORCHESTRATION_ITERATIONS}). "
        "The conversation has become too complex. Please start a new session."
    )


async def orchestrate_chat_streaming(
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
    """
    Streaming variant of orchestrate_chat.

    Uses Claude's streaming API to stream text token by token.
    This provides a more responsive experience for longer responses.

    Args:
        db: Database session
        session_id: UUID of the chat session
        cluster_name: Name of the Kubernetes cluster
        kubeconfig_path: Path to kubeconfig file
        context_name: Optional kubectl context name
        user_message: The user's message to process
        namespaces: Optional list of namespaces for context
        cancel_event: Optional event to signal cancellation

    Yields:
        ChatEvent objects for each token/event
    """
    if isinstance(session_id, str):
        session_id_str = session_id
        session_id = uuid.UUID(session_id)
    else:
        session_id_str = str(session_id)

    logger.info(f"Starting streaming chat orchestration for session {session_id_str}")

    try:
        client = get_ai_client()
    except Exception as e:
        logger.error(f"Failed to get AI client: {e}")
        yield error_event(f"AI service unavailable: {str(e)}")
        return

    # Load existing messages
    try:
        messages = await get_session_messages(db, session_id)
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
        yield error_event(f"Failed to load conversation history: {str(e)}")
        return

    # Add new user message
    messages.append({"role": "user", "content": user_message})

    # Save user message
    try:
        await save_message(db, session_id, "user", user_message)
    except Exception as e:
        logger.error(f"Failed to save user message: {e}")
        yield error_event(f"Failed to save message: {str(e)}")
        return

    # Auto-generate session title
    if len(messages) == 1:
        try:
            await ensure_session_title(db, session_id, user_message)
        except Exception as e:
            logger.warning(f"Failed to set session title: {e}")

    base_prompt = await resolve_session_system_prompt(db, session_id)
    system_prompt = get_system_prompt(cluster_name, namespaces, base_prompt=base_prompt)
    model = get_model_name()

    cached_system = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]
    cached_tools = list(get_tool_definitions())
    cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

    total_input_tokens = 0
    total_output_tokens = 0
    total_tool_calls = 0
    iteration = 0

    while iteration < MAX_ORCHESTRATION_ITERATIONS:
        iteration += 1

        if cancel_event and cancel_event.is_set():
            logger.info(f"Streaming chat cancelled at iteration {iteration} (before API call)")
            yield done_event(stopped=True)
            return

        if len(messages) > MAX_CONVERSATION_MESSAGES:
            logger.info(
                f"Truncating conversation from {len(messages)} "
                f"to {MAX_CONVERSATION_MESSAGES} messages"
            )
            messages = truncate_messages(messages, MAX_CONVERSATION_MESSAGES)

        if messages and isinstance(messages[-1].get("content"), list):
            blocks = messages[-1]["content"]
            if blocks:
                blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}

        try:
            # Use streaming API
            async with client.messages.stream(
                model=model,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=cached_system,
                messages=messages,
                tools=cached_tools,
            ) as stream:
                assistant_content = []
                tool_calls = []
                current_text = ""
                current_tool_use = None

                async for event in stream:
                    if cancel_event and cancel_event.is_set():
                        logger.info(f"Streaming chat cancelled mid-stream at iteration {iteration}")
                        break

                    if event.type == "content_block_start":
                        if event.content_block.type == "text":
                            current_text = ""
                        elif event.content_block.type == "tool_use":
                            current_tool_use = {
                                "id": event.content_block.id,
                                "name": event.content_block.name,
                                "input": {},
                            }

                    elif event.type == "content_block_delta":
                        if hasattr(event.delta, "text"):
                            # Stream text delta immediately
                            yield text_event(event.delta.text)
                            current_text += event.delta.text
                        elif hasattr(event.delta, "partial_json"):
                            # Accumulate tool input JSON
                            pass  # Handle in content_block_stop

                    elif event.type == "content_block_stop":
                        if current_text:
                            assistant_content.append({"type": "text", "text": current_text})
                            current_text = ""
                        if current_tool_use:
                            # Get final tool input from stream
                            tool_calls.append(current_tool_use)
                            assistant_content.append(
                                {
                                    "type": "tool_use",
                                    "id": current_tool_use["id"],
                                    "name": current_tool_use["name"],
                                    "input": current_tool_use["input"],
                                }
                            )
                            current_tool_use = None

                if cancel_event and cancel_event.is_set():
                    yield done_event(stopped=True)
                    return

                # Get final response for stop_reason and usage
                response = await stream.get_final_message()

                # Update tool calls with final input
                for i, block in enumerate(response.content):
                    if block.type == "tool_use":
                        # Find matching tool call and update input
                        for tc in tool_calls:
                            if tc["id"] == block.id:
                                tc["input"] = block.input
                                # Update in assistant_content too
                                for ac in assistant_content:
                                    if ac.get("id") == block.id:
                                        ac["input"] = block.input
                                break

        except RateLimitError:
            yield error_event("AI service is currently rate limited.")
            return
        except APIConnectionError:
            yield error_event("Unable to connect to AI service.")
            return
        except Exception as e:
            logger.exception("Error in streaming API call")
            yield error_event(f"AI error: {str(e)}")
            return

        # Save assistant message
        try:
            await save_message(
                db,
                session_id,
                "assistant",
                assistant_content,
                tool_calls=tool_calls if tool_calls else None,
                token_count=response.usage.output_tokens if response.usage else None,
            )
        except Exception as e:
            logger.error(f"Failed to save assistant message: {e}")

        # Accumulate token usage across iterations
        iter_input = response.usage.input_tokens if response.usage else 0
        iter_output = response.usage.output_tokens if response.usage else 0
        total_input_tokens += iter_input
        total_output_tokens += iter_output

        # Check if done
        if response.stop_reason == "end_turn":
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

            meta: dict[str, Any] = {
                "iterations": iteration,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
            if usage_failed:
                meta["usage_tracking_failed"] = True
            yield done_event(metadata=meta)
            return

        # Execute tools
        if response.stop_reason == "tool_use" and tool_calls:
            if cancel_event and cancel_event.is_set():
                logger.info(f"Streaming chat cancelled at iteration {iteration} (before tools)")
                yield done_event(stopped=True)
                return

            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            total_tool_calls += len(tool_calls)

            for tool_call in tool_calls:
                if cancel_event and cancel_event.is_set():
                    logger.info(
                        f"Streaming chat cancelled during tool execution at iteration {iteration}"
                    )
                    yield done_event(stopped=True)
                    return

                tool_id = tool_call["id"]
                tool_name = tool_call["name"]
                tool_input = tool_call["input"]

                yield tool_start_event(tool_name, tool_input, tool_id)

                tool_ctx = ToolExecContext(
                    user_id=user_id,
                    session_id=session_id,
                    kubeconfig_path=kubeconfig_path,
                    context_name=context_name,
                )
                try:
                    output, is_error = await execute_tool(tool_name, tool_input, tool_ctx)
                except Exception as e:
                    output = f"Error: {str(e)}"
                    is_error = True

                yield tool_result_event(tool_name, output, tool_id, is_error)

                connector = connector_for_tool(str(tool_name))
                if connector and is_needs_auth(output, is_error):
                    yield connector_required_event(connector, session_id_str)

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": output,
                        "is_error": is_error,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

            try:
                await save_message(db, session_id, "tool_result", tool_results)
            except Exception as e:
                logger.error(f"Failed to save tool results: {e}")

            continue

        yield done_event()
        return

    yield error_event(f"Reached maximum iterations ({MAX_ORCHESTRATION_ITERATIONS}).")
