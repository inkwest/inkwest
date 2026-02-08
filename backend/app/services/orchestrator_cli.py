"""
CLI-based Chat Orchestrator for Inkwest.

This module implements chat orchestration using the Claude CLI instead of
the Anthropic API. The CLI handles tool execution via MCP servers, so we
just parse and stream the output.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.ai_cli_client import execute_claude_cli, parse_cli_response
from app.services.budget_service import record_usage
from app.services.events import (
    ChatEvent,
    done_event,
    error_event,
)
from app.services.messages import (
    ensure_session_title,
    get_cli_session_id,
    save_message,
    set_cli_session_id,
    update_session_stats,
)

logger = logging.getLogger(__name__)


async def orchestrate_chat_cli(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    cluster_name: str,
    kubeconfig_path: str,
    context_name: str | None,
    user_message: str,
    namespaces: list[str] | None = None,
    user_id: uuid.UUID | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[ChatEvent]:
    """
    Orchestrate chat using Claude CLI.

    Unlike the API provider, tool execution happens inside the CLI via MCP.
    We just parse and stream the output.

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
    if isinstance(session_id, uuid.UUID):
        session_id_str = str(session_id)
    else:
        session_id_str = session_id

    logger.info(f"Starting CLI chat orchestration for session {session_id_str}")

    settings = get_settings()

    # Save user message
    try:
        await save_message(db, session_id, "user", user_message)
    except Exception as e:
        logger.error(f"Failed to save user message: {e}")
        yield error_event(f"Failed to save message: {str(e)}")
        return

    # Auto-generate session title for first message
    try:
        title = await ensure_session_title(db, session_id, user_message)
        if title:
            logger.debug(f"Generated session title: {title}")
    except Exception as e:
        logger.warning(f"Failed to set session title: {e}")
        # Non-critical, continue

    # Get stored CLI session ID for --resume (if any)
    cli_session_id = await get_cli_session_id(db, session_id)
    if cli_session_id:
        logger.info(f"Found existing CLI session: {cli_session_id}")

    # Build prompt with context
    # CLI will have access to kubectl via MCP, but we provide context
    namespace_context = ""
    if namespaces:
        namespace_context = f"\nAvailable namespaces: {', '.join(namespaces)}"

    context_info = ""
    if context_name:
        context_info = f"\nKubectl context: {context_name}"

    prompt = f"""You are a Kubernetes debugging assistant for cluster: {cluster_name}

CRITICAL KUBECONFIG REQUIREMENT:
- Kubeconfig files are stored in /etc/inkwest/kubeconfigs/
- The kubeconfig for this cluster is: {kubeconfig_path}
- You MUST use --kubeconfig={kubeconfig_path} with EVERY kubectl command
- Without this flag, kubectl will fail to connect to the cluster
{context_info}{namespace_context}

Correct usage example:
  kubectl --kubeconfig={kubeconfig_path} get pods
  kubectl --kubeconfig={kubeconfig_path} get services -n <namespace>
  kubectl --kubeconfig={kubeconfig_path} describe pod <pod-name>

User question: {user_message}

Investigate using kubectl and provide actionable insights."""

    try:
        # Execute CLI and stream response
        # Pass resume_session_id if we have one from a previous interaction
        output_stream = execute_claude_cli(
            session_id=session_id_str,
            prompt=prompt,
            cli_path=settings.claude_cli_path,
            timeout=settings.claude_cli_timeout,
            resume_session_id=cli_session_id,
        )

        collected_text: list[str] = []
        collected_tools: list[dict] = []
        received_done = False
        new_cli_session_id: str | None = None
        total_input_tokens = 0
        total_output_tokens = 0

        async for event in parse_cli_response(output_stream):
            if cancel_event and cancel_event.is_set():
                logger.info("CLI chat cancelled by user")
                yield done_event(stopped=True)
                return

            if event.type == "session_info" and event.metadata.get("cli_session_id"):
                new_cli_session_id = event.metadata["cli_session_id"]
                logger.info(f"Captured new CLI session ID: {new_cli_session_id}")

            elif event.type == "text_delta" and event.content:
                collected_text.append(event.content)
                yield event
            elif event.type == "tool_start":
                collected_tools.append(
                    {
                        "type": "tool_use",
                        "id": event.tool_use_id,
                        "name": event.tool_name,
                        "input": event.tool_input,
                    }
                )
                yield event
            elif event.type == "tool_result":
                collected_tools.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": event.tool_use_id,
                        "content": event.content,
                        "is_error": event.is_error,
                    }
                )
                yield event
            elif event.type == "done":
                received_done = True
                if event.metadata:
                    total_input_tokens = event.metadata.get("input_tokens", 0)
                    total_output_tokens = event.metadata.get("output_tokens", 0)
                yield event
            else:
                yield event

        # Store CLI session ID for future --resume calls
        if new_cli_session_id and new_cli_session_id != cli_session_id:
            try:
                await set_cli_session_id(db, session_id, new_cli_session_id)
            except Exception as e:
                logger.warning(f"Failed to store CLI session ID: {e}")

        # Save assistant message if we have content
        if collected_text or collected_tools:
            content: list[dict] = []

            if collected_text:
                content.append({"type": "text", "text": "".join(collected_text)})

            for tool_event in collected_tools:
                content.append(tool_event)

            try:
                await save_message(
                    db, session_id, "assistant", content if content else "".join(collected_text)
                )
            except Exception as e:
                logger.error(f"Failed to save assistant message: {e}")
                # Continue anyway - response was already streamed

        # Persist session stats and usage
        if total_input_tokens or total_output_tokens:
            try:
                await update_session_stats(
                    db,
                    session_id,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    tool_calls=len([t for t in collected_tools if t.get("type") == "tool_use"]),
                    iterations=1,
                )
                await record_usage(
                    db,
                    user_id=user_id,
                    session_id=uuid.UUID(str(session_id)),
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to persist session stats/usage: {e}")

        if not received_done:
            yield done_event()

    except TimeoutError as e:
        logger.error(f"Claude CLI timed out: {e}")
        yield error_event(f"Request timed out after {settings.claude_cli_timeout}s")

    except RuntimeError as e:
        logger.error(f"Claude CLI error: {e}")
        yield error_event(str(e))

    except Exception as e:
        logger.exception("Unexpected error in CLI orchestration")
        yield error_event(f"Unexpected error: {str(e)}")
