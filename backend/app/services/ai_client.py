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
        from app.services.orchestrator_cli import orchestrate_chat_cli

        logger.info("Using Claude CLI orchestrator")
        return orchestrate_chat_cli

    if settings.ai_provider == "bedrock":
        # Bedrock serves both Claude (Anthropic Messages protocol via the SDK) and
        # open-weight models (Bedrock Converse API). The target isn't known until
        # the session's model is resolved, so dispatch per turn.
        from app.services.model_catalog import is_anthropic_model
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
            is_open = entry.provider == "bedrock-open" if entry else not is_anthropic_model(model)
            orchestrate = orchestrate_chat_converse if is_open else orchestrate_chat
            logger.info(f"Bedrock turn via {'Converse' if is_open else 'Anthropic'}: {model}")
            async for event in orchestrate(db=db, session_id=session_id, **kwargs):
                yield event

        return _dispatch_bedrock

    from app.services.orchestrator import orchestrate_chat

    logger.info(f"Using Anthropic orchestrator with provider: {settings.ai_provider}")
    return orchestrate_chat
