"""
Broker orchestrator for the sandboxed ``inkwest-agent`` engine.

The custom Converse loop no longer runs in the portal: this orchestrator is the
**broker** side of the Portal ↔ Agent contract (docs/SANDBOXED_AGENT_RUNTIME.md,
decisions #2/#5). Per turn it resolves everything DB-bound (history, system
prompt, model + ARN/limits), packs it into a :class:`TurnSpec`, spawns
``inkwest-agent`` inside the conversation's sandbox, streams its NDJSON
``ChatEvent`` output back to the client, and persists from that stream — the
agent itself is DB-free. Cancellation is **by-kill** (the broker SIGTERM/SIGKILLs
the in-container process), so the custom loop carries no in-process cancel_event.

Mirrors ``orchestrate_chat_cli``'s sandbox lifecycle (ensure → run → release) and
``orchestrate_chat_converse``'s persistence model (assistant + tool_result as
separate role-correct messages, so the history round-trips through Converse on
the next turn).
"""

import asyncio
import dataclasses
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.core.constants import MAX_RESPONSE_TOKENS
from app.services import attachments as attachments_svc
from app.services import model_registry
from app.services.ai_cli_client import execute_agent, parse_agent_response
from app.services.ai_client import resolve_session_model
from app.services.budget_service import record_usage
from app.services.events import ChatEvent, done_event, error_event, mcp_auth_required_event
from app.services.messages import (
    ensure_session_title,
    get_enabled_mcp_servers,
    get_session_messages,
    save_message,
    update_session_stats,
)
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.services.session_skills import apply_session_skills, skills_container_root
from app.services.user_preferences import get_effective_skills
from app.tools.connector_exec import build_connector_env, session_kube_contexts
from app.tools.definitions import get_tool_definitions
from inkwest_agent.protocol import (
    AttachmentSpec,
    SkillSpec,
    ToolSpec,
    TurnContext,
    TurnLimits,
    TurnSpec,
)

logger = logging.getLogger(__name__)


