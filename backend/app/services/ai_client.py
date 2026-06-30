"""
Orchestrator dispatch for Inkwest.

A single engine-dispatched path: ``get_orchestrator`` resolves the turn's engine
from the session's model (plus any explicit Engine·Model pin) and routes to the
sandboxed inkwest-agent (custom Bedrock Converse loop) or the Claude Code CLI. The
inference backend is owned by the CLI/sandbox configuration, not by a backend-level
provider setting.
"""

import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.models.session import Session
from app.services.model_registry import (
    configured_default_id,
    default_model,
    selectable_models,
)

logger = logging.getLogger(__name__)


def get_model_name() -> str:
    """
    Get the default model name to use: the admin-selected default when valid
    for the current provider, otherwise the provider's env-configured default.
    """
    return default_model()


async def resolve_session_model_choice(db: AsyncSession, session_id: "str | UUID") -> str | None:
    """
    The explicit model choice for a chat turn: the session's own pick, else the
    admin-set default — each honored only while in the admin-ENABLED subset of
    the current provider's catalog. Checking enabled (not just available) is
    what makes an admin "disable" actually bite: a session pinned to a model
    the admin later unchecks falls back to the default from its next turn.
    None means nobody chose: the engine's own default applies (catalog default
    for the API, the CLI's own configuration for claude-cli).
    """
    selectable = selectable_models()
    session_model = await db.scalar(select(Session.model).where(Session.id == session_id))
    if session_model:
        if session_model in selectable:
            return session_model
        logger.warning(
            f"Session {session_id} pinned to model {session_model} which is no longer "
            "enabled/available; using the default"
        )
    override = configured_default_id()
    if override and override in selectable:
        return override
    return None


async def resolve_session_model(db: AsyncSession, session_id: "str | UUID") -> str:
    """Resolve the model for an API chat turn: explicit choice or the default."""
    return await resolve_session_model_choice(db, session_id) or default_model()


async def resolve_session_engine(db: AsyncSession, session_id: "str | UUID") -> str | None:
    """The session's explicit engine pin, if any (NULL = resolve per turn).

    Validity against the enabled set + the model's runnable engines is decided at
    dispatch (``engine_for_turn``), so a stale pin degrades gracefully rather than
    erroring here.
    """
    return await db.scalar(select(Session.engine).where(Session.id == session_id))


def get_orchestrator():
    """
    Return the engine-dispatched orchestrator.

    Engine is resolved per turn from the session's model (and any explicit
    Engine·Model pin), decoupled from the inference backend. A turn routes to the
    sandboxed inkwest-agent (custom Bedrock Converse loop) when that engine is
    enabled and a sandbox is available; otherwise to the Claude Code CLI — its
    native path, and the dev-local fallback that needs no sandbox.

    Returns:
        Orchestrator function
    """
    # Engine is decoupled from the inference backend: the default deployment can
    # additionally enable the sandboxed inkwest-agent (custom Converse loop). Resolve
    # the engine per turn from the session's model — a non-Claude model needs the
    # agent (the CLI is Claude-only). Claude models stay on the CLI (its native path).
    from app.services.engines import (
        CLAUDE_CLI,
        INKWEST_AGENT,
        capable_engines,
        configured_default_engine,
        engine_for_turn,
        inkwest_agent_enabled,
    )
    from app.services.events import error_event

    async def _dispatch_engine(
        db: AsyncSession, session_id: "str | UUID", **kwargs: Any
    ) -> AsyncIterator[Any]:
        model = await resolve_session_model(db, session_id)
        # Honor the session's explicit engine pin when set (the Engine·Model picker,
        #) — a Claude model can then run through the sandboxed Converse loop
        # too, not only the CLI. With no session pin, fall back to the admin's chosen
        # default engine for the default model; otherwise engine_for_turn applies the
        # default rule (non-Claude → agent; Claude → native CLI), reconciled against the
        # registry's provider tag. Resolved fresh per turn so a mid-session switch /
        # admin change takes effect without a dispatcher rebuild.
        pinned = await resolve_session_engine(db, session_id)
        if pinned is None and model == default_model():
            pinned = configured_default_engine()
        engine = engine_for_turn(model, pinned)
        agent_runnable = inkwest_agent_enabled() and get_settings().sandbox_enabled
        # Route to the broker only when the agent can actually run (engine enabled +
        # sandbox available); otherwise fall back to the CLI.
        if engine == INKWEST_AGENT and agent_runnable:
            from app.services.orchestrator_inkwest_agent import (
                orchestrate_chat_inkwest_agent,
            )

            logger.info(f"Engine inkwest-agent (sandboxed) for model {model}")
            orchestrate = orchestrate_chat_inkwest_agent
            used_engine = INKWEST_AGENT
        elif engine == INKWEST_AGENT and CLAUDE_CLI not in capable_engines(model):
            # The model needs the inkwest-agent engine (the CLI is Claude-only) but the agent
            # isn't runnable here (engine disabled or sandbox unavailable). Do NOT fall back to
            # the CLI — it can't serve this model, the turn would fail there, and the usage
            # would be misattributed to claude-cli. Surface a clean error instead.
            logger.warning(
                "inkwest-agent unavailable for non-Claude model %s (agent_enabled=%s sandbox=%s)",
                model,
                inkwest_agent_enabled(),
                get_settings().sandbox_enabled,
            )
            yield error_event(
                f"The model '{model}' requires the inkwest-agent engine, which is "
                "currently unavailable. Ask an administrator to enable it, or switch to "
                "a Claude model.",
                metadata={"model": model, "engine": INKWEST_AGENT, "reason": "engine_unavailable"},
            )
            return
        else:
            from app.services.orchestrator_cli import orchestrate_chat_cli

            orchestrate = orchestrate_chat_cli
            used_engine = CLAUDE_CLI
        metrics.set_turn_engine(used_engine)
        metrics.set_turn_model(model)
        metrics.chat_turns_total.labels(engine=used_engine, model=model).inc()
        async for event in orchestrate(db=db, session_id=session_id, engine=used_engine, **kwargs):
            yield event

    logger.info("Using engine-dispatched orchestrator")
    return _dispatch_engine
