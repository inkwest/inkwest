"""
Azure OpenAI Chat Orchestrator for Inkwest.

This module implements the tool-use loop for Azure OpenAI / GPT models.
Handles the OpenAI-style API format which differs from Anthropic's format.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    AsyncAzureOpenAI,
    RateLimitError,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import (
    MAX_API_RETRIES,
    MAX_CONVERSATION_MESSAGES,
    MAX_ORCHESTRATION_ITERATIONS,
    MAX_RESPONSE_TOKENS,
)
from app.core.retry import retry_with_backoff
from app.services.ai_client import get_azure_client
from app.services.budget_service import record_usage
from app.services.events import (
    ChatEvent,
    connector_required_event,
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
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.tools.connector_exec import connector_for_tool, is_needs_auth
from app.tools.definitions import get_tool_definitions
from app.tools.dispatch import execute_tool
from app.tools.exec_context import ToolExecContext

logger = logging.getLogger(__name__)


def convert_tools_to_openai_format(anthropic_tools: list[dict]) -> list[dict]:
    """
    Convert Anthropic-style tool definitions to OpenAI format.

    Anthropic: { name, description, input_schema: { type, properties, required } }
    OpenAI:    { type: "function", function: { name, description, parameters } }
    """
    openai_tools = []
    for tool in anthropic_tools:
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
        )
    return openai_tools


def convert_messages_to_openai_format(messages: list[dict]) -> list[dict]:
    """
    Convert Anthropic-style messages to OpenAI format.

    Handles:
    - Simple text messages
    - Tool use blocks (assistant)
    - Tool results (user -> tool role)
    """
    openai_messages = []

    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        # Simple string content
        if isinstance(content, str):
            openai_messages.append({"role": role, "content": content})
            continue

        # Complex content (list of blocks)
        if isinstance(content, list):
            # Check if it's tool results
            if content and isinstance(content[0], dict) and content[0].get("type") == "tool_result":
                # Tool results become tool messages
                for block in content:
                    openai_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block["tool_use_id"],
                            "content": block["content"],
                        }
                    )
                continue

            # Assistant message with potential tool calls
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
                if text_parts:
                    assistant_msg["content"] = "\n".join(text_parts)
                else:
                    assistant_msg["content"] = None
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls

                openai_messages.append(assistant_msg)
                continue

        # Fallback for other formats
        openai_messages.append({"role": role, "content": str(content)})

    return openai_messages


def _is_non_retryable_azure_error(e: Exception) -> bool:
    """Check if an Azure API error should not be retried (4xx errors except rate limits)."""
    # RateLimitError is a subclass of APIStatusError with 429, but should be retried
    if isinstance(e, RateLimitError):
        return False
    if isinstance(e, APIStatusError) and e.status_code < 500:
        return True
    return False


async def call_azure_with_retry(
    client: AsyncAzureOpenAI, deployment: str, max_retries: int = MAX_API_RETRIES, **kwargs: Any
) -> Any:
    """
    Call Azure OpenAI API with exponential backoff retry.

    Args:
        client: AsyncAzureOpenAI client
        deployment: Azure deployment name
        max_retries: Maximum number of retry attempts
        **kwargs: Arguments to pass to client.chat.completions.create

    Returns:
        Azure OpenAI API response

    Raises:
        RateLimitError: If rate limit exceeded after all retries
        APIConnectionError: If connection failed after all retries
        APIStatusError: For other API errors
    """

    async def _call() -> Any:
        return await client.chat.completions.create(model=deployment, **kwargs)

    return await retry_with_backoff(
        operation=_call,
        max_retries=max_retries,
        retryable_exceptions=(RateLimitError, APIConnectionError, APIStatusError),
        non_retryable_check=_is_non_retryable_azure_error,
        operation_name="Azure OpenAI API call",
    )


async def orchestrate_chat_azure(
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
    Main orchestration loop for Azure OpenAI.

    Yields events as they occur for streaming to frontend.

    Args:
        db: Database session
        session_id: UUID of the chat session
        cluster_name: Name of the Kubernetes cluster
        kubeconfig_path: Path to kubeconfig file
        context_name: Optional kubectl context name
        user_message: The user's message to process
        namespaces: Optional list of namespaces for context

    Yields:
        ChatEvent objects for each step of the orchestration
    """
    if isinstance(session_id, str):
        session_id_str = session_id
        session_id = uuid.UUID(session_id)
    else:
        session_id_str = str(session_id)

    logger.info(f"Starting Azure chat orchestration for session {session_id_str}")

    settings = get_settings()

    try:
        client = get_azure_client()
    except Exception as e:
        logger.error(f"Failed to get Azure client: {e}")
        yield error_event(f"AI service unavailable: {str(e)}")
        return

    deployment = settings.azure_openai_deployment
    if not deployment:
        yield error_event("Azure deployment not configured")
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

    # Get system prompt and convert tools
    base_prompt = await resolve_session_system_prompt(db, session_id)
    system_prompt = get_system_prompt(cluster_name, namespaces, base_prompt=base_prompt)
    openai_tools = convert_tools_to_openai_format(get_tool_definitions())

    iteration = 0

    while iteration < MAX_ORCHESTRATION_ITERATIONS:
        iteration += 1
        logger.debug(f"Azure orchestration iteration {iteration}")

        if cancel_event and cancel_event.is_set():
            logger.info(f"Azure chat cancelled at iteration {iteration}")
            yield done_event(stopped=True)
            return

        openai_messages = [
            {"role": "system", "content": system_prompt}
        ] + convert_messages_to_openai_format(
            truncate_messages(messages, MAX_CONVERSATION_MESSAGES)
        )

        try:
            response = await call_azure_with_retry(
                client,
                deployment,
                messages=openai_messages,
                tools=openai_tools,
                max_tokens=MAX_RESPONSE_TOKENS,
            )
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
            logger.exception("Unexpected error calling Azure OpenAI API")
            yield error_event(f"AI error: {str(e)}")
            return

        if cancel_event and cancel_event.is_set():
            logger.info(f"Azure chat cancelled at iteration {iteration} (after API call)")
            yield done_event(stopped=True)
            return

        # Process response
        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason

        # Track content for Anthropic-style storage
        assistant_content = []
        tool_calls_data = []

        # Handle text content
        if message.content:
            yield text_event(message.content)
            assistant_content.append({"type": "text", "text": message.content})

        # Handle tool calls
        if message.tool_calls:
            for tool_call in message.tool_calls:
                tool_id = tool_call.id
                tool_name = tool_call.function.name
                try:
                    tool_input = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    tool_input = {"raw": tool_call.function.arguments}

                tool_calls_data.append({"id": tool_id, "name": tool_name, "input": tool_input})
                assistant_content.append(
                    {"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input}
                )

        # Save assistant message (in Anthropic format for consistency)
        try:
            await save_message(
                db,
                session_id,
                "assistant",
                assistant_content,
                tool_calls=tool_calls_data if tool_calls_data else None,
                token_count=response.usage.completion_tokens if response.usage else None,
            )
        except Exception as e:
            logger.error(f"Failed to save assistant message: {e}")

        # Check if done
        if finish_reason == "stop":
            input_tok = response.usage.prompt_tokens if response.usage else 0
            output_tok = response.usage.completion_tokens if response.usage else 0
            logger.info(
                f"Chat completed after {iteration} iterations, tokens: {input_tok}/{output_tok}"
            )

            try:
                await update_session_stats(
                    db,
                    session_id,
                    input_tokens=input_tok,
                    output_tokens=output_tok,
                    iterations=iteration,
                )
                await record_usage(
                    db,
                    user_id=user_id,
                    session_id=session_id,
                    input_tokens=input_tok,
                    output_tokens=output_tok,
                )
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to persist session stats/usage: {e}")

            yield done_event(
                metadata={
                    "iterations": iteration,
                    "input_tokens": input_tok,
                    "output_tokens": output_tok,
                }
            )
            return

        # Execute tools if requested
        if finish_reason == "tool_calls" and tool_calls_data:
            if cancel_event and cancel_event.is_set():
                logger.info(f"Azure chat cancelled at iteration {iteration} (before tools)")
                yield done_event(stopped=True)
                return

            # Add assistant message to conversation (Anthropic format)
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []

            for tool_call in tool_calls_data:
                tool_id = tool_call["id"]
                tool_name = tool_call["name"]
                tool_input = tool_call["input"]

                logger.debug(f"Executing tool: {tool_name}")

                # Notify frontend that tool is starting
                yield tool_start_event(tool_name, tool_input, tool_id)

                # Execute the tool (kubectl or connector); credentials are injected
                # backend-side from the RAM store, never via the model.
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

                connector = connector_for_tool(tool_name)
                if connector and is_needs_auth(output, is_error):
                    yield connector_required_event(connector, session_id_str)

                # Format for storage (Anthropic format)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": output,
                        "is_error": is_error,
                    }
                )

            # Add tool results to conversation (role="user" per API convention)
            messages.append({"role": "user", "content": tool_results})

            try:
                await save_message(db, session_id, "tool_result", tool_results)
            except Exception as e:
                logger.error(f"Failed to save tool results: {e}")

            continue

        # Unexpected finish reason
        logger.warning(f"Unexpected finish_reason: {finish_reason}")
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
