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
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.services import engines, llm_broker
from app.services.ai_cli_client import execute_opencode, parse_opencode_response
from app.services.ai_client import resolve_session_model
from app.services.events import (
    ChatEvent,
    connector_required_event,
    done_event,
    error_event,
    mcp_auth_required_event,
)
from app.services.messages import (
    ensure_session_title,
    get_enabled_mcp_servers,
    get_session_messages,
    save_message,
)
from app.services.prompts import get_system_prompt, resolve_session_system_prompt
from app.services.session_skills import apply_session_skills, skills_container_root
from app.services.turn_usage import persist_turn_usage
from app.services.user_preferences import get_effective_skills

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

    Reuses the SAME inputs as the claude-cli / inkwest-agent engines: static catalog
    servers are BROKERED via :func:`build_session_static_servers` (HTTP servers become
    the in-app ``/api/mcp-proxy/{key}/`` URL + a mint-stable internal JWT, so their real
    headers/API key never enter the sandbox; stdio stays verbatim; no proxy base or no
    user ⇒ verbatim fallback, same as the siblings) + OAuth2 proxy servers. ``dead_keys``
    are enabled OAuth2 servers with no active grant (surfaced as ``mcp_auth_required``).
    """
    if not enabled_keys:
        return {}, []
    from app.services.mcp_config import build_session_static_servers, oauth_proxy_servers_for

    static = build_session_static_servers(
        enabled_keys,
        base=get_settings().mcp_proxy_base_url,
        user_id=user_id,
        session_id=session_id,
        existing={},
    )
    oauth, dead = await oauth_proxy_servers_for(session_id, enabled_keys, user_id=user_id, db=db)
    return _to_opencode_mcp({**static, **oauth}), dead


def _detect_expired_aws(content: str | None, is_error: bool) -> bool:
    """Whether a FAILED tool result looks like expired/invalid AWS credentials.

    Gated on ``is_error`` so a successful command whose output merely mentions a signature
    (e.g. grepping logs for "ExpiredToken") can't trigger a spurious re-auth prompt.
    """
    return bool(is_error and content and any(s in content for s in _AWS_EXPIRY_SIGNATURES))


def _publish_workdir_file(workdir: str, filename: str, content: str) -> None:
    """Symlink-safe publish of ``content`` to ``<workdir>/<filename>`` in the bind mount.

    The workdir is world-writable and bind-mounted into the sandbox, so a prior turn's
    (untrusted) agent can plant a symlink at the target — ``ln -s <backend-host-path>
    <workdir>/AGENTS.md``. A naive ``open(target, "w")`` FOLLOWS that link and truncates
    the host file (a write-escape out of the sandbox). Instead stage on a sibling OUTSIDE
    the mount, then ``os.replace`` into place: rename REPLACES a symlink sitting at the
    destination (it operates on the link, never its target), so the write can't be
    redirected. A bind mount follows the inode, so renaming a file INTO the dir is
    path-resolved and the warm container sees it. Same discipline as write_aws_profiles.
    """
    dest = os.path.join(workdir, filename)
    staging = os.path.join(os.path.dirname(workdir), f".{filename}.{uuid.uuid4().hex}")
    try:
        with open(staging, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(staging, dest)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(staging)


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
    # (build_connector_env), not these args. attachments ride the durable workdir already
    # (attachments.py); the list itself is only consulted to detect an attachments-only turn.
    _ = (kubeconfig_path, context_name)

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
        if not history:
            try:
                await ensure_session_title(db, session_id, user_message)
            except Exception as e:
                logger.warning(f"Failed to set session title: {e}")

    base_prompt = await resolve_session_system_prompt(db, session_id)
    # With no statically-configured cluster, surface the EKS clusters discovered from the
    # user's AWS access (same as the inkwest-agent path): KUBECONFIG in the projected
    # connector env already points at the generated contexts, so the base prompt must not
    # claim "no Kubernetes access". opencode drives kubectl via bash, so tell it to use
    # `kubectl --context <name>` rather than the connector engines' `context` tool arg.
    from app.tools.connector_exec import session_kube_contexts

    eks_contexts = session_kube_contexts(session_uuid) if kubeconfig_path is None else []
    effective_cluster_name = cluster_name
    if cluster_name is None and eks_contexts:
        effective_cluster_name = f"EKS via AWS access ({len(eks_contexts)} cluster(s))"
    system_prompt = get_system_prompt(effective_cluster_name, namespaces, base_prompt=base_prompt)
    if eks_contexts:
        listed = "\n".join(f"- {c}" for c in eks_contexts)
        system_prompt += (
            "\n\n## Kubernetes (EKS) access\n"
            "kubectl is available for the EKS clusters discovered from your AWS access — "
            "KUBECONFIG is already set with one context per cluster. Use "
            "`kubectl --context <name>` to target a specific cluster; omit it for the "
            "current one. Do NOT run `aws eks update-kubeconfig` — the contexts are "
            "already configured. Available contexts:\n" + listed
        )
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
    # Registry max_tokens override: opencode has no per-run output-token knob we can drive
    # (the agent engine honors it via its Converse call) — surface the mismatch in logs
    # rather than silently ignoring the admin's setting.
    if entry and isinstance((getattr(entry, "options", None) or {}).get("max_tokens"), int):
        logger.warning(
            "Registry max_tokens override for %s is not honored by the opencode engine", model
        )

    # Make the agent model/date-aware (opencode/Bedrock models aren't told their deployment
    # identity, and open-weight models routinely guess the year wrong). Same anchors as the
    # inkwest-agent broker; DATE (not instant) granularity keeps any future prompt cache stable.
    display_model = (getattr(entry, "label", None) or model) if entry else model
    system_prompt += (
        "\n\n## Runtime identity\n"
        f"You are running as the **{display_model}** model on the Inkwest opencode engine "
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
        # Effective (admin ∩ per-user) skills, matching the other engines — a per-user
        # disable / personal skill must apply identically regardless of engine.
        active_skills = await get_effective_skills(db, user_id)
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
        from app.tools.connector_exec import build_connector_env, connector_prompt_hint

        try:
            connector_env, injected = await build_connector_env(user_id, session_uuid)
        except Exception as e:
            logger.warning(f"Failed to project connector credentials: {e}")
            connector_env, injected = {}, []
        if injected:
            logger.info(
                "Projecting connector credentials to opencode",
                extra={"session_id": session_id_str, "connectors": injected},
            )
            # Tell the model the access exists (parity with the cli engine's hint / the
            # agent's tool defs) — env alone leaves it claiming it has no AWS/GitLab.
            system_prompt += connector_prompt_hint(injected)

    container: str | None = None
    # Path to the cred-bearing config; declared here so the finally can remove it even if the
    # turn throws between ensure() and the write (None ⇒ nothing was published yet).
    opencode_json: str | None = None
    # LLM auth broker grant + base. Declared here so the finally always burns the grant;
    # MINTED inside the try (below) so a failure can never leak a grant the finally can't see.
    broker_env: dict[str, str] = {}
    broker_base: str | None = None
    # On an abnormal exit (cancel / timeout / crash) we TEAR DOWN the conv container rather
    # than just releasing it: opencode's bash can spawn children (kubectl, long curls) that a
    # SIGKILL of opencode alone would orphan in the container. Killing the container reaps the
    # whole tree; durable state (workdir, opencode.db, .claude) lives on bind-mounts and
    # survives, so the next turn simply re-provisions. A normal turn keeps the container warm.
    abnormal = False
    # Accounting state, bound BEFORE the try so the outer error handler can persist
    # whatever accumulated (an errored turn's completed Bedrock calls are still billed).
    received_terminal = False
    done_meta: dict[str, Any] = {}
    turn_id = uuid.uuid4()
    usage_persisted = False
    usage_message_id: uuid.UUID | None = None

    async def _persist_accumulated_usage() -> None:
        """Idempotent per-turn handoff to the shared persist_turn_usage (see its module
        contracts); called from success, every error handler AND the user-stop path."""
        nonlocal usage_persisted
        if usage_persisted:
            return
        usage_persisted = True
        in_tokens = done_meta.get("input_tokens", 0)
        out_tokens = done_meta.get("output_tokens", 0)
        # opencode reports an authoritative per-turn USD cost (cache-aware) — pass it
        # through so we don't recompute a wrong figure from token×rate. Guard the parse:
        # a malformed value degrades to the shared helper's cache-aware token×rate
        # estimate. Truthy check on purpose: opencode reports 0.0 when models.dev has no
        # pricing for the model (a genuinely free turn doesn't exist — output tokens
        # always bill), so zero means "unknown" → estimate. Negative/malformed fall back.
        oc_cost = done_meta.get("cost_usd")
        cost_override: Decimal | None = None
        if oc_cost:
            try:
                cost_override = Decimal(str(oc_cost))
            except (InvalidOperation, ValueError):
                cost_override = None
            if cost_override is not None and cost_override < 0:
                cost_override = None
        await persist_turn_usage(
            db,
            session_id=session_id,
            session_uuid=session_uuid,
            user_id=user_id,
            model=model,
            turn_id=turn_id,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cache_read_tokens=done_meta.get("cache_read_tokens", 0),
            cache_write_tokens=done_meta.get("cache_write_tokens", 0),
            # No cache_write_1h_tokens: opencode's tokens.cache carries no TTL split, so
            # the 1h portion is unknowable here — the fallback estimate prices all cache
            # writes at the 5m rate. Moot whenever opencode reports its authoritative
            # cache-aware cost_usd (the normal case, passed through below).
            tool_calls=done_meta.get("tool_calls", 0),
            iterations=done_meta.get("iterations", 1),
            skip_budget_row=bool(broker_env),
            cost_usd=cost_override,
            # Turn's FIRST assistant row — see stamp_message_usage for the fusion rationale.
            stamp_message_id=usage_message_id,
        )

    try:
        from app.services import sandbox

        uid = str(user_id) if user_id else None
        # Per-user admission control before provisioning, same as the cli/inkwest-agent
        # engines: reap the user's oldest idle pairs, refuse if at the hard ceiling with
        # nothing reclaimable. Without this opencode turns could provision past the cap.
        try:
            await sandbox.enforce_user_sandbox_cap(uid, session_id_str)
        except sandbox.SandboxCapExceededError as e:
            yield error_event(str(e))
            return
        container = await sandbox.ensure(session_id_str, uid)

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

        # LLM auth broker (llm_broker_enabled): route opencode's model call through the backend
        # proxy. The provider gets the broker base as `baseURL` (it appends /model/{id}/{op},
        # the broker route); the per-turn grant rides the env as AWS_BEARER_TOKEN_BEDROCK,
        # injected via docker exec -e so it overrides the env-file value for opencode's process.
        # With the broker on, sandbox.env drops the real key (see sandbox.env.example / docs), so
        # the only Bedrock token in the sandbox is this ephemeral grant — burned at turn end
        # (finally). Off ⇒ empty/None ⇒ opencode uses the sandbox.env key direct (today's behavior).
        # One turn_id for every usage row this turn produces (broker grant + engine fallback).
        broker_env = llm_broker.agent_broker_env(
            user_id, session_uuid, engine=engines.OPENCODE, turn_id=turn_id
        )
        broker_base = llm_broker.agent_bedrock_endpoint_url()

        # The broker is fail-closed: with NO enabled registry models nothing is invocable,
        # and the turn would die on an opaque mid-stream 403 from inside opencode. Surface
        # it as a clear config error instead (parity with the claude-cli broker path).
        if broker_env:
            if not any(e.enabled for e in model_registry.merged_entries()):
                yield error_event(
                    "No models are enabled for the LLM broker. Enable at least one model "
                    "in admin settings before using the broker."
                )
                return

        # Materialize this turn's skill bodies into the sandbox (claude/skills, mounted at
        # <home>/.claude/skills — readable by opencode). Same cadence/dir as the other engines.
        apply_session_skills(session_id_str, active_skills)

        # Single source of truth for the workdir paths (reused in the finally cleanup).
        # Assigned before any await that could throw so the finally — guarded on
        # `container is not None`, set at ensure() above — can always reference them.
        workdir = os.path.join(sandbox.session_dir(session_id_str), "workdir")
        os.makedirs(workdir, exist_ok=True)
        opencode_json = os.path.join(workdir, "opencode.json")  # noqa: F841 — used in finally

        # Security: prune any cred-bearing opencode.json orphaned by a PRIOR turn whose broker
        # was SIGKILLed before its finally cleanup ran (it can embed MCP headers/JWTs). Done
        # right after ensure so stale creds never linger; the fresh one is written just below.
        # islink-unlink first: a planted symlink at the target must not survive the prune (it
        # would be re-followed by a naive reader), and os.remove on a link removes the link.
        with contextlib.suppress(FileNotFoundError):
            os.remove(opencode_json)
        # Security: also prune the project-level `.opencode/` tree (agents/commands/plugins
        # auto-loaded from --dir). A prompt-injected turn could drop persistence there that
        # re-executes on every later turn of the conversation; nothing legitimate writes it.
        # (`--pure` already blocks plugin loading; this clears prompt-level extensions too.)
        # rmtree REFUSES a symlink root (and ignore_errors would swallow that): unlink a
        # planted `.opencode -> elsewhere` link first or the prune is bypassable.
        dot_opencode = os.path.join(workdir, ".opencode")
        if os.path.islink(dot_opencode):
            with contextlib.suppress(OSError):
                os.unlink(dot_opencode)
        shutil.rmtree(dot_opencode, ignore_errors=True)

        # MCP servers for the turn (static catalog + OAuth2 proxy URLs carrying a mint-stable
        # JWT). dead = enabled OAuth2 servers with no active grant → tell the user to reconnect.
        mcp_block, mcp_dead = await _opencode_mcp_block(
            session_id_str, enabled_mcp, user_id=user_id, db=db
        )
        for dead_key in mcp_dead:
            yield mcp_auth_required_event(dead_key, session_id_str)

        # System prompt as AGENTS.md in the workdir (opencode reads it from --dir). Rewritten
        # every turn so cluster/namespace context + the date anchor stay current. Not secret,
        # but published symlink-safe: the workdir is sandbox-writable, so a planted
        # `AGENTS.md -> <host-path>` symlink must not redirect this write off the sandbox.
        _publish_workdir_file(workdir, "AGENTS.md", system_prompt + "\n")

        # opencode.json (auto-loaded from --dir). Least-privilege tool surface: disable the
        # network-egress tools the investigation agent doesn't need (websearch has no provider
        # here; webfetch would egress) — defence in depth on top of the squid allowlist.
        # `skill` (opencode's native skill tool) is disabled too: Inkwest surfaces skills as an
        # AGENTS.md index the agent reads on demand via `read` (progressive disclosure), so the
        # native tool — which can't resolve our `~/.claude/skills` layout — only errors.
        # Permissions stay implicit-allow via --dangerously-skip-permissions (no TTY for "ask").
        # The `mcp` block can embed creds/JWT headers → this file is removed on turn end (finally).
        opencode_cfg: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            # Never self-update: the image pins the exact version the NDJSON parser was
            # validated against (agent/Dockerfile OPENCODE_VERSION); a silent runtime
            # update would defeat the pin (wire-format drift + unpinned code execution).
            "autoupdate": False,
            "tools": {"websearch": False, "webfetch": False, "skill": False},
            # Defensive pin: opencode's built-in amazon-bedrock small model is claude-haiku,
            # which isn't an enabled registry model → the broker (fail-closed) would 403 any
            # use. Point it at the SESSION model (enabled/brokered). It's effectively unused
            # since the title/summary sub-agents below are disabled, but this keeps any residual
            # small_model use off the un-enabled default.
            "small_model": f"amazon-bedrock/{model}",
            # Disable opencode's auxiliary small-model sub-agents (title + summary). They run
            # OUTSIDE the foreground `run` stream, so their Bedrock spend is NOT captured in the
            # turn's usage/cost (the broker doesn't meter, and the parser only sees `run` steps)
            # — unaccounted cost. Inkwest titles backend-side, so these earn nothing here.
            # (Context compaction runs mid-`run`, so its cost IS metered → left enabled.)
            "agent": {"title": {"disable": True}, "summary": {"disable": True}},
        }
        if mcp_block:
            opencode_cfg["mcp"] = mcp_block
        # LLM broker on: point opencode's amazon-bedrock provider at the proxy base. opencode
        # (AI SDK) appends /model/{id}/{op} to baseURL — the broker route shape. The grant
        # (AWS_BEARER_TOKEN_BEDROCK) rides the spawn env below. Off ⇒ no baseURL ⇒ direct Bedrock.
        if broker_base:
            opencode_cfg["provider"] = {"amazon-bedrock": {"options": {"baseURL": broker_base}}}
        # Atomic, symlink-safe publish into the bind-mounted workdir (a half-written
        # cred-bearing file must be invisible to the container, and a planted symlink at the
        # target must not redirect the write): stage on a sibling outside the mount, os.replace
        # in. Same helper/discipline as the AGENTS.md write above.
        _publish_workdir_file(workdir, "opencode.json", json.dumps(opencode_cfg))

        # Reconstruct role-correct messages from the flat event stream so the persisted
        # history round-trips (toolResult lives in a user turn, not the assistant one). A
        # pending tool_result batch is flushed when the next assistant content arrives.
        assistant_blocks: list[dict[str, Any]] = []
        pending_results: list[dict[str, Any]] = []
        aws_reauth_sent = False

        async def _flush() -> None:
            nonlocal assistant_blocks, pending_results, usage_message_id
            if assistant_blocks:
                try:
                    saved = await save_message(
                        db, session_id, "assistant", assistant_blocks, model=model, engine=engine
                    )
                    # Track the turn's FIRST assistant row: the persist helper stamps the
                    # token totals onto it (see stamp_message_usage for the fusion WHY).
                    if usage_message_id is None:
                        usage_message_id = saved.id
                except Exception as e:
                    logger.error(f"Failed to save assistant message: {e}")
            if pending_results:
                try:
                    await save_message(db, session_id, "tool_result", pending_results)
                except Exception as e:
                    logger.error(f"Failed to save tool results: {e}")
            assistant_blocks = []
            pending_results = []

        # opencode takes the prompt as argv and ignores the attachments list, but uploaded
        # files already live in /workdir (attachments.py). ALWAYS surface the filenames
        # (parity with the cli's @/workdir refs / the agent's AttachmentSpec) — "summarize
        # the attached file" must not leave the agent guessing what/where the upload is;
        # an attachments-only turn would otherwise send an empty prompt and silently no-op.
        agent_message = user_message
        if attachments:
            names = ", ".join(f"/workdir/{a['filename']}" for a in attachments if a.get("filename"))
            if not agent_message.strip():
                agent_message = (
                    f"Please review the attached file(s): {names}"
                    if names
                    else "Please review the attached file(s) in /workdir."
                )
            elif names:
                agent_message += f"\n\n(Attached file(s) for this message: {names})"

        try:
            output_stream = execute_opencode(
                session_id_str,
                agent_message,
                container=container,
                model=model,
                resume=resume,
                docker_path=settings.sandbox_docker_path,
                extra_env={**connector_env, **broker_env} or None,
                timeout=settings.claude_cli_timeout,
                cancel_event=cancel_event,
            )
            async for event in parse_opencode_response(output_stream):
                # A terminal event (done/error) already reached the client and closed the
                # turn's persisted state. The parser keeps DRAINING the stream after an error
                # (so execute_opencode's cleanup runs deterministically), which can surface
                # stray post-error text/tool parts — ignore them: don't re-yield, don't
                # persist a trailing assistant message, don't re-fire connector_required.
                if received_terminal and event.type not in ("done", "error"):
                    continue
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
                    # Once per turn — an agent retrying `aws` must not spam the banner.
                    if not aws_reauth_sent and _detect_expired_aws(event.content, event.is_error):
                        aws_reauth_sent = True
                        yield event  # stream the tool_result first
                        yield connector_required_event("aws", session_id_str)
                        continue
                elif event.type in ("done", "error"):
                    if event.type == "done":
                        done_meta = event.metadata or {}
                        # A truncated done is the kill artifact on cancel — drop it; the
                        # cancel branch below emits the single terminal event.
                        if done_meta.get("truncated") and cancel_event and cancel_event.is_set():
                            continue
                    else:
                        # In-container opencode's state after an error is unknown (it may
                        # still be running) → abnormal, so the finally tears the container
                        # down instead of releasing it warm with live connector creds. The
                        # parser keeps the error's accumulated usage in its metadata so a
                        # failed turn's Bedrock spend is still recorded below.
                        abnormal = True
                        if event.metadata:
                            done_meta = event.metadata
                    received_terminal = True
                yield event

            if cancel_event and cancel_event.is_set() and not received_terminal:
                # Cancel = the in-container opencode was SIGKILLed mid-turn: assistant_blocks
                # may hold a tool_use whose tool_result never arrived. Persisting that leaves a
                # dangling tool_use that breaks the NEXT turn's history — so discard the partial
                # (skip _flush) and emit a single terminal stopped done. (Do NOT move _flush
                # above this check.) Mirrors orchestrate_chat_inkwest_agent / orchestrate_chat_cli.
                # Gated on received_terminal: a Stop racing a GENUINE stop must not discard a
                # completed turn (its done already reached the client) nor double-terminate.
                logger.info("opencode chat cancelled by user")
                abnormal = True  # tear down → reap any opencode-spawned children
                # Persist whatever usage reached us before the kill (no-op when nothing
                # terminal arrived) — AWS bills the completed calls.
                await _persist_accumulated_usage()
                yield done_event(stopped=True)
                return

            await _flush()
        except (RuntimeError, TimeoutError) as e:
            # A non-zero exit / timeout raises INSIDE the parser's async-for, before its
            # trailing truncated-done can run — so a caught exception here means the stream
            # was cut. Surface it when nothing terminal reached the user (silent crash: bad
            # creds, model-not-found, squid block); when a terminal error/stop DID come
            # through, the user already has the outcome — fall through so the usage
            # accumulated up to the failure still gets recorded.
            abnormal = True
            if not received_terminal:
                logger.error(f"opencode failed: {e}")
                yield error_event(str(e))
                return
            logger.warning(f"opencode exited abnormally after a terminal event: {e}")
            await _flush()  # persist what streamed before the failure (pairs can't dangle)

        await _persist_accumulated_usage()

        if not received_terminal:
            yield done_event()

    except Exception as e:
        logger.exception("Unexpected error in opencode orchestration")
        abnormal = True
        await _persist_accumulated_usage()
        yield error_event(f"Unexpected error: {str(e)}")

    finally:
        from app.services import sandbox

        # Burn this turn's broker grant: the injected AWS_BEARER_TOKEN_BEDROCK is dead the moment
        # the turn ends, bounding a leaked grant to the turn. No-op when the broker was off.
        llm_broker.burn_env(broker_env)
        if container is not None:
            # Drop OUR lease first: the runner frees the session slot on the terminal event
            # while this tail still runs, so a NEXT turn may already hold the pair — shared
            # state (the config file, the container itself) then belongs to it, not us.
            with contextlib.suppress(Exception):
                await sandbox.release(session_id_str)
            if opencode_json is not None and not sandbox.in_use(session_id_str):
                # Remove the cred-bearing opencode.json (MCP JWTs) — must not outlive the
                # turn. Skipped when a next turn owns the pair: that turn wrote its OWN
                # config (after pruning ours) and removes it in its own finally.
                with contextlib.suppress(Exception):
                    os.remove(opencode_json)
                if abnormal:
                    # Kill the container pair so any opencode-spawned child processes die
                    # with it; keep the durable dirs (remove_dir=False) so the next turn
                    # re-provisions clean. only_if_idle re-checks the lease under the
                    # sandbox lock — never kills a container a new turn just re-leased.
                    with contextlib.suppress(Exception):
                        await sandbox.teardown(session_id_str, remove_dir=False, only_if_idle=True)
        # container None (turn died before ensure): the config is written after ensure,
        # so there is nothing to clean.
