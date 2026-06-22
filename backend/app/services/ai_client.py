"""
AI client factory for Inkwest.

Supports two AI provider families:
- claude-cli: Claude CLI subprocess (default, no API costs)
- anthropic / bedrock: Direct Anthropic API or AWS Bedrock (Claude models)
"""

import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, TypeAlias
from uuid import UUID

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.models.session import Session
from app.services.model_registry import (
    configured_default_id,
    default_model,
    get_entry,
    selectable_models,
)

logger = logging.getLogger(__name__)

AnthropicClient: TypeAlias = AsyncAnthropic | AsyncAnthropicBedrock


class AIClientError(Exception):
    """Raised when AI client initialization fails."""

    pass


def get_ai_client() -> AnthropicClient:
    """
    Get configured Anthropic-compatible AI client based on settings.

    Returns:
        AsyncAnthropic or AsyncAnthropicBedrock client instance

    Raises:
        AIClientError: If configuration is invalid or provider is not Anthropic-compatible
    """
    settings = get_settings()

    if settings.ai_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise AIClientError(
                "ANTHROPIC_API_KEY is required when AI_PROVIDER=anthropic. "
                "Please set the ANTHROPIC_API_KEY environment variable."
            )
        logger.info("Initializing Anthropic client with API key")
        return AsyncAnthropic(api_key=settings.anthropic_api_key)

    elif settings.ai_provider == "bedrock":
        if not settings.aws_region:
            raise AIClientError(
                "AWS_REGION is required when AI_PROVIDER=bedrock. "
                "Please set the AWS_REGION environment variable."
            )
        logger.info(f"Initializing Bedrock client in region {settings.aws_region}")

        # Build kwargs for Bedrock client
        bedrock_kwargs = {"aws_region": settings.aws_region}

        # Add explicit credentials if provided (otherwise uses default credential chain)
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            bedrock_kwargs["aws_access_key"] = settings.aws_access_key_id
            bedrock_kwargs["aws_secret_key"] = settings.aws_secret_access_key
            logger.debug("Using explicit AWS credentials for Bedrock")
        else:
            logger.debug("Using default AWS credential chain for Bedrock")

        return AsyncAnthropicBedrock(**bedrock_kwargs)

    else:
        raise AIClientError(
            f"Unknown AI provider: {settings.ai_provider}. "
            "Supported providers: anthropic, bedrock, claude-cli"
        )


@lru_cache(maxsize=1)
def get_cached_ai_client() -> AnthropicClient:
    """
    Get cached Anthropic-compatible AI client instance.

    Uses LRU cache to reuse the same client across requests.
    """
    return get_ai_client()


def validate_ai_client() -> bool:
    """
    Validate AI client configuration on startup.

    Returns:
        True if client can be created successfully

    Raises:
        AIClientError: If configuration is invalid
    """
    settings = get_settings()

    try:
        if settings.ai_provider == "claude-cli":
            logger.info("Claude CLI provider configured (no client validation needed)")
        else:
            get_ai_client()
            logger.info(f"AI client validated successfully (provider: {settings.ai_provider})")
        return True
    except AIClientError:
        raise
    except Exception as e:
        raise AIClientError(f"Failed to initialize AI client: {str(e)}") from e


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
    Get the appropriate orchestrator based on AI_PROVIDER setting.

    Returns a function that can be called to orchestrate chat.
    - claude-cli: Uses Claude CLI for local development
    - anthropic/bedrock: Uses Anthropic API (directly or via Bedrock)

    Returns:
        Orchestrator function
    """
    settings = get_settings()

    if settings.ai_provider == "claude-cli":
        # Engine is decoupled from the provider: the default claude-cli deployment can
        # additionally enable the sandboxed inkwest-agent (custom Converse loop). Resolve
        # the engine per turn from the session's model — a non-Claude model needs the
        # agent (the CLI is Claude-only). Claude models stay on the CLI (its native path).
        from app.services.engines import (
            INKWEST_AGENT,
            configured_default_engine,
            engine_for_turn,
            inkwest_agent_enabled,
        )

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
            # Route to the broker only when the agent can actually run (engine enabled +
            # sandbox available); otherwise fall back to the CLI.
            if (
                engine == INKWEST_AGENT
                and inkwest_agent_enabled()
                and get_settings().sandbox_enabled
            ):
                from app.services.orchestrator_inkwest_agent import (
                    orchestrate_chat_inkwest_agent,
                )

                logger.info(f"Engine inkwest-agent (sandboxed) for model {model}")
                orchestrate = orchestrate_chat_inkwest_agent
                used_engine = INKWEST_AGENT
            else:
                from app.services.orchestrator_cli import orchestrate_chat_cli

                orchestrate = orchestrate_chat_cli
                used_engine = "claude-cli"
            metrics.set_turn_engine(used_engine)
            metrics.set_turn_model(model)
            metrics.chat_turns_total.labels(engine=used_engine, model=model).inc()
            async for event in orchestrate(db=db, session_id=session_id, **kwargs):
                yield event

        logger.info("Using Claude CLI orchestrator (engine-dispatched)")
        return _dispatch_engine

    if settings.ai_provider == "bedrock":
        # Bedrock serves both Claude (Anthropic Messages protocol via the SDK) and
        # open-weight models (Bedrock Converse API). The target isn't known until
        # the session's model is resolved, so dispatch per turn.
        from app.services.model_catalog import is_anthropic_model, provider_speaks_messages
        from app.services.orchestrator import orchestrate_chat
        from app.services.orchestrator_converse import orchestrate_chat_converse

        async def _dispatch_bedrock(
            db: AsyncSession, session_id: "str | UUID", **kwargs: Any
        ) -> AsyncIterator[Any]:
            # Resolved again inside the chosen orchestrator — cheap (in-memory
            # selection + one indexed scalar), and keeps both paths standalone.
            model = await resolve_session_model(db, session_id)
            # Route by the registry's provider tag when the model has an entry —
            # this honors an admin's explicit provider choice on a custom model
            # (e.g. a gateway alias whose ID would fool the substring heuristic).
            # Fall back to the ID heuristic for anything not in the catalog.
            entry = get_entry(model)
            is_open = (
                not provider_speaks_messages(entry.provider)
                if entry
                else not is_anthropic_model(model)
            )
            orchestrate = orchestrate_chat_converse if is_open else orchestrate_chat
            logger.info(f"Bedrock turn via {'Converse' if is_open else 'Anthropic'}: {model}")
            used_engine = "bedrock-converse" if is_open else "bedrock-anthropic"
            metrics.set_turn_engine(used_engine)
            metrics.set_turn_model(model)
            metrics.chat_turns_total.labels(engine=used_engine, model=model).inc()
            async for event in orchestrate(db=db, session_id=session_id, **kwargs):
                yield event

        return _dispatch_bedrock

    from app.services.orchestrator import orchestrate_chat

    logger.info(f"Using Anthropic orchestrator with provider: {settings.ai_provider}")

    async def _dispatch_anthropic(
        db: AsyncSession, session_id: "str | UUID", **kwargs: Any
    ) -> AsyncIterator[Any]:
        # Direct Anthropic API: one engine, but resolve the model so the turn counter
        # carries the same engine+model labels as the other dispatch paths.
        model = await resolve_session_model(db, session_id)
        metrics.set_turn_engine("anthropic")
        metrics.set_turn_model(model)
        metrics.chat_turns_total.labels(engine="anthropic", model=model).inc()
        async for event in orchestrate_chat(db=db, session_id=session_id, **kwargs):
            yield event

    return _dispatch_anthropic
