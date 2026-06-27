"""
Broker orchestrator for the sandboxed ``opencode`` engine.

opencode (anomalyco/opencode) is a terminal coding agent run non-interactively in
the conversation sandbox — parity with ``orchestrate_chat_cli``: resolve the DB-bound
turn state, spawn ``opencode run --format json`` in the container, translate its NDJSON
to the canonical ``ChatEvent`` stream, and persist from it. The portal stays a broker;
opencode owns the loop + its built-in tools (bash/read/write/edit/grep/glob).

Resume model = opencode's own per-conversation session store (durable
``$HOME/.local/share/opencode`` bind-mount, see sandbox.py): the new user message rides
as argv and prior turns are resumed with ``--continue`` whenever history exists, so the
store stays the working transcript while Postgres remains the durable source of truth
(persisted from the event stream below). Cancellation is by-kill (the broker SIGKILLs the
in-container process); the system prompt is delivered as an ``AGENTS.md`` in the workdir.
"""

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.services.ai_cli_client import execute_opencode, parse_opencode_response
from app.services.ai_client import resolve_session_model
from app.services.budget_service import record_usage
from app.services.events import (
    ChatEvent,
    connector_required_event,
    done_event,
    error_event,
    mcp_auth_required_event,
)
from app.services.messages import (
    ensure_session_title,
    get_active_skills,
    get_enabled_mcp_servers,
    get_session_messages,
    save_message,
    update_session_stats,
)
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.services.session_skills import apply_session_skills, skills_container_root

logger = logging.getLogger(__name__)

# Unambiguous AWS credential-expiry/invalidity signatures the `aws` CLI prints to stderr.
# opencode runs `aws` in its own bash (no structured connector tool → no re-auth marker), so
# we sniff a FAILED tool result for these to still offer the "Give access" affordance. Kept
# tight on purpose — these mean "creds expired/invalid", NOT "permission denied" (a read-only
# role legitimately gets AccessDenied on disallowed calls, which must NOT trigger re-auth).
_AWS_EXPIRY_SIGNATURES = (
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "UnrecognizedClientException",
    "security token included in the request is expired",
)


