"""
CLI-based Chat Orchestrator for Inkwest.

This module implements chat orchestration using the Claude CLI instead of
the Anthropic API. The CLI handles tool execution via MCP servers, so we
just parse and stream the output.
"""

import asyncio
import logging
import os
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
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.tools.connector_exec import build_connector_env, connector_prompt_hint

logger = logging.getLogger(__name__)


async def orchestrate_chat_cli(
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

    # Expose this session's connected connector credentials as standard env vars
    # (AWS_*, GITLAB_TOKEN, KUBECONFIG, …) so the agent can use `aws`/`kubectl`/`curl`
    # naturally. Computed before the prompt so the kube section can reflect whether
    # per-session EKS contexts were wired. Read-only is enforced by the IAM/SSO role
    # the user selected, not here.
    connector_env, injected = await build_connector_env(user_id, uuid.UUID(str(session_id)))
    has_eks = "eks" in injected

    # Build prompt with context
    namespace_context = ""
    if namespaces:
        namespace_context = f"\nAvailable namespaces: {', '.join(namespaces)}"

    context_info = ""
    if context_name:
        context_info = f"\nKubectl context: {context_name}"

    if kubeconfig_path:
        kube_section = f"""
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
"""
    elif has_eks:
        # No statically-selected cluster, but the AWS role select discovered EKS
        # clusters and KUBECONFIG points at the generated contexts (details in the
        # external-access section below). Don't claim kubectl is unavailable.
        kube_section = """
KUBERNETES ACCESS IS AVAILABLE: kubectl is preconfigured with one context per
reachable EKS cluster (see the external access section below for how to use it).
Do NOT reconfigure kubectl or run `aws eks update-kubeconfig` — the contexts are
already set up.
"""
    else:
        kube_section = """
NO KUBERNETES CLUSTER IS CONFIGURED for this session: kubectl is unavailable
until a cluster or AWS/EKS access is set up. Rely on any external access listed
below (e.g. AWS, GitLab, Coralogix) and ask the user to configure cluster access
if Kubernetes inspection is required.
"""

    # Persona/behavior comes from the managed system prompt (DB default or admin
    # custom), layered onto Claude Code via --append-system-prompt. The user turn
    # carries only the runtime operational context + the question.
    base_prompt = await resolve_session_system_prompt(db, session_id)
    system_prompt = get_system_prompt(cluster_name, namespaces, base_prompt=base_prompt)

    prompt = f"""{kube_section}
User question: {user_message}

Investigate using the available tools and provide actionable insights.

IMPORTANT: You are NOT running on the target system. Investigate via the tools
and any external access listed below."""

    if injected:
        logger.info(
            "Injecting connector credentials into CLI session",
            extra={"session_id": session_id_str, "connectors": injected},
        )
        # Tell the agent which connectors it can use (the shell equivalent of the
        # tool descriptions the API providers receive).
        prompt += connector_prompt_hint(injected)

    # Run the agent in a per-session scratch dir so it neither sees the backend
    # source (/app) nor shares Claude Code's per-project transcripts with other
    # conversations — the working dir is the isolation key on both counts.
    work_dir = f"/tmp/inkwest-sessions/{session_id_str}"
    os.makedirs(work_dir, exist_ok=True)

    try:
        # Execute CLI and stream response
        # Pass resume_session_id if we have one from a previous interaction
        output_stream = execute_claude_cli(
            session_id=session_id_str,
            prompt=prompt,
            cli_path=settings.claude_cli_path,
            timeout=settings.claude_cli_timeout,
            resume_session_id=cli_session_id,
            extra_env=connector_env or None,
            cwd=work_dir,
            system_prompt=system_prompt,
        )

        # Single ordered content list built in stream arrival order, so the
        # persisted block order matches the causal order shown during live
        # streaming (steps then report, interleaving preserved). Building two
        # separate text/tool lists merged text-first would reorder the history
        # on reload.
        content: list[dict] = []
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
                # Coalesce consecutive deltas into the trailing text block; start
                # a new block if the last one is a tool event, to keep ordering.
                if content and content[-1]["type"] == "text":
                    content[-1]["text"] += event.content
                else:
                    content.append({"type": "text", "text": event.content})
                yield event
            elif event.type == "tool_start":
                content.append(
                    {
                        "type": "tool_use",
                        "id": event.tool_use_id,
                        "name": event.tool_name,
                        "input": event.tool_input,
                    }
                )
                yield event
            elif event.type == "tool_result":
                content.append(
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
        if content:
            try:
                await save_message(db, session_id, "assistant", content)
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
                    tool_calls=len([b for b in content if b.get("type") == "tool_use"]),
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
