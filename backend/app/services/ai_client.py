"""
AI client factory for Inkwest.

Supports multiple AI providers:
- anthropic: Direct Anthropic API
- bedrock: AWS Bedrock (Claude models)
- azure: Azure OpenAI (GPT models)
- openai: Direct OpenAI API
- openrouter: OpenRouter unified API (multiple providers)
"""

import logging
from functools import lru_cache
from typing import TypeAlias

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock
from openai import AsyncAzureOpenAI

from app.core.config import get_settings

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

    elif settings.ai_provider == "azure":
        raise AIClientError("Azure provider uses OpenAI-style API. Use get_azure_client() instead.")

    elif settings.ai_provider == "openai":
        raise AIClientError("OpenAI provider uses its own SDK. Use get_openai_client() instead.")

    elif settings.ai_provider == "openrouter":
        raise AIClientError(
            "OpenRouter provider uses OpenAI-style API. Use get_openrouter_client() instead."
        )

    elif settings.ai_provider == "google":
        raise AIClientError("Google provider uses its own SDK. Use get_google_client() instead.")

    else:
        raise AIClientError(
            f"Unknown AI provider: {settings.ai_provider}. "
            "Supported providers: anthropic, bedrock, azure, openai, openrouter, google"
        )


def get_azure_client() -> AsyncAzureOpenAI:
    """
    Get configured Azure OpenAI client.

    Returns:
        AsyncAzureOpenAI client instance

    Raises:
        AIClientError: If configuration is invalid
    """
    settings = get_settings()

    if settings.ai_provider != "azure":
        raise AIClientError(f"Azure client requested but AI_PROVIDER is '{settings.ai_provider}'")

    if not settings.azure_openai_endpoint:
        raise AIClientError("AZURE_OPENAI_ENDPOINT is required when AI_PROVIDER=azure")
    if not settings.azure_openai_api_key:
        raise AIClientError("AZURE_OPENAI_API_KEY is required when AI_PROVIDER=azure")

    logger.info(f"Initializing Azure OpenAI client at {settings.azure_openai_endpoint}")

    return AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )


@lru_cache(maxsize=1)
def get_cached_ai_client() -> AnthropicClient:
    """
    Get cached Anthropic-compatible AI client instance.

    Uses LRU cache to reuse the same client across requests.
    """
    return get_ai_client()


@lru_cache(maxsize=1)
def get_cached_azure_client() -> AsyncAzureOpenAI:
    """
    Get cached Azure OpenAI client instance.

    Uses LRU cache to reuse the same client across requests.
    """
    return get_azure_client()


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
        if settings.ai_provider == "azure":
            get_azure_client()
            logger.info("Azure OpenAI client validated successfully")
        elif settings.ai_provider == "openai":
            from app.services.orchestrator_openai import get_openai_client

            get_openai_client()
            logger.info("OpenAI client validated successfully")
        elif settings.ai_provider == "openrouter":
            from app.services.orchestrator_openrouter import get_openrouter_client

            get_openrouter_client()
            logger.info("OpenRouter client validated successfully")
        elif settings.ai_provider == "google":
            from app.services.orchestrator_google import get_google_client

            get_google_client()
            logger.info("Google Gemini client validated successfully")
        elif settings.ai_provider == "claude-cli":
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
    Get the model name to use based on provider.

    Returns:
        Model name/ID string appropriate for the configured provider
    """
    settings = get_settings()

    if settings.ai_provider == "bedrock":
        # Return the Bedrock model ID (can be overridden via config)
        return settings.bedrock_model_id

    elif settings.ai_provider == "azure":
        # For Azure, the deployment name is configured separately
        # Return a descriptive name for logging
        return settings.azure_openai_deployment or "azure-deployment"

    elif settings.ai_provider == "openai":
        return settings.openai_model

    elif settings.ai_provider == "openrouter":
        return settings.openrouter_model

    elif settings.ai_provider == "google":
        return settings.google_model

    else:
        # Anthropic and claude-cli use the configured model
        return settings.anthropic_model


def get_orchestrator():
    """
    Get the appropriate orchestrator based on AI_PROVIDER setting.

    Returns a function that can be called to orchestrate chat.
    - claude-cli: Uses Claude CLI for local development
    - azure: Uses Azure OpenAI with GPT models
    - anthropic/bedrock: Uses Anthropic API (directly or via Bedrock)

    Returns:
        Orchestrator function
    """
    settings = get_settings()

    if settings.ai_provider == "claude-cli":
        from app.services.orchestrator_cli import orchestrate_chat_cli

        logger.info("Using Claude CLI orchestrator")
        return orchestrate_chat_cli

    elif settings.ai_provider == "azure":
        from app.services.orchestrator_azure import orchestrate_chat_azure

        logger.info("Using Azure OpenAI orchestrator")
        return orchestrate_chat_azure

    elif settings.ai_provider == "openai":
        from app.services.orchestrator_openai import orchestrate_chat_openai

        logger.info("Using OpenAI orchestrator")
        return orchestrate_chat_openai

    elif settings.ai_provider == "openrouter":
        from app.services.orchestrator_openrouter import orchestrate_chat_openrouter

        logger.info("Using OpenRouter orchestrator")
        return orchestrate_chat_openrouter

    elif settings.ai_provider == "google":
        from app.services.orchestrator_google import orchestrate_chat_google

        logger.info("Using Google Gemini orchestrator")
        return orchestrate_chat_google

    else:
        from app.services.orchestrator import orchestrate_chat

        logger.info(f"Using Anthropic orchestrator with provider: {settings.ai_provider}")
        return orchestrate_chat