async def orchestrate_chat_inkwest_agent(
    db: AsyncSession,
    session_id: str | uuid.UUID,
    cluster_name: str | None,
    kubeconfig_path: str | None,
    context_name: str | None,
    user_message: str,
    namespaces: list[str] | None = None,
    user_id: uuid.UUID | None = None,
    cancel_event: asyncio.Event | None = None,
    attachments: list[dict[str, Any]] | None = None,
    persist_user_message: bool = True,
    engine: str | None = None,
) -> AsyncIterator[ChatEvent]:
    """Broker one turn through the sandboxed ``inkwest-agent``. Mirrors orchestrate_chat_cli.

    ``attachments`` (P1.8): the upload route already wrote each file into the conversation's
    durable ``workdir/`` (mounted at ``/workdir`` in the sandbox), so the broker passes only
    each file's IN-CONTAINER ``/workdir/<filename>`` path + metadata on the TurnSpec — never
    the bytes (reuses the same one-way /workdir projection as claude-cli's @-file refs). The
    agent's Converse adapter turns IMAGE files into image blocks, gated on the model's IMAGE
    capability (``input_modalities``, also carried on the spec). A non-image / oversized /
    text-only-model attachment is skipped for image translation (the turn still runs).
    """
    if isinstance(session_id, uuid.UUID):
        session_id_str = str(session_id)
        session_uuid = session_id
    else:
        session_id_str = session_id
        session_uuid = uuid.UUID(session_id)

    logger.info(f"Starting inkwest-agent orchestration for session {session_id_str}")

    settings = get_settings()
    # The agent runs in the sandbox; the broker path is only reached when sandboxing is
    # on (get_orchestrator gates it), but guard defensively rather than spawn nothing.
    if not settings.sandbox_enabled:
        yield error_event("inkwest-agent requires the sandbox runtime, which is disabled.")
        return

    # Load prior turns BEFORE the new user turn (the agent loop appends user_message
    # itself from the spec). On retry the errored user row is already the last message;
    # drop it from history so it isn't sent twice (once in history, once as user_message).
    try:
        history = await get_session_messages(db, session_id)
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
        yield error_event(f"Failed to load conversation history: {str(e)}")
        return
    if not persist_user_message and history and history[-1]["role"] == "user":
        history = history[:-1]

    if persist_user_message:
        try:
            # Persist attachment descriptors on the user row too (history thumbnails),
            # parity with the claude-cli path. None when there are no files.
            await save_message(
                db,
                session_id,
                "user",
                user_message,
                attachments=attachments or None,
                author_user_id=user_id,
            )
        except Exception as e:
            logger.error(f"Failed to save user message: {e}")
            yield error_event(f"Failed to save message: {str(e)}")
            return
        # First message → auto-title (history is the prior turns, so empty == first).
        if not history:
            try:
                await ensure_session_title(db, session_id, user_message)
            except Exception as e:
                logger.warning(f"Failed to set session title: {e}")

    base_prompt = await resolve_session_system_prompt(db, session_id)
    # With no statically-configured cluster, surface the EKS clusters discovered from the
    # user's AWS access: the kubectl tools fall back to this session's generated kubeconfig
    # (tools/dispatch._resolve_eks_kubeconfig) and a `context` arg targets one. Reflect it
    # in the cluster label too, so the base prompt doesn't claim "no Kubernetes access".
    eks_contexts = session_kube_contexts(session_uuid) if kubeconfig_path is None else []
    effective_cluster_name = cluster_name
    if cluster_name is None and eks_contexts:
        effective_cluster_name = f"EKS via AWS access ({len(eks_contexts)} cluster(s))"
    system_prompt = get_system_prompt(effective_cluster_name, namespaces, base_prompt=base_prompt)
    if eks_contexts:
        listed = "\n".join(f"- {c}" for c in eks_contexts)
        system_prompt += (
            "\n\n## Kubernetes (EKS) access\n"
            "kubectl tools are available for the EKS clusters discovered from your AWS "
            "access. Pass the `context` argument on any kubectl tool to target a specific "
            "cluster; omit it to use the first one. Do NOT run `aws eks update-kubeconfig` "
            "— the contexts are already configured. Available contexts:\n" + listed
        )

    model = await resolve_session_model(db, session_id)
    # A custom row may pin an ARN (provisioned/imported model) and a max_tokens override —
    # reconcile the engine-resolution heuristic with the registry's authoritative config.
    entry = model_registry.get_entry(model)
    model_target = entry.arn if entry and entry.arn else None
    max_tokens = MAX_RESPONSE_TOKENS
    if entry and isinstance(entry.options.get("max_tokens"), int):
        max_tokens = entry.options["max_tokens"]

    # Make the agent model-aware. Non-Claude Converse models (Llama/Mistral/Nova/…) have
    # no innate sense of their own identity, and even Claude-on-Converse isn't told its
    # deployment name — so users asking "which model are you?" got vague answers. State it
    # explicitly in the system prompt (the broker is the only component that knows the
    # resolved model + engine for the turn).
    model_label = (getattr(entry, "label", None) or model) if entry else model
    system_prompt += (
        "\n\n## Runtime identity\n"
        f"You are running as the **{model_label}** model (id `{model}`) on the Inkwest "
        "Agent engine (Amazon Bedrock, Converse API). When asked which model or engine "
        "you are, answer with this."
    )

    # Anchor today's date in the prompt. Open-weight Converse models routinely guess the
    # year wrong (e.g. emit 2024 timestamps in metric queries) and don't chain a datetime
    # tool's output into later tool args — yielding empty windows that read as false "no
    # data" findings. DATE granularity, not second: a per-turn timestamp would make the
    # system prompt unique every turn and defeat any prompt cache later added to this path
    # (none today — no cachePoint here yet); a date stays stable within a UTC day and still
    # fixes the year/month guess. For an exact instant the agent has the synthetic
    # `current_datetime` tool (see the caveat) — accuracy on demand, reliability up front.
    today = datetime.now(UTC).date().isoformat()
    system_prompt += (
        "\n\n## Current date\n"
        f"Today's UTC date is **{today}** (captured at turn start). Use it for the "
        "year/month in timestamps and for relative ranges (today, last 24h, this week). "
        "This date is approximate — for an exact current instant, or before any "
        "time-sensitive query, call the `current_datetime` tool and re-check accurately."
    )

    # Pre-provide the session's connector creds: build_connector_env renews-on-read and
    # projects the AWS profiles onto the shared tmpfs (write_aws_profiles), returning the
    # IN-CONTAINER env (AWS_* file paths, GITLAB_TOKEN, GITLAB_BASE_URL, KUBECONFIG). We
    # forward this env to the agent at spawn (docker exec -e) — a one-way push; the agent
    # never reaches back to the portal credential store. The agent's tool executors read
    # these creds from the env; creds_path names the tmpfs ~/.aws for the contract (it is
    # a descriptor — the executors consume the env, not this path).
    #
    # The env carries AWS_SHARED_CREDENTIALS_FILE (the connector profiles), NOT explicit
    # AWS_ACCESS_KEY_ID/SECRET, so it does not displace the agent's model-call credential:
    # the Bedrock backend authenticates with AWS_BEARER_TOKEN_BEDROCK (set in the
    # container's sandbox.env), and botocore prefers the bearer token over the
    # shared-credentials file for the bedrock service — so make_bedrock_client still talks
    # to the backend identity. (The connector `aws` subprocess gets the bearer token
    # stripped — see _projected_aws_env.) Requires sandbox_creds_root to be set for the
    # AWS connector to resolve in-container; without it build_connector_env emits
    # backend-local paths the agent can't see and aws_call degrades to NEEDS_AUTH.
    #
    # Best-effort: a credential-renewal hiccup must not crash the turn — degrade to no
    # connector env (kubectl/model still work; connector tools report NEEDS_AUTH), exactly
    # how a missing grant already behaves.
    connector_env: dict[str, str] = {}
    if user_id is not None:
        try:
            connector_env, injected = await build_connector_env(user_id, session_uuid)
        except Exception as e:
            logger.warning(f"Failed to project connector credentials: {e}")
            connector_env, injected = {}, []
        if injected:
            logger.info(
                "Projecting connector credentials to inkwest-agent",
                extra={"session_id": session_id_str, "connectors": injected},
            )
    creds_path = f"{settings.sandbox_home.rstrip('/')}/.aws"

    # Skills + MCP (P1.7): resolve from the DB here (the agent is DB-free). Skills ride
    # the spec as name+description ONLY (progressive disclosure); their SKILL.md bodies
    # are materialized into the sandbox below (parity with the claude-cli path) and read
    # on demand by the agent's read_skill tool. MCP server defs are materialized into a
    # sandbox file whose in-container path rides the spec (set after sandbox.ensure).
    try:
        active_skills = await get_effective_skills(db, user_id)
    except Exception as e:
        logger.warning("active_skills_load_failed", extra={"error": str(e)})
        active_skills = []
    try:
        enabled_mcp = await get_enabled_mcp_servers(db, session_id)
    except Exception as e:
        logger.warning("enabled_mcp_load_failed", extra={"error": str(e)})
        enabled_mcp = []

    container: str | None = None
    try:
        from app.services import sandbox
        from app.services.agent_mcp_config import (
            remove_agent_mcp_config,
            write_agent_mcp_config_with_oauth,
        )

        uid = str(user_id) if user_id else None
        # Reap the user's oldest idle pairs first; refuse if they're at the hard ceiling
        # with every conversation busy (nothing reclaimable). The finally is None-safe.
        try:
            await sandbox.enforce_user_sandbox_cap(uid, session_id_str)
        except sandbox.SandboxCapExceededError as e:
            yield error_event(str(e))
            return
        container = await sandbox.ensure(session_id_str, uid)

        # Security: prune any cred-bearing MCP config orphaned by a PRIOR turn whose broker
        # was SIGKILLed before its `finally` cleanup could run (the file lives in the durable
        # claude dir). Done right after ensure so the stale creds never sit around — the
        # fresh config (if any) is re-materialized just below from the current DB state.
        remove_agent_mcp_config(session_id_str)

        # Materialize skill bodies + the MCP config into THIS conversation's sandbox, every
        # turn (survives a sandbox rebuild; picks up admin edits next turn) — same cadence
        # as orchestrate_chat_cli. apply_session_skills writes one SKILL.md per skill; the
        # MCP config is removed on turn end (it can embed creds — see the finally).
        apply_session_skills(session_id_str, active_skills)
        # Static-catalog servers inject verbatim; enabled OAuth2 servers inject a proxy URL
        # + mint-stable JWT (engine parity with the claude-cli path). An enabled OAuth2
        # server with no active grant is skipped — tell the user to reconnect instead of
        # letting its tools silently vanish from the agent's tool surface.
        mcp_config_path, mcp_dead = await write_agent_mcp_config_with_oauth(
            session_id_str, enabled_mcp, user_id=user_id, db=db
        )
        for dead_key in mcp_dead:
            yield mcp_auth_required_event(dead_key, session_id_str)

        # Project this turn's attachments onto the spec (P1.8). The files are already in the
        # conversation's durable workdir (the upload route wrote them); they bind-mount at
        # /workdir in the sandbox, so the in-container path is /workdir/<filename> — the same
        # one-way projection claude-cli uses for @-file refs.
        #
        # SECURITY: the `attachments` descriptors are echoed by the client over the WS, so the
        # filename is untrusted. Validate each through attachments.resolve_existing, which
        # rejects any name with a path component / dot-segment and confirms the file actually
        # lives in THIS session's workdir (realpath+commonpath, so traversal and symlink escape
        # are both refused) — otherwise a crafted `filename` like "../.aws/credentials" would
        # have the in-sandbox adapter read+exfiltrate an arbitrary file via the model. Only the
        # validated basename is used to build the in-container path; bad entries are dropped.
        attachment_specs: list[AttachmentSpec] = []
        for a in attachments or []:
            if not (isinstance(a, dict) and a.get("filename")):
                continue
            name = str(a["filename"])
            try:
                attachments_svc.resolve_existing(session_id_str, name)
            except attachments_svc.AttachmentError as e:
                # NB: "filename" is a reserved LogRecord attribute — use "attachment".
                logger.warning("attachment_rejected", extra={"attachment": name, "error": str(e)})
                continue
            attachment_specs.append(
                AttachmentSpec(
                    path=f"/workdir/{name}",
                    content_type=str(a.get("content_type") or ""),
                    filename=name,
                    size=a.get("size") if isinstance(a.get("size"), int) else None,
                )
            )

        spec = TurnSpec(
            session_id=session_id_str,
            user_message=user_message,
            model=model,
            system_prompt=system_prompt,
            context=TurnContext(history=history),  # mode=replay: broker seeds history
            limits=TurnLimits(max_tokens=max_tokens),
            tools=[ToolSpec(name=t["name"]) for t in get_tool_definitions()],
            skills=[SkillSpec(name=s.name, description=s.description or "") for s in active_skills],
            skills_root=skills_container_root() if active_skills else None,
            mcp_config_path=mcp_config_path,
            attachments=attachment_specs,
            # The agent is DB-free; hand it the resolved model's input modalities so the
            # Converse adapter can gate IMAGE translation (empty = text-only/unknown).
            input_modalities=list(getattr(entry, "input_modalities", []) or []),
            model_target=model_target,
            region=settings.aws_region,
            user_id=str(user_id) if user_id else None,
            kubeconfig_path=kubeconfig_path,
            context_name=context_name,
            creds_path=creds_path,
        )
        turn_spec_json = json.dumps(dataclasses.asdict(spec))

        # Reconstruct role-correct messages from the flat event stream so the persisted
        # history round-trips through Converse next turn (toolResult must live in a user
        # turn, not the assistant one). A pending tool_result batch is flushed — as an
        # assistant message + a tool_result message — when the next assistant content
        # arrives (text or a new tool call), keeping every stored message valid.
        assistant_blocks: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []
        received_terminal = False
        done_meta: dict[str, Any] = {}

        async def _flush() -> None:
            nonlocal assistant_blocks, pending_results
            if assistant_blocks:
                try:
                    await save_message(
                        db, session_id, "assistant", assistant_blocks, model=model, engine=engine
                    )
                except Exception as e:
                    logger.error(f"Failed to save assistant message: {e}")
            if pending_results:
                try:
                    await save_message(db, session_id, "tool_result", pending_results)
                except Exception as e:
                    logger.error(f"Failed to save tool results: {e}")
            assistant_blocks = []
            pending_results = []

        try:
            output_stream = execute_agent(
                session_id_str,
                turn_spec_json,
                container=container,
                docker_path=settings.sandbox_docker_path,
                extra_env=connector_env or None,
                timeout=settings.claude_cli_timeout,
                cancel_event=cancel_event,
            )
            async for event in parse_agent_response(output_stream):
                # Cancellation is handled inside execute_agent (races each read against
                # the stop, kills the in-container agent, ends the stream) — not here.
                if event.type == "text_delta" and event.content:
                    if pending_results:
                        await _flush()  # new assistant turn closes the prior tool exchange
                    if assistant_blocks and assistant_blocks[-1]["type"] == "text":
                        assistant_blocks[-1]["text"] += event.content
                    else:
                        assistant_blocks.append({"type": "text", "text": event.content})
                elif event.type == "tool_start":
                    if pending_results:
                        await _flush()
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": event.tool_use_id,
                            "name": event.tool_name,
                            "input": event.tool_input,
                        }
                    )
                elif event.type == "tool_result":
                    # Count agent-side tools here (the agent runs its own executor in the
                    # unscraped sandbox; the backend only sees the streamed result). Covers
                    # skill/MCP tools that never touch tool_execution.execute_tool_calls —
                    # record_tool_call routes mcp__* to the MCP metric.
                    metrics.record_tool_call(event.tool_name or "unknown", event.is_error)
                    pending_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": event.tool_use_id,
                            "content": event.content,
                            "is_error": event.is_error,
                        }
                    )
                elif event.type in ("done", "error"):
                    received_terminal = True
                    if event.type == "done":
                        done_meta = event.metadata or {}
                yield event

            # On an explicit cancel the agent was SIGKILLed mid-turn: assistant_blocks may
            # hold a tool_use whose tool_result never arrived. Persisting that would leave a
            # dangling toolUse in history and break the NEXT Converse turn — so discard the
            # partial (matches orchestrator_cli) and stop before _flush.
            if cancel_event and cancel_event.is_set():
                logger.info("inkwest-agent chat cancelled by user")
                yield done_event(stopped=True)
                return

            # Drain anything left from the final iteration (terminal text / tool exchange).
            await _flush()
        except (RuntimeError, TimeoutError) as e:
            # A non-zero exit / timeout AFTER the agent already emitted its terminal event
            # is redundant (the client saw it) — only surface when nothing terminal came
            # through, i.e. the agent crashed silently.
            if not received_terminal:
                logger.error(f"inkwest-agent failed: {e}")
                yield error_event(str(e))
            return

        # Persist usage from the terminal done meta (the agent reports aggregate totals).
        in_tokens = done_meta.get("input_tokens", 0)
        out_tokens = done_meta.get("output_tokens", 0)
        if in_tokens or out_tokens:
            try:
                await update_session_stats(
                    db,
                    session_id,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    tool_calls=done_meta.get("tool_calls", 0),
                    iterations=done_meta.get("iterations", 1),
                )
                await record_usage(
                    db,
                    user_id=user_id,
                    session_id=session_uuid,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    model=model,
                )
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to persist session stats/usage: {e}")

        if not received_terminal:
            yield done_event()

    except Exception as e:
        logger.exception("Unexpected error in inkwest-agent orchestration")
        yield error_event(f"Unexpected error: {str(e)}")

    finally:
        # Remove the materialized MCP config: it can embed MCP creds/headers, so it must
        # not outlive the turn (one-way projection — re-materialized from the DB next turn).
        from app.services.agent_mcp_config import remove_agent_mcp_config

        remove_agent_mcp_config(session_id_str)
        # Drop the in-use lease ensure() took so the idle reaper can reclaim the sandbox.
        if container is not None:
            from app.services import sandbox

            await sandbox.release(session_id_str)
