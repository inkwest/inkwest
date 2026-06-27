"""
Application configuration using pydantic-settings.
"""

import re
from datetime import datetime
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Captured once at import (≈ process startup). When no build-time VERSION is
# injected (the "dev" sentinel below), the backend reports this startup-stamped
# version so every restart looks like a fresh deploy to the update-banner check
# letting the banner be exercised locally without building an image.
# The leading "v" matches the prod build convention (Dockerfile VERSION=v…, git tags).
_DEV_VERSION = f"v0.0.0-dev-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/inkwest"

    # Agentic engines enabled in this deployment (CSV). An engine is *which
    # agentic loop* runs a turn — the single driver of a turn, decoupled from the
    # inference backend (the CLI/sandbox configures its own):
    #   - claude-cli    : Claude Code CLI in the sandbox (Claude models only)
    #   - inkwest-agent : the custom Converse loop (all Bedrock vendors)
    # Default `claude-cli` preserves today's behavior exactly (no AWS needed until
    # inkwest-agent is enabled). The catalog is engine-aware (see app.services.engines
    # / model_catalog): non-Claude Bedrock models are only surfaced when an engine
    # that can run them is enabled. See docs/SANDBOXED_AGENT_RUNTIME.md (decision #7).
    agentic_engines: str = "claude-cli"

    # Anthropic settings. The default model is NOT env-configured: it lives in
    # the database (admin Model screen), falling back to the newest model of
    # the provider's catalog (see app.services.model_catalog).
    anthropic_api_key: str | None = None

    # AWS Bedrock settings
    aws_region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    # Real Bedrock API key (bearer) the LLM auth broker swaps the sandbox's per-turn grant
    # for (app.api.routes.llm_proxy). Read from the backend process env
    # AWS_BEARER_TOKEN_BEDROCK — the SAME var name that used to ride sandbox_env_file INTO
    # the sandbox. When llm_broker_enabled is on it lives ONLY in the backend process: both
    # the Claude CLI and boto3 send it to Bedrock as `Authorization: Bearer …` (no SigV4),
    # so the broker is a pure header swap. Unset = broker route returns 503 (not configured).
    aws_bearer_token_bedrock: str | None = None

    # Claude CLI settings (for local development without API costs)
    claude_cli_path: str = "claude"  # Path to claude binary
    claude_cli_timeout: int = 120  # Longer timeout for CLI
    # Comma-separated model IDs the CLI's backend serves (Anthropic API, Bedrock
    # profiles, gateway...). There is no catalog API for this provider, so the
    # operator declares it; selected models are passed via `claude --model`.
    # Order matters: the FIRST entry is the zero-config default model.
    # Empty = fall back to the pricing-table catalog.
    claude_cli_models: str = ""

    # Kubernetes
    kubeconfig_dir: str = "/etc/inkwest/kubeconfigs"
    kubectl_timeout: int = 30

    # Build metadata, injected as env vars at image build time (see Dockerfile:
    # VERSION / COMMIT_HASH). Surfaced read-only via /version so the SPA can detect
    # a newer deployment and prompt a reload. Defaults are dev/local placeholders.
    version: str = "dev"
    commit_hash: str = "unknown"
    build_timestamp: str = "unknown"

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
    # Per-user default spend cap in USD/month applied to every user without a more
    # specific (user/role/global) budget row. 0 = no default cap. Admin-overridable
    # at runtime via the budget_enabled / default_monthly_cost_limit_usd app settings.
    default_monthly_cost_limit_usd: float = 0.0
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

    # ==========================================================================
    # External Access Connectors (AWS / GitLab)
    # ==========================================================================
    connectors_enabled: bool = False
    # Backend callback URL the IdPs redirect to. Defaults to
    # f"{frontend_url}/api/connectors/callback" when unset.
    connector_redirect_base: str | None = None

    # AWS IAM Identity Center (SSO). Device Authorization Grant -> SSO token -> STS creds.
    aws_sso_enabled: bool = False
    aws_sso_start_url: str | None = None  # https://<org>.awsapps.com/start
    aws_sso_region: str | None = None  # region hosting Identity Center
    aws_sso_oidc_client_id: str | None = None  # pre-provisioned IdC OAuth app
    aws_sso_oidc_client_secret: str | None = None  # optional (confidential client)
    aws_sso_oidc_issuer: str | None = None  # e.g. https://oidc.<region>.amazonaws.com
    aws_cli_path: str = "aws"
    # Defense in depth: regex matched (case-insensitive) against "<account_name>.<role_name>"
    # to restrict which roles are offered/mintable. Empty/unset → no filter (all roles).
    aws_sso_role_filter: str | None = None

    # GitLab OAuth (public PKCE client -> no secret). Read-only scopes only.
    gitlab_enabled: bool = False
    gitlab_base_url: str = "https://gitlab.com"
    gitlab_oauth_client_id: str | None = None
    gitlab_oauth_scopes: str = "read_api read_repository"

    # ==========================================================================
    # Per-conversation Docker sandbox (claude-cli provider only)
    # Each conversation IS its own claude-toolbox container, paired with a sibling
    # squid proxy on an internal-only network; the toolbox's only egress path is the
    # proxy's domain allowlist. The backend drives the host Docker daemon via the
    # `docker` CLI (Docker-out-of-Docker). See app.services.sandbox.
    # ==========================================================================
    sandbox_enabled: bool = False
    sandbox_docker_path: str = "docker"
    sandbox_toolbox_image: str = ""  # claude-toolbox image (claude + kubectl + aws)
    # Image the broker spawns the inkwest-agent worker in (P1.3). Empty = reuse
    # sandbox_toolbox_image (the toolbox bakes the agent in — see backend/Dockerfile.agent); set this to point at a dedicated agent image later (decision #6).
    sandbox_agent_image: str = ""
    # The sandbox container's HOME. Claude Code state (durable, for --resume) and the creds
    # tmpfs are anchored here, and CLAUDE_CONFIG_DIR is set to <home>/.claude so it works
    # regardless of the image's own HOME env. Set this to your image's home directory.
    sandbox_home: str = "/home/sandbox"
    sandbox_squid_image: str = ""  # squid proxy image
    # Optional host path bind-mounted read-only as the squid config (empty = baked
    # into the image). The conf's access_log MUST target the mounted logs dir
    # (/var/log/squid/access.log), and its allowlist must cover .eks.amazonaws.com +
    # the chosen Claude backend endpoint.
    sandbox_squid_conf: str = ""
    # Optional host path bind-mounted read-only as the squid error_directory
    # (/etc/squid/errors): the LLM-parseable error templates in sandbox/errors.
    # The checked-in squid.conf sets error_directory, so set this whenever
    # sandbox_squid_conf points at it. Empty = squid's stock pages.
    sandbox_squid_errors: str = ""
    # Optional host path bind-mounted read-only at a DEDICATED /conf.d in the proxy: the
    # checked-in squid.conf `include`s /conf.d/*.conf, so local/site-specific ACL fragments
    # (e.g. extra allowed_domains for a VPN) can extend the policy without editing the
    # tracked squid.conf. A dedicated dir (not the image's /etc/squid/conf.d) avoids pulling
    # in the stock image's `http_access allow localnet` and fails CLOSED when unmounted. A
    # committed placeholder keeps the glob non-empty (squid FATALs on a zero-match include).
    # Empty = not mounted (squid.conf's include then FATALs — set this for the proxy to run).
    sandbox_squid_confd: str = ""
    # Optional resolver for the proxy container (`docker run --dns`). The stock squid
    # image often can't resolve public names via the host's stub resolver — set this
    # (e.g. 1.1.1.1, as in the POC) when egress fails with DNS errors. Empty = default.
    sandbox_proxy_dns: str = ""
    # Host path to the sandbox .env — provider-agnostic model-backend creds for the
    # in-toolbox `claude` (Anthropic API / Bedrock / Vertex / gateway). Mounted into
    # the backend; passed to the conversation container via `docker run --env-file`.
    sandbox_env_file: str = ""
    # Host root for per-conversation dirs (logs/, home/, workdir/). Bind-mounted into
    # the backend at the same path so it can create the dirs the daemon then binds.
    sandbox_sessions_root: str = "/var/lib/inkwest/sessions"
    # Root for per-session AWS/kube credentials, on a RAM-backed (tmpfs) volume shared
    # between the backend and the dind daemon so the daemon can bind-mount each session's
    # `<id>/.aws` into its conversation container — without the secrets ever touching
    # disk. When set, it becomes the AWS creds base (see connector_exec._aws_base); empty
    # falls back to the backend-local /dev/shm projection (no sandbox propagation).
    sandbox_creds_root: str = ""
    sandbox_idle_ttl: int = 1800  # reap a conversation idle longer than this (s)
    sandbox_provision_timeout: int = 60  # wait for proxy healthy + toolbox up (s)
    sandbox_cpu_limit: str = "1.0"  # docker run --cpus, per container
    sandbox_memory_limit: str = "1g"  # docker run --memory, per container
    sandbox_pids_limit: int = 512  # docker run --pids-limit, per container
    # Per-user ceiling on concurrent conversation sandboxes (each = conv + squid
    # container pair). When a user's new turn would exceed it, their oldest IDLE
    # sandbox(es) are soft-reaped (durable dir kept → cold-start later); in-use ones
    # are never killed, so an all-busy user runs briefly over-cap. 0 = unlimited. This
    # is only the boot default — the live value is admin-tunable (app_settings) without
    # a restart; see app.services.sandbox._max_sessions_per_user.
    sandbox_max_sessions_per_user: int = 5

    # File attachments uploaded into a conversation's /workdir and referenced to the
    # agent as @/workdir/<file>. Sandbox/claude-cli only (the only orchestrator with a
    # workdir + @-syntax). Limits guard host disk and prompt size; only these
    # extensions are accepted (comma-separated, case-insensitive, no dots).
    attachment_max_bytes: int = 10 * 1024 * 1024  # 10 MiB per file
    attachment_max_per_message: int = 10
    attachment_allowed_extensions: str = (
        "png,jpg,jpeg,gif,webp,svg,pdf,txt,md,log,json,yaml,yml,"
        "csv,tsv,toml,ini,conf,xml,html,sh,py,js,ts,go,sql"
    )

    # Per-user preferences + personal skills quotas. Soft caps, server-
    # enforced: they bound the agent's per-turn context (every active personal skill
    # is materialized into the sandbox) and host storage. Tunable per deployment.
    user_skills_max_count: int = 50  # max ACTIVE personal skills per user
    user_skill_max_bytes: int = 64 * 1024  # max SKILL.md size, per personal skill
    user_prefs_doc_max_bytes: int = 256 * 1024  # max serialized preferences document

    # ==========================================================================
    # MCP catalog — the set of MCP servers users may enable per conversation.
    # JSON object {"<key>": "<base64(server-def-json)>"}; each value base64-decodes
    # to the Claude `mcpServers[<key>]` definition (command/args/env/url…). Kept
    # server-side: only keys are exposed to the browser, never the definitions or
    # any secret they embed. Parsed/validated by app.services.mcp_catalog.
    # ==========================================================================
    mcp_catalog: str = ""

    # ==========================================================================
    # OAuth2 MCP catalog — remote MCP servers that authenticate via the MCP
    # Authorization spec (OAuth 2.1). Same outer shape as MCP_CATALOG
    # ({"<key>": "<base64(server-def-json)>"}) but each decoded def is the oauth
    # kind: {"type":"http","url":"https://…/mcp","scopes":[…],"client_id?":…}.
    # Kept separate from MCP_CATALOG so static entries (Coralogix, …) are never
    # entangled with the brokered-OAuth flow. Parsed by app.services.mcp_oauth_catalog.
    # ==========================================================================
    mcp_catalog_oauth2: str = ""
    # Fixed base for the OAuth2 MCP callback (must match what is registered at the
    # AS / sent in DCR). Falls back to frontend_url, mirroring connector_redirect_base.
    mcp_oauth_redirect_base: str | None = None
    # Base URL the sandboxed CLI uses to reach the in-app MCP reverse proxy
    # (app.api.routes.mcp_proxy). MUST be a host the per-conversation squid allowlists and
    # can route to the backend (Docker host-gateway alias) — NOT 127.0.0.1 (that is the
    # toolbox itself). Empty disables oauth-entry injection into .claude.json (Increment 2).
    mcp_proxy_base_url: str = ""
    # Explicit allowlist (comma-separated exact hostnames) of OAuth2 MCP servers permitted to
    # resolve to a non-public address — for internal/VPN-hosted MCP servers behind private DNS.
    # The SSRF guard still requires https and still IP-pins the connection; this only waives the
    # public-IP requirement for these exact hosts. Empty = public-only (the safe default). A
    # deliberate, scoped operator decision — not a blanket private-network opener.
    mcp_oauth_allow_hosts: str = ""

    # ==========================================================================
    # LLM auth broker (claude-cli provider only) — zero model secret in the sandbox.
    # On: the broker route (app.api.routes.llm_proxy) verifies a short-lived opaque grant
    # minted per turn, swaps it for the backend-held aws_bearer_token_bedrock, and forwards
    # to real Bedrock. The sandbox holds only the grant + the broker base URL (which rides the
    # SAME host as mcp_proxy_base_url, inheriting its squid allowlist + host-gateway --add-host
    # — no new egress rule). Off (default): today's behavior — the real bearer rides
    # sandbox_env_file into the sandbox. Rollback = flip this off + restore the bearer in
    # sandbox_env_file (see docs/llm-auth-broker.md §Rollback).
    # ==========================================================================
    llm_broker_enabled: bool = False

    # At-rest encryption keys for operator secrets (e.g. the OAuth2 MCP catalog client
    # secret). JSON map {"<version>": "<base64 32-byte key>"}; the highest version seals new
    # writes, older versions stay to decrypt existing values (rotation). Empty = sealing
    # disabled (secrets stored as-is). Generate a key with `openssl rand -base64 32`.
    secretbox_keys: str = ""

    # Prometheus metrics exporter on a DEDICATED port (separate from the API). On by default;
    # serves the global registry (process/Python + the inkwest_* app metrics) at /metrics.
    # The endpoint is UNAUTHENTICATED and binds 0.0.0.0 for in-cluster scraping — restrict
    # this port to the scraper via NetworkPolicy/firewall (the port is the isolation
    # boundary). Boot is best-effort: a port clash logs and the app continues. The exporter
    # serves ONE process's registry, so this assumes the documented single-worker /
    # single-replica deployment; with multiple uvicorn workers only one binds the port and
    # counters would reflect that worker alone (move to prometheus_client multiprocess mode
    # before scaling out).
    metrics_enabled: bool = True
    metrics_port: int = 9090

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def agent_image(self) -> str:
        """Image to spawn the inkwest-agent worker in: the dedicated image if set,
        else the toolbox image (which bakes the agent in)."""
        return self.sandbox_agent_image or self.sandbox_toolbox_image

    @property
    def agentic_engines_list(self) -> list[str]:
        """Enabled engines, parsed from the CSV (order preserved, deduped)."""
        seen: dict[str, None] = {}
        for raw in self.agentic_engines.split(","):
            engine = raw.strip()
            if engine:
                seen.setdefault(engine, None)
        return list(seen)

    @model_validator(mode="after")
    def _stamp_dev_version(self) -> "Settings":
        # "dev" means no build-time VERSION was injected. On a local/dev run, swap it
        # for a startup-stamped version so a backend restart trips the update banner.
        # Gated on app_env: a prod image that forgot to inject VERSION keeps the static
        # "dev" sentinel rather than churning a fresh version (and banner) per restart.
        if self.version == "dev" and self.app_env == "development":
            self.version = _DEV_VERSION
        # CI non-tag image: the build injects COMMIT_HASH but no tag, so VERSION keeps
        # the Dockerfile default "v0.0.0". Append the sha so the build is identifiable
        # and the update banner trips per redeploy (every dev image would otherwise
        # report an identical "v0.0.0"). Skip when no real sha was injected.
        elif self.version == "v0.0.0" and self.commit_hash not in ("", "unknown", "00000000-dirty"):
            self.version = f"{self.version}-{self.commit_hash}"
        return self


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

    # Engines validated here (local import: app.services.engines reads settings).
    from app.services.engines import inkwest_agent_enabled, validate_engines

    validate_engines()

    # The inkwest-agent engine runs a Bedrock Converse loop, so it needs an AWS region
    # (orchestrator_inkwest_agent passes settings.aws_region to the agent). Credentials
    # are NOT required here in general: with the broker off they ride sandbox_env_file
    # (opaque to the backend); only the broker — which swaps in the backend-held bearer —
    # is checkable, so require aws_bearer_token_bedrock just in that case. The default
    # claude-cli-only deployment needs no AWS at all (dev-local parity preserved).
    if inkwest_agent_enabled():
        if not settings.aws_region:
            raise ValueError("AWS_REGION is required when the 'inkwest-agent' engine is enabled")
        if settings.llm_broker_enabled and not settings.aws_bearer_token_bedrock:
            raise ValueError(
                "AWS_BEARER_TOKEN_BEDROCK is required when 'inkwest-agent' runs with "
                "LLM_BROKER_ENABLED (the broker swaps it in for the sandbox grant)"
            )

    # Connectors: validate each enabled connector's required config in production.
    if settings.connectors_enabled and settings.app_env == "production":
        if settings.aws_sso_enabled and not (
            settings.aws_sso_start_url
            and settings.aws_sso_region
            and settings.aws_sso_oidc_client_id
        ):
            raise ValueError(
                "AWS_SSO_START_URL, AWS_SSO_REGION and AWS_SSO_OIDC_CLIENT_ID "
                "are required when AWS_SSO_ENABLED is true"
            )
        if settings.gitlab_enabled and not settings.gitlab_oauth_client_id:
            raise ValueError("GITLAB_OAUTH_CLIENT_ID is required when GITLAB_ENABLED is true")

    # Fail fast on a malformed role filter (any env): a bad regex would otherwise
    # only surface at SSO enumeration time, mid-request.
    if settings.aws_sso_role_filter:
        try:
            re.compile(settings.aws_sso_role_filter)
        except re.error as e:
            raise ValueError(f"AWS_SSO_ROLE_FILTER is not a valid regex: {e}") from e

    # claude-cli provider requires no additional configuration
    # (claude CLI uses its own authentication)