def _to_opencode_mcp(servers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Translate Claude-shaped ``mcpServers`` defs into opencode's ``mcp`` config shape.

    Claude remote (``{url, headers}``) → opencode ``{type:"remote", url, headers, enabled}``;
    Claude stdio (``{command, args, env}``) → opencode ``{type:"local", command:[...],
    environment, enabled}``. Both the static catalog and the OAuth2 proxy entries (which are
    remote URLs carrying a mint-stable JWT header) round-trip through this.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, d in servers.items():
        if d.get("url"):
            entry: dict[str, Any] = {"type": "remote", "url": d["url"], "enabled": True}
            if d.get("headers"):
                entry["headers"] = d["headers"]
            out[name] = entry
        elif d.get("command"):
            cmd = d["command"]
            command = [cmd, *d.get("args", [])] if isinstance(cmd, str) else list(cmd)
            entry = {"type": "local", "command": command, "enabled": True}
            if d.get("env"):
                entry["environment"] = d["env"]
            out[name] = entry
        # An entry that is neither remote nor stdio is skipped (opencode would reject it).
    return out


async def _opencode_mcp_block(
    session_id: str, enabled_keys: list[str], *, user_id: uuid.UUID | None, db: AsyncSession
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Build opencode's ``mcp`` block for the enabled servers; return (mcp, dead_keys).

    Reuses the SAME inputs as the claude-cli / inkwest-agent engines (static catalog +
    OAuth2 proxy servers carrying a mint-stable internal JWT) so MCP brokering is identical
    across engines — only the on-disk config shape differs. ``dead_keys`` are enabled OAuth2
    servers with no active grant (surfaced to the user as ``mcp_auth_required``).
    """
    if not enabled_keys:
        return {}, []
    from app.services.mcp_catalog import build_mcp_servers
    from app.services.mcp_config import oauth_proxy_servers_for

    static = build_mcp_servers(enabled_keys)
    oauth, dead = await oauth_proxy_servers_for(session_id, enabled_keys, user_id=user_id, db=db)
    return _to_opencode_mcp({**static, **oauth}), dead


def _detect_expired_aws(content: str | None, is_error: bool) -> bool:
    """Whether a FAILED tool result looks like expired/invalid AWS credentials.

    Gated on ``is_error`` so a successful command whose output merely mentions a signature
    (e.g. grepping logs for "ExpiredToken") can't trigger a spurious re-auth prompt.
    """
    return bool(is_error and content and any(s in content for s in _AWS_EXPIRY_SIGNATURES))


async def orchestrate_chat_opencode(
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
    """Broker one turn through the sandboxed ``opencode`` engine. Mirrors orchestrate_chat_cli."""
    if isinstance(session_id, uuid.UUID):
        session_id_str = str(session_id)
        session_uuid = session_id
    else:
        session_id_str = session_id
        session_uuid = uuid.UUID(session_id)

    logger.info(f"Starting opencode orchestration for session {session_id_str}")
    # kubeconfig_path/context_name are part of the uniform orchestrator contract but unused
    # here: opencode's bash reaches the cluster via KUBECONFIG in the projected connector env
    # (build_connector_env), not these args. attachments ride the durable workdir already.
    _ = (kubeconfig_path, context_name, attachments)

    settings = get_settings()
    if not settings.sandbox_enabled:
        yield error_event("opencode requires the sandbox runtime, which is disabled.")
        return

    # Prior turns decide resume: opencode keeps the working transcript in its own session
    # store, so we only pass the NEW message + `--continue` when history exists. On retry the
    # errored user row is already last; drop it so it isn't replayed as a fresh prompt too.
    try:
        history = await get_session_messages(db, session_id)
    except Exception as e:
        logger.error(f"Failed to load messages: {e}")
        yield error_event(f"Failed to load conversation history: {str(e)}")
        return
    if not persist_user_message and history and history[-1]["role"] == "user":
        history = history[:-1]
    resume = bool(history)

    if persist_user_message:
        try:
            await save_message(
                db, session_id, "user", user_message, attachments=attachments or None
            )
        except Exception as e:
            logger.error(f"Failed to save user message: {e}")
            yield error_event(f"Failed to save message: {str(e)}")
            return
        if not history:
            try:
                await ensure_session_title(db, session_id, user_message)
            except Exception as e:
                logger.warning(f"Failed to set session title: {e}")

    base_prompt = await resolve_session_system_prompt(db, session_id)
    system_prompt = get_system_prompt(cluster_name, namespaces, base_prompt=base_prompt)
    model = await resolve_session_model(db, session_id)

    # opencode selects the model with `-m amazon-bedrock/<id>` and resolves <id> against its
    # own models.dev catalog — it cannot target a raw ARN that way (that needs an
    # opencode.json provider.models mapping, deferred). An ARN-pinned custom row would be
    # silently misrouted, so fail loudly and tell the user to use a standard model or the
    # inkwest-agent engine (which honors entry.arn). Foundation/inference-profile ids
    # (incl. us./eu. regional prefixes) are catalog-resolvable and pass through fine.
    from app.services import model_registry

    entry = model_registry.get_entry(model)
    if entry and getattr(entry, "arn", None):
        yield error_event(
            f"Model '{model}' is ARN-pinned (provisioned/imported); the opencode engine "
            "does not yet support ARN models. Pick a standard Bedrock model, or run this "
            "session on the inkwest-agent engine."
        )
        return

    # Make the agent model/date-aware (opencode/Bedrock models aren't told their deployment
    # identity, and open-weight models routinely guess the year wrong). Same anchors as the
    # inkwest-agent broker; DATE (not instant) granularity keeps any future prompt cache stable.
    system_prompt += (
        "\n\n## Runtime identity\n"
        f"You are running as the **{model}** model on the Inkwest opencode engine "
        "(Amazon Bedrock). When asked which model or engine you are, answer with this."
    )
    today = datetime.now(UTC).date().isoformat()
    system_prompt += (
        f"\n\n## Current date\nToday's UTC date is **{today}** (turn start). Use it for the "
        "year/month in timestamps and relative ranges."
    )

    # Skills + MCP (resolved from the DB here; the agent is DB-free). Best-effort — a load
    # failure degrades to no-skills / no-MCP rather than failing the turn (same as the
    # inkwest-agent broker). Skills become an AGENTS.md index opencode reads on demand; MCP
    # servers are materialized into opencode.json's `mcp` block below.
    try:
        active_skills = await get_active_skills(db)
    except Exception as e:
        logger.warning("active_skills_load_failed", extra={"error": str(e)})
        active_skills = []
    try:
        enabled_mcp = await get_enabled_mcp_servers(db, session_id)
    except Exception as e:
        logger.warning("enabled_mcp_load_failed", extra={"error": str(e)})
        enabled_mcp = []

    # Skills as progressive disclosure: list name+description in AGENTS.md and point at the
    # SKILL.md path; opencode reads the body on demand via its native read/bash (no special
    # tool needed). Bodies are materialized into the sandbox below (apply_session_skills).
    if active_skills:
        from app.core.slug import slugify

        skills_root = skills_container_root()
        idx = "\n".join(
            f"- **{s.name}**: {(s.description or '').strip()} "
            f"— read `{skills_root}/{slugify(s.name)}/SKILL.md` for the full procedure."
            for s in active_skills
        )
        system_prompt += (
            "\n\n## Available skills\nRead a skill's SKILL.md on demand before applying it:\n" + idx
        )

    # Pre-provide connector creds as env (AWS profile paths, KUBECONFIG, GITLAB_TOKEN) — a
    # one-way push the agent's bash uses via `aws`/`kubectl`/`curl`. The Bedrock MODEL creds
    # (AWS_BEARER_TOKEN_BEDROCK) ride the container's own sandbox.env, not this env, so they
    # don't displace the connector profiles. Best-effort: a renewal hiccup degrades to no
    # connector env (model + bash still work) rather than failing the turn.
    connector_env: dict[str, str] = {}
    if user_id is not None:
        try:
            from app.tools.connector_exec import build_connector_env

            connector_env, injected = await build_connector_env(user_id, session_uuid)
        except Exception as e:
            logger.warning(f"Failed to project connector credentials: {e}")
            connector_env, injected = {}, []
        if injected:
            logger.info(
                "Projecting connector credentials to opencode",
                extra={"session_id": session_id_str, "connectors": injected},
            )

    container: str | None = None
    # On an abnormal exit (cancel / timeout / crash) we TEAR DOWN the conv container rather
    # than just releasing it: opencode's bash can spawn children (kubectl, long curls) that a
    # SIGKILL of opencode alone would orphan in the container. Killing the container reaps the
    # whole tree; durable state (workdir, opencode.db, .claude) lives on bind-mounts and
    # survives, so the next turn simply re-provisions. A normal turn keeps the container warm.
    abnormal = False
    try:
        from app.services import sandbox

        container = await sandbox.ensure(session_id_str)

        # Resume only if opencode's OWN session store actually survived. `--continue` resumes
        # the store in the durable per-conv mount; if Postgres has history but the store is
        # gone (replica without the volume / wiped dir / sandbox rebuilt before a turn ran),
        # `--continue` would resume an EMPTY session and silently drop all prior context. The
        # agent is never handed the history otherwise, so fall back to a fresh run instead.
        store_db = os.path.join(sandbox.session_dir(session_id_str), "opencode", "opencode.db")
        if resume and not os.path.isfile(store_db):
            logger.warning(
                "opencode session store missing; starting fresh despite DB history",
                extra={"session_id": session_id_str},
            )
            resume = False

        # Materialize this turn's skill bodies into the sandbox (claude/skills, mounted at
        # <home>/.claude/skills — readable by opencode). Same cadence/dir as the other engines.
        apply_session_skills(session_id_str, active_skills)

        # Security: prune any cred-bearing opencode.json orphaned by a PRIOR turn whose broker
        # was SIGKILLed before its finally cleanup ran (it can embed MCP headers/JWTs). Done
        # right after ensure so stale creds never linger; the fresh one is written just below.
        opencode_json = os.path.join(
            sandbox.session_dir(session_id_str), "workdir", "opencode.json"
        )
        with contextlib.suppress(FileNotFoundError):
            os.remove(opencode_json)

        # MCP servers for the turn (static catalog + OAuth2 proxy URLs carrying a mint-stable
        # JWT). dead = enabled OAuth2 servers with no active grant → tell the user to reconnect.
        mcp_block, mcp_dead = await _opencode_mcp_block(
            session_id_str, enabled_mcp, user_id=user_id, db=db
        )
        for dead_key in mcp_dead:
            yield mcp_auth_required_event(dead_key, session_id_str)

        # System prompt as AGENTS.md in the workdir (opencode reads it from --dir). Rewritten
        # every turn so cluster/namespace context + the date anchor stay current. Not secret.
        workdir = os.path.join(sandbox.session_dir(session_id_str), "workdir")
        os.makedirs(workdir, exist_ok=True)
        with open(os.path.join(workdir, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write(system_prompt + "\n")

        # opencode.json (auto-loaded from --dir). Least-privilege tool surface: disable the
        # network-egress tools the investigation agent doesn't need (websearch has no provider
        # here; webfetch would egress) — defence in depth on top of the squid allowlist.
        # Permissions stay implicit-allow via --dangerously-skip-permissions (no TTY for "ask").
        # The `mcp` block can embed creds/JWT headers → this file is removed on turn end (finally).
        opencode_cfg: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "tools": {"websearch": False, "webfetch": False},
        }
        if mcp_block:
            opencode_cfg["mcp"] = mcp_block
        with open(opencode_json, "w", encoding="utf-8") as f:
            json.dump(opencode_cfg, f)

        # Reconstruct role-correct messages from the flat event stream so the persisted
        # history round-trips (toolResult lives in a user turn, not the assistant one). A
        # pending tool_result batch is flushed when the next assistant content arrives.
        assistant_blocks: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []
        received_terminal = False
        # The parser tags a stream-end ``done`` with no genuine ``stop`` as ``truncated``
        # (max steps / killed / crash). We persist its usage but DON'T let it mask a non-zero
        # process exit: on a truncated turn the RuntimeError below still surfaces as an error.
        truncated = False
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
            output_stream = execute_opencode(
                session_id_str,
                user_message,
                container=container,
                model=model,
                resume=resume,
                docker_path=settings.sandbox_docker_path,
                extra_env=connector_env or None,
                timeout=settings.claude_cli_timeout,
                cancel_event=cancel_event,
            )
            async for event in parse_opencode_response(output_stream):
                if event.type == "text_delta" and event.content:
                    if pending_results:
                        await _flush()
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
                    metrics.record_tool_call(event.tool_name or "unknown", event.is_error)
                    pending_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": event.tool_use_id,
                            "content": event.content,
                            "is_error": event.is_error,
                        }
                    )
                    # Best-effort re-auth affordance: opencode's bash `aws` has no structured
                    # connector tool, so surface "Give access" when a failed result smells of
                    # expired AWS creds (parity with the connector engines' NEEDS_AUTH event).
                    if _detect_expired_aws(event.content, event.is_error):
                        yield event  # stream the tool_result first
                        yield connector_required_event("aws", session_id_str)
                        continue
                elif event.type in ("done", "error"):
                    if event.type == "done":
                        done_meta = event.metadata or {}
                        if done_meta.get("truncated"):
                            # On cancel this is the kill artifact — drop it; the cancel
                            # branch below emits the single terminal event. Otherwise mark
                            # truncated so a non-zero exit still surfaces as an error.
                            if cancel_event and cancel_event.is_set():
                                continue
                            truncated = True
                    received_terminal = True
                yield event

            if cancel_event and cancel_event.is_set():
                # Cancel = the in-container opencode was SIGKILLed mid-turn: assistant_blocks
                # may hold a tool_use whose tool_result never arrived. Persisting that leaves a
                # dangling tool_use that breaks the NEXT turn's history — so discard the partial
                # (skip _flush) and emit a single terminal stopped done. (Do NOT move _flush
                # above this check.) Mirrors orchestrate_chat_inkwest_agent / orchestrate_chat_cli.
                logger.info("opencode chat cancelled by user")
                abnormal = True  # tear down → reap any opencode-spawned children
                yield done_event(stopped=True)
                return

            await _flush()
        except (RuntimeError, TimeoutError) as e:
            # Surface when nothing genuinely terminal came through (silent crash) OR the turn
            # was truncated — a non-zero exit after a truncated stream is a real failure
            # (bad creds, model-not-found, squid block) whose reason must reach the user.
            abnormal = True
            if not received_terminal or truncated:
                logger.error(f"opencode failed: {e}")
                yield error_event(str(e))
            return

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
                # opencode reports an authoritative per-turn USD cost (cache-aware) — pass it
                # through so we don't recompute a wrong figure from token×rate. Guard the parse:
                # a malformed value degrades to the token estimate rather than failing the turn.
                oc_cost = done_meta.get("cost_usd")
                cost_override: Decimal | None = None
                if oc_cost:
                    try:
                        cost_override = Decimal(str(oc_cost))
                    except (InvalidOperation, ValueError):
                        cost_override = None
                await record_usage(
                    db,
                    user_id=user_id,
                    session_id=session_uuid,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    model=model,
                    cost_usd=cost_override,
                )
                await db.commit()
            except Exception as e:
                logger.error(f"Failed to persist session stats/usage: {e}")
                # A failed commit leaves the session in an aborted transaction; roll back so
                # the next DB op on this session doesn't error (parity with finalize_turn).
                try:
                    await db.rollback()
                except Exception:
                    pass

        if not received_terminal:
            yield done_event()

    except Exception as e:
        logger.exception("Unexpected error in opencode orchestration")
        abnormal = True
        yield error_event(f"Unexpected error: {str(e)}")

    finally:
        from app.services import sandbox

        # Remove the cred-bearing opencode.json (MCP headers/JWT) — must not outlive the turn.
        with contextlib.suppress(Exception):
            os.remove(os.path.join(sandbox.session_dir(session_id_str), "workdir", "opencode.json"))
        if container is not None:
            if abnormal:
                # Kill the container pair so any opencode-spawned child processes die with it;
                # keep the durable dirs (remove_dir=False) so the next turn re-provisions clean.
                with contextlib.suppress(Exception):
                    await sandbox.teardown(session_id_str, remove_dir=False)
            else:
                await sandbox.release(session_id_str)
