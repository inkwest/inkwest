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

from app.core import metrics
from app.core.config import get_settings
from app.services import attachments as attachments_svc
from app.services import engines, llm_broker, model_registry
from app.services.ai_cli_client import execute_claude_cli, parse_cli_response
from app.services.ai_client import resolve_session_model_choice
from app.services.budget_service import record_usage
from app.services.events import (
    ChatEvent,
    done_event,
    error_event,
    mcp_auth_required_event,
)
from app.services.messages import (
    clear_cli_session_id,
    ensure_session_title,
    get_cli_session_id,
    get_enabled_mcp_servers,
    save_message,
    set_cli_session_id,
    update_session_stats,
)
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.services.session_skills import apply_session_skills
from app.services.user_preferences import get_effective_skills
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
    attachments: list[dict] | None = None,
    persist_user_message: bool = True,
    engine: str | None = None,
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
        attachments: Files uploaded to this turn's /workdir, referenced to the
            agent as @/workdir/<file> (sandbox only)

    Yields:
        ChatEvent objects for each step of the orchestration
    """
    if isinstance(session_id, uuid.UUID):
        session_id_str = str(session_id)
    else:
        session_id_str = session_id

    logger.info(f"Starting CLI chat orchestration for session {session_id_str}")

    settings = get_settings()

    # Save user message with its attachment descriptors (for history thumbnails).
    # Skipped on retry — the errored row is reused. NOTE: the CLI keeps its own
    # --resume history; if the interrupted attempt already recorded this user turn
    # there, re-sending the prompt below can duplicate it CLI-side. Accepted: the
    # CLI provider is local-dev only (the API providers, which replay DB history,
    # are used in prod).
    if persist_user_message:
        try:
            await save_message(
                db,
                session_id,
                "user",
                user_message,
                attachments=attachments,
                author_user_id=user_id,
            )
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
        # One connector among those granted this session — kubectl access via a
        # statically-selected kubeconfig. Framed as a usage note, consistent with
        # the external-access section below, not as the agent's whole purpose.
        kube_section = f"""
