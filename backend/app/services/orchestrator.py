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
from app.services.ai_client import get_ai_client, resolve_session_model
from app.services.events import (
    ChatEvent,
    done_event,
    error_event,
    text_event,
    thinking_event,
)
from app.services.messages import (
    ensure_session_title,
    get_session_messages,
    save_message,
    truncate_messages,
)
from app.services.orchestrator_common import (
    ToolRunResult,
    execute_tool_calls,
    finalize_turn,
)
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.tools.definitions import get_tool_definitions

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
    persist_user_message: bool = True,
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
        persist_user_message: When False (retry), the user message is already the
            trailing row in history — don't append/re-save it or it would duplicate.

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

    # Add new user message (skipped on retry — already the trailing history row)
    if persist_user_message:
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
    model = await resolve_session_model(db, session_id)

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
                model=model,
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

        # Execute tools if requested
        if response.stop_reason == "tool_use" and tool_calls:
            if cancel_event and cancel_event.is_set():
                logger.info(f"Chat cancelled at iteration {iteration} (before tool execution)")
                yield done_event(stopped=True)
                return

            # Add assistant message to conversation
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

            # Tool results go back as role="user" per Anthropic API convention.
            messages.append({"role": "user", "content": run.tool_results})
            try:
                await save_message(db, session_id, "tool_result", run.tool_results)
            except Exception as e:
                logger.error(f"Failed to save tool results: {e}")  # streamed already; continue
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
