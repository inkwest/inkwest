"""
Application configuration using pydantic-settings.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/inkwest"

    # AI Provider: anthropic, bedrock, azure, openrouter, google, or claude-cli
    ai_provider: Literal[
        "anthropic", "bedrock", "azure", "openai", "openrouter", "google", "claude-cli"
    ] = "claude-cli"

    # Anthropic settings
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-4-20250514"  # claude-opus-4-20250514, etc.

    # AWS Bedrock settings
    aws_region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    bedrock_model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"

    # Azure OpenAI settings
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_deployment: str | None = None  # e.g., "gpt-5-codex"
    azure_openai_api_version: str = "2024-12-01-preview"

    # Claude CLI settings (for local development without API costs)
    claude_cli_path: str = "claude"  # Path to claude binary
    claude_cli_timeout: int = 120  # Longer timeout for CLI

    # OpenAI settings (direct API)
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    openai_base_url: str = "https://api.openai.com/v1"

    # Google Gemini settings (direct API)
    google_api_key: str | None = None
    google_model: str = "gemini-2.5-flash"

    # OpenRouter settings (unified API for multiple AI providers)
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "anthropic/claude-sonnet-4"  # or openai/gpt-4o, google/gemini-pro, etc.
    openrouter_site_url: str | None = None  # Optional: your site URL for rankings
    openrouter_app_name: str = "Inkwest"  # App name shown in OpenRouter dashboard

    # Kubernetes
    kubeconfig_dir: str = "/etc/inkwest/kubeconfigs"
    kubectl_timeout: int = 30

    # App
    app_env: Literal["development", "production"] = "development"
    frontend_url: str = "http://localhost:3000"
    cors_origins: str = ""  # Comma-separated allowed origins; empty = frontend_url only
    secret_key: str = "change-me-in-production"  # For JWT signing
    system_prompt_extra: str = ""  # Extra instructions appended to the default system prompt

    # ==========================================================================
    # OIDC Authentication
    # ==========================================================================
    oidc_enabled: bool = False
    oidc_issuer_url: str | None = None  # e.g., https://accounts.google.com
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_redirect_uri: str | None = None  # e.g., http://localhost:8000/api/auth/callback
    oidc_scopes: str = "openid profile email"  # Space-separated scopes
    oidc_roles_claim: str = "roles"  # Claim path for roles in ID token
    oidc_admin_role: str = "admin"  # Role name that grants admin access
    login_redirect_mode: Literal["auto", "prompt"] = "prompt"

    # ==========================================================================
    # Budget Management
    # ==========================================================================
    budget_enabled: bool = False
    default_daily_token_limit: int = 100000  # Default daily limit per user
    default_monthly_token_limit: int = 2000000  # Default monthly limit per user
    global_daily_token_limit: int = 1000000  # Global daily limit
    global_monthly_token_limit: int = 20000000  # Global monthly limit

    # ==========================================================================
    # Interactive Flow
    # ==========================================================================
    default_confirmation_mode: str = "expensive"  # none, tools, expensive, iterations, all
    default_cost_threshold_cents: int = 10  # Confirm when cost > $0.10
    default_iteration_interval: int = 5  # Confirm every 5 iterations

    # ==========================================================================
    # Web Search
    # ==========================================================================
    web_search_enabled: bool = False
    web_search_provider: str = "brave"  # brave, serpapi, google
    web_search_api_key: str | None = None
    web_search_max_results: int = 5
    # Comma-separated trusted domains
    web_search_trusted_domains: str = (
        "kubernetes.io,docs.docker.com,helm.sh,prometheus.io,grafana.com"
    )
    # Comma-separated blocked domains
    web_search_blocked_domains: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def validate_settings() -> None:
    """Validate required settings on startup."""
    settings = get_settings()

    # Check AI provider configuration
    default_secret = "change-me-in-production"  # pragma: allowlist secret
    if settings.app_env == "production":
        if not settings.secret_key or not settings.secret_key.strip():
            raise ValueError("SECRET_KEY must not be empty in production")
        if settings.secret_key == default_secret:
            raise ValueError("SECRET_KEY must be changed from default in production")

    if settings.ai_provider == "anthropic" and not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is required when AI_PROVIDER is 'anthropic'")

    if settings.ai_provider == "bedrock" and not settings.aws_region:
        raise ValueError("AWS_REGION is required when AI_PROVIDER is 'bedrock'")

    if settings.ai_provider == "azure":
        if not settings.azure_openai_endpoint:
            raise ValueError("AZURE_OPENAI_ENDPOINT is required when AI_PROVIDER is 'azure'")
        if not settings.azure_openai_api_key:
            raise ValueError("AZURE_OPENAI_API_KEY is required when AI_PROVIDER is 'azure'")
        if not settings.azure_openai_deployment:
            raise ValueError("AZURE_OPENAI_DEPLOYMENT is required when AI_PROVIDER is 'azure'")

    if settings.ai_provider == "openai" and not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required when AI_PROVIDER is 'openai'")

    if settings.ai_provider == "openrouter" and not settings.openrouter_api_key:
        raise ValueError("OPENROUTER_API_KEY is required when AI_PROVIDER is 'openrouter'")

    if settings.ai_provider == "google" and not settings.google_api_key:
        raise ValueError("GOOGLE_API_KEY is required when AI_PROVIDER is 'google'")

    # claude-cli provider requires no additional configuration
    # (claude CLI uses its own authentication)