Kubernetes access (one of this session's connectors):
- Kubeconfig files are stored in /etc/inkwest/kubeconfigs/
- The kubeconfig for this cluster is: {kubeconfig_path}
- You MUST pass --kubeconfig={kubeconfig_path} on EVERY kubectl command;
  without it kubectl cannot reach the cluster.
{context_info}{namespace_context}

Usage example:
  kubectl --kubeconfig={kubeconfig_path} get pods
  kubectl --kubeconfig={kubeconfig_path} get services -n <namespace>
  kubectl --kubeconfig={kubeconfig_path} describe pod <pod-name>
"""
    elif has_eks:
        # No statically-selected cluster, but the AWS role select discovered EKS
        # clusters and KUBECONFIG points at the generated contexts (details in the
        # external-access section below). Don't claim kubectl is unavailable.
        kube_section = """
Kubernetes access (via EKS): kubectl is preconfigured with one context per
reachable EKS cluster (see the external access section below for how to use it).
Do NOT reconfigure kubectl or run `aws eks update-kubeconfig` — the contexts are
already set up.
"""
    else:
        # No Kubernetes this session — investigate with whatever other connectors
        # are granted; the external-access section below lists them.
        kube_section = """
No Kubernetes cluster is configured for this session, so kubectl is unavailable.
Investigate using the other connectors granted below (e.g. AWS, GitLab), and ask
the user to configure cluster access if Kubernetes inspection turns out to be
required.
"""

    # Persona/behavior comes from the managed system prompt (DB default or admin
    # custom), layered onto Claude Code via --append-system-prompt. The runtime
    # operational context (how to reach the cluster + which connectors are granted) goes
    # into that appended system prompt too — NOT the user turn — so the user message is
    # sent VERBATIM. That lets a "/skill" reference expand ANYWHERE in the message (Claude
    # Code resolves it from the prompt string); wrapping it would bury the "/" and the
    # surrounding text would be captured as the skill's $ARGUMENTS.
    base_prompt = await resolve_session_system_prompt(db, session_id)
    system_prompt = get_system_prompt(cluster_name, namespaces, base_prompt=base_prompt)

    runtime_context = (
        f"{kube_section}\n"
        "Investigate using the available tools and provide actionable insights. You are "
        "NOT running on the target system — investigate via the tools and any external "
        "access listed below."
    )

    # Explicit model choice (session pick or admin default) is forwarded to the
    # CLI; None lets the in-toolbox claude use its own configured default.
    model_choice = await resolve_session_model_choice(db, session_id)
    if injected:
        logger.info(
            "Injecting connector credentials into CLI session",
            extra={"session_id": session_id_str, "connectors": injected},
        )
        # Tell the agent which connectors it can use (the shell equivalent of the
        # tool descriptions the API providers receive).
        runtime_context += connector_prompt_hint(injected)
    system_prompt = f"{system_prompt}\n\n{runtime_context}"

    prompt = user_message
    # Attached files live in this conversation's /workdir; append @/workdir/<file>
    # references so Claude Code opens them. Sandbox only — that's the only mode with a
    # /workdir mount (the local fallback dir holds no uploads). Appended AFTER the
    # verbatim user message so a leading "/skill" still resolves (see system-prompt note).
    if attachments and settings.sandbox_enabled:
        # SECURITY: attachment descriptors are echoed by the client over the WS, so `filename`
        # is untrusted. Validate each through attachments.resolve_existing (rejects any path
        # component / dot-segment and confirms the file lives in THIS session's workdir via
        # realpath+commonpath) before building an @-file ref — otherwise a crafted name like
        # "../.aws/credentials" would have Claude Code read+exfiltrate an arbitrary file.
        # Mirrors orchestrator_inkwest_agent's guard; bad entries are dropped.
        safe_names: list[str] = []
        for a in attachments:
            name = a.get("filename")
            if not name:
                continue
            name = str(name)
            try:
                attachments_svc.resolve_existing(session_id_str, name)
            except attachments_svc.AttachmentError as e:
                logger.warning("attachment_rejected", extra={"attachment": name, "error": str(e)})
                continue
            safe_names.append(name)
        refs = " ".join(f"@/workdir/{name}" for name in safe_names)
        if refs:
            prompt = f"{user_message}\n\n{refs}" if user_message else refs

    # Bound to this turn; the broker grant (if any) is minted below and BURNED in the finally
    # so it dies with the turn. Initialized here so the finally can revoke it even on an early
    # failure (it carries the token in AWS_BEARER_TOKEN_BEDROCK; empty ⇒ burn is a no-op).
    broker_env: dict[str, str] = {}
    try:
        # Where the agent works. In sandbox mode `claude` runs inside this
        # conversation's own container: ensure() provisions the toolbox+proxy pair on
        # the first turn (reused after), the work dir is /workdir (durable scratch), and
        # Claude Code state persists on the durable CLAUDE_CONFIG_DIR mount for --resume.
        # Otherwise fall back to a local per-session scratch dir so the agent neither sees
        # the backend source (/app) nor shares Claude Code transcripts across conversations.
        container: str | None = None
        if settings.sandbox_enabled:
            from app.services import sandbox
            from app.services.mcp_config import apply_session_mcp_selection

            uid = str(user_id) if user_id else None
            # Reap the user's oldest idle pairs first; refuse if they're at the hard ceiling
            # with every conversation busy. Caught here (not the generic handler below) so the
            # user gets the plain "too many conversations" message, not "Unexpected error".
            try:
                await sandbox.enforce_user_sandbox_cap(uid, session_id_str)
            except sandbox.SandboxCapExceededError as e:
                yield error_event(str(e))
                return
            container = await sandbox.ensure(session_id_str, uid)
            work_dir = "/workdir"
            # Reconcile the conversation's enabled MCP servers into its .claude.json
            # before the CLI starts (the CLI reads its config at launch). Done every
            # turn so it survives a sandbox rebuild that recreates an empty claude dir.
            enabled_mcp = await get_enabled_mcp_servers(db, session_id)
            mcp_dead = await apply_session_mcp_selection(
                session_id_str, enabled_mcp, user_id=user_id, db=db
            )
            # An enabled OAuth2 MCP server with no active grant is skipped — tell the user to
            # reconnect instead of letting its tools silently vanish.
            for dead_key in mcp_dead:
                yield mcp_auth_required_event(dead_key, session_id_str)
            # Materialize the user's EFFECTIVE skills as Claude skills in this
            # conversation's sandbox (one SKILL.md per skill): active admin skills the
            # user hasn't disabled, plus their personal skills (which shadow admin ones).
            # Like the MCP reconcile above, done every turn from the DB so edits propagate
            # on the next turn of each session and it survives a sandbox rebuild.
            templates = await get_effective_skills(db, user_id)
            apply_session_skills(session_id_str, templates)
        else:
            work_dir = f"/tmp/inkwest-sessions/{session_id_str}"
            os.makedirs(work_dir, exist_ok=True)

        # Execute the CLI and stream the response. A stored --resume target can be stale
        # (the sandbox was rebuilt, so the transcript is gone): `claude --resume` then
        # fails before emitting anything. Detect that, clear the id, and retry once from a
        # fresh session so the conversation self-heals instead of erroring on every turn.
        # The assistant message is a single ordered `content` list built in stream arrival
        # order, so the persisted block order matches what was shown live.
        content: list[dict] = []
        received_done = False
        new_cli_session_id: str | None = None
        # Model reported by the CLI's init event — what actually ran this turn.
        actual_model: str | None = None
        total_input_tokens = 0
        total_output_tokens = 0
        resume_id = cli_session_id

        # Route the CLI's Bedrock calls through the LLM auth broker when enabled (sandbox only
        # — the broker base URL resolves to the host-gateway alias reachable solely from inside
        # the sandbox). The grant + CLAUDE_CODE_USE_BEDROCK + ANTHROPIC_BEDROCK_BASE_URL ride the
        # connector-env channel (docker exec -e), overriding sandbox_env_file so the real
        # bearer no longer needs to live in the sandbox. Off/local ⇒ empty ⇒ today's behavior.
        # One turn_id for every usage row this turn produces: threaded into the broker grant
        # (each proxied Bedrock call's row carries it) and into the engine-side record_usage
        # fallback, so per-turn aggregation works identically regardless of the source.
        turn_id = uuid.uuid4()
        broker_env = (
            llm_broker.cli_broker_env(
                user_id, session_id, engine=engines.CLAUDE_CLI, turn_id=turn_id
            )
            if settings.sandbox_enabled
            else {}
        )
        engine_env = {**connector_env, **broker_env}

        # With the broker ON, a None model_choice (no session pin, no admin default) would let
        # the in-toolbox `claude` invoke its OWN built-in default id — which the broker's
        # enabled-models allowlist rejects (403), breaking the turn opaquely. Force an explicit
        # model the broker permits: the registry's resolved default (an enabled model when any
        # is enabled). If NOTHING is enabled the broker is fail-closed and no model is invocable
        # — surface that as a clear config error instead of a mid-turn 403. Off ⇒ unchanged.
        if broker_env and model_choice is None:
            if not any(e.enabled for e in model_registry.merged_entries()):
                yield error_event(
                    "No models are enabled for the LLM broker. Enable at least one model "
                    "in admin settings before using the broker."
                )
                return
            model_choice = model_registry.default_model()

        for attempt in (1, 2):
            content = []
            received_done = False
            new_cli_session_id = None
            total_input_tokens = 0
            total_output_tokens = 0
            emitted = False
            try:
                output_stream = execute_claude_cli(
                    session_id=session_id_str,
                    prompt=prompt,
                    cli_path=settings.claude_cli_path,
                    timeout=settings.claude_cli_timeout,
                    resume_session_id=resume_id,
                    extra_env=engine_env or None,
                    cwd=work_dir,
                    system_prompt=system_prompt,
                    container=container,
                    docker_path=settings.sandbox_docker_path,
                    model=model_choice,
                    cancel_event=cancel_event,
                    idle_timeout=settings.agent_stream_idle_timeout,
                )

                async for event in parse_cli_response(output_stream):
                    # NB: cancellation is handled authoritatively inside execute_claude_cli
                    # (it races each read against the stop, kills the agent, then ends the
                    # stream) — NOT by returning here. Returning mid-stream would abandon the
                    # CLI generator before its kill ran (async-gen cleanup is GC-timed), so
                    # the in-container agent could outlive the stop. We let the stream end and
                    # emit the stopped marker once, below.

                    if event.type == "session_info" and event.metadata.get("cli_session_id"):
                        # init/metadata only — NOT client-visible output, so it must not
                        # set `emitted` (a --resume run can emit init before claude finds
                        # the transcript missing and fails; the retry must still fire).
                        new_cli_session_id = event.metadata["cli_session_id"]
                        logger.info(f"Captured new CLI session ID: {new_cli_session_id}")
                        # The model the CLI actually runs with (its own default when no
                        # --model was passed) — used for accurate cost attribution.
                        if event.metadata.get("model"):
                            actual_model = event.metadata["model"]
                        continue

                    # Any client-visible event means the turn produced output → don't retry.
                    emitted = True
                    if event.type == "text_delta" and event.content:
                        # Coalesce consecutive deltas into the trailing text block; start a
                        # new block if the last is a tool event, to keep order.
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
                        # The CLI runs tools inside its own subprocess loop (not
                        # execute_tool_calls), so count them here off the streamed result —
                        # otherwise the claude-cli engine's tool usage is invisible.
                        # record_tool_call routes mcp__* to the MCP metric.
                        metrics.record_tool_call(event.tool_name or "unknown", event.is_error)
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

                # The stream ends naturally once execute_claude_cli has stopped the agent
                # on an explicit cancel; surface that to the client as a stopped turn.
                if cancel_event and cancel_event.is_set():
                    logger.info("CLI chat cancelled by user")
                    yield done_event(stopped=True)
                    return
                break  # turn completed
            except RuntimeError as e:
                # Stale --resume target (sandbox rebuilt -> transcript gone): the CLI fails
                # before any client-visible output. Detect it via the message OR the resume
                # id echoed in the error (resilient to CLI wording changes), then clear it
                # and retry once from a fresh session.
                err = str(e)
                is_stale_resume = resume_id is not None and (
                    "No conversation found" in err or resume_id in err
                )
                if attempt == 1 and not emitted and is_stale_resume:
                    logger.warning(f"Stale CLI session {resume_id}; clearing and retrying fresh")
                    try:
                        await clear_cli_session_id(db, session_id)
                    except Exception as ce:
                        logger.warning(f"Failed to clear stale CLI session ID: {ce}")
                    resume_id = None
                    continue
                raise

        # Store CLI session ID for future --resume calls
        if new_cli_session_id and new_cli_session_id != cli_session_id:
            try:
                await set_cli_session_id(db, session_id, new_cli_session_id)
            except Exception as e:
                logger.warning(f"Failed to store CLI session ID: {e}")

        # Save assistant message if we have content. Attribute to what actually
        # ran (CLI-reported model, else the explicit choice) — the same source the
        # usage stats use below — for accurate per-turn cost.
        if content:
            try:
                await save_message(
                    db,
                    session_id,
                    "assistant",
                    content,
                    model=actual_model or model_choice,
                    engine=engine,
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
                    tool_calls=len([b for b in content if b.get("type") == "tool_use"]),
                    iterations=1,
                )
                # Broker-on (grant minted ⇒ broker_env non-empty): the proxy already
                # persisted one authoritative UsageRecord per Bedrock call
                # (source='broker', cache tokens included) — an engine-side row would
                # double-count the budget. Session stats above (and the Message row)
                # still come from the engine.
                if not broker_env:
                    await record_usage(
                        db,
                        user_id=user_id,
                        session_id=uuid.UUID(str(session_id)),
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                        # Attribute to what actually ran: CLI-reported model, else the
                        # explicit choice; None falls back to the catalog default
                        # (approximate — the CLI used its own default then).
                        model=actual_model or model_choice,
                        turn_id=turn_id,
                    )
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to persist session stats/usage: {e}")

        if not received_done:
            yield done_event()

    except TimeoutError as e:
        # Surface the actual cause — there are two timeout sources with distinct messages:
        # the post-stream process.wait() cap and the per-line idle-stall guard. A hardcoded
        # "after {claude_cli_timeout}s" would misreport the idle case.
        logger.error(f"Claude CLI timed out: {e}")
        yield error_event(f"Request timed out: {e}")

    except RuntimeError as e:
        logger.error(f"Claude CLI error: {e}")
        yield error_event(str(e))

    except Exception as e:
        logger.exception("Unexpected error in CLI orchestration")
        yield error_event(f"Unexpected error: {str(e)}")

    finally:
        # Burn this turn's broker grant: the injected AWS_BEARER_TOKEN_BEDROCK is dead the
        # moment the turn ends, so a token exfiltrated from the sandbox can't be replayed
        # afterwards. No-op when the broker is off (empty env). Runs on every exit path,
        # including cancel/error (the agent process is already gone by here).
        llm_broker.burn_env(broker_env)
        # Drop the in-use lease ensure() took so the idle reaper can reclaim this sandbox
        # once it goes quiet. Held for the whole turn (not just ensure()) so a turn longer
        # than SANDBOX_IDLE_TTL isn't torn down mid-exec.
        if settings.sandbox_enabled and container is not None:
            from app.services import sandbox

            await sandbox.release(session_id_str)
