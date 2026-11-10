"""
Wiring helpers that route a sandboxed engine's Bedrock calls through the LLM auth broker.

Centralizes the ``llm_broker_enabled`` gate + the broker base-URL derivation so both
sandboxed engines stay in lock-step:

- **claude-cli** (:func:`cli_broker_env`): the Claude CLI talks to Bedrock when
  ``CLAUDE_CODE_USE_BEDROCK=1`` + ``ANTHROPIC_BEDROCK_BASE_URL`` point it at the broker, with
  ``AWS_BEARER_TOKEN_BEDROCK`` carrying the per-turn grant. All three are injected per turn via
  the connector-env channel (``docker exec -e``), which overrides the static ``sandbox_env_file``.
- **inkwest-agent** (:func:`agent_bedrock_endpoint_url` + :func:`agent_broker_env`): boto3's
  ``endpoint_url`` rides the TurnSpec; the grant rides the spawn env.

The broker base reuses ``mcp_proxy_base_url``'s host (squid-allowlisted, host-gateway
``--add-host``) — no new egress rule, just a distinct ``/api/llm-proxy`` path prefix. When the
broker is off or ``mcp_proxy_base_url`` is unset, every helper returns empty/None so the engine
falls back to today's behavior (the real bearer in ``sandbox_env_file`` reaches Bedrock direct).

A fresh grant is minted per turn and the orchestrator BURNS it in its ``finally``
(:func:`burn_env`), so the token is dead the moment the turn ends — bounding a leaked grant to
the turn's duration. Mint and burn are paired: every env this module builds carries its token
in ``AWS_BEARER_TOKEN_BEDROCK``, which :func:`burn_env` reads back to revoke.
"""

import logging
from uuid import UUID

from app.core.config import get_settings
from app.services.llm_proxy_token import burn, mint

logger = logging.getLogger(__name__)

# The env key carrying the broker grant in every env this module builds — also where
# burn_env reads the token back to revoke it at turn end.
_TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"


def broker_base_url() -> str | None:
    """The broker's base URL (``<mcp_proxy_base_url-host>/api/llm-proxy``), or None when the
    broker is disabled or its host isn't configured.

    Both the CLI's ``ANTHROPIC_BEDROCK_BASE_URL`` and the agent's boto3 ``endpoint_url`` append
    ``/model/{id}/{op}`` to this, matching the broker route.
    """
    settings = get_settings()
    if not settings.llm_broker_enabled:
        return None
    # Prefer the dedicated broker host when set: broker traffic is pod-local and can skip the
    # load balancer entirely, while mcp_proxy_base_url must stay externally resolvable for
    # OAuth2 redirects. Unset ⇒ inherit that host, which is the previous behavior.
    base = settings.llm_broker_base_url or settings.mcp_proxy_base_url
    if not base:
        # validate_settings refuses this at boot with the sandbox on; kept as defence in depth.
        # Enabled but unreachable: the sandbox can't egress to a broker that has no host (the
        # squid allowlist + --add-host both key off the broker host). Fall back to direct
        # Bedrock rather than break model calls — but this RE-EXPOSES the real bearer in the
        # sandbox env-file (the exact posture the broker removes), so log at ERROR (alertable),
        # not WARNING: a misconfiguration here is a silent security regression, not a hiccup.
        logger.error(
            "llm_broker_enabled but neither llm_broker_base_url nor mcp_proxy_base_url is set; "
            "broker NOT wired — falling back to the real Bedrock bearer in the sandbox "
            "(credential re-exposed)"
        )
        return None
    return f"{base.rstrip('/')}/api/llm-proxy"


def cli_broker_env(
    user_id: UUID | str | None,
    session_id: UUID | str | None,
    engine: str | None = None,
    turn_id: UUID | str | None = None,
) -> dict[str, str]:
    """Env that routes the Claude CLI's Bedrock calls through the broker (claude-cli engine).

    ``engine``/``turn_id`` come from the orchestrator (it knows the resolved engine and owns
    the per-turn id) and attribute the broker's usage rows. Empty when the broker is
    off/misconfigured ⇒ the CLI uses whatever ``sandbox_env_file`` provides (today's
    behavior). Injected via ``docker exec -e``, so these override the env-file values for
    this turn's process without rebuilding the container.
    """
    base = broker_base_url()
    if base is None:
        return {}
    return {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": base,
        _TOKEN_ENV: mint(user_id, session_id, engine=engine, turn_id=turn_id),
    }


def agent_bedrock_endpoint_url() -> str | None:
    """boto3 ``endpoint_url`` for the inkwest-agent's Bedrock client; None ⇒ direct Bedrock."""
    return broker_base_url()


def agent_broker_env(
    user_id: UUID | str | None,
    session_id: UUID | str | None,
    engine: str | None = None,
    turn_id: UUID | str | None = None,
) -> dict[str, str]:
    """Grant env injected into a sandboxed agent at spawn (``docker exec -e``).

    Serves BOTH inkwest-agent and opencode, so ``engine`` is passed by the orchestrator, not
    hardcoded. Only the grant rides the env here — the endpoint_url rides the TurnSpec
    (:func:`agent_bedrock_endpoint_url`). Empty when the broker is off/misconfigured.
    """
    if broker_base_url() is None:
        return {}
    return {_TOKEN_ENV: mint(user_id, session_id, engine=engine, turn_id=turn_id)}


def burn_env(env: dict[str, str] | None) -> None:
    """Revoke the grant carried in a broker env, at turn end. None/empty-safe (from a
    ``finally``). A no-op when the broker was off (the env carries no token)."""
    if env:
        burn(env.get(_TOKEN_ENV))
