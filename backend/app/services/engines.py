"""
Agentic engines: which loop runs a turn, decoupled from the model backend.

An *engine* is the agentic loop driving a conversation, distinct from
``AI_PROVIDER`` (the model API/credentials):

- ``claude-cli``    — Claude Code CLI in the sandbox; Claude models only.
- ``inkwest-agent`` — the custom Converse loop; every Bedrock vendor (Claude
  included, via Converse). The limiter for ``claude-cli`` is the *client*, not
  Bedrock — see docs/SANDBOXED_AGENT_RUNTIME.md (Engine model).

The set of engines enabled in a deployment comes from ``AGENTIC_ENGINES``
(default ``claude-cli`` → today's behavior unchanged). Two things derive from it:

- the **catalog gate** (``inkwest_agent_enabled``): non-Claude Bedrock models are
  only surfaced when an engine that can run them is enabled — pointless to offer
  models no enabled engine can drive;
- the **per-turn engine** (``resolve_engine``): the contract the Phase 1 broker
 consumes to dispatch a turn. First-class today (``session.engine``
  reserved); the in-process orchestrator selection still routes by provider.
"""

from app.core.config import get_settings

CLAUDE_CLI = "claude-cli"
INKWEST_AGENT = "inkwest-agent"
KNOWN_ENGINES = frozenset({CLAUDE_CLI, INKWEST_AGENT})

# Engine applied to a Claude model when the choice is ambiguous (multiple engines
# enabled): Claude Code is the native, lowest-overhead path for Claude.
DEFAULT_ENGINE = CLAUDE_CLI


def enabled_engines() -> list[str]:
    """Engines enabled in this deployment, from ``AGENTIC_ENGINES``."""
    return get_settings().agentic_engines_list


def inkwest_agent_enabled() -> bool:
    """Whether the custom Converse engine is enabled (gates the open catalog)."""
    return INKWEST_AGENT in enabled_engines()


def resolve_engine(model_id: str) -> str:
    """The engine that should run a turn for ``model_id``.

    - Non-Claude model → ``inkwest-agent`` (the only engine that can run it; it
      is selectable only when that engine is enabled, so this is reachable).
    - Claude model → the single enabled engine, else ``claude-cli`` (the default,
      native Claude path) when more than one is enabled.
    """
    # Local import breaks the import cycle (model_catalog imports this module for
    # the catalog gate); is_anthropic_model is a pure string predicate.
    from app.services.model_catalog import is_anthropic_model

    engines = enabled_engines()
    if not is_anthropic_model(model_id):
        return INKWEST_AGENT
    if len(engines) == 1:
        return engines[0]
    return DEFAULT_ENGINE


def validate_engines() -> None:
    """Fail fast on an unknown/empty ``AGENTIC_ENGINES`` entry (startup)."""
    engines = enabled_engines()
    if not engines:
        raise ValueError("AGENTIC_ENGINES must list at least one engine")
    unknown = [e for e in engines if e not in KNOWN_ENGINES]
    if unknown:
        raise ValueError(
            f"AGENTIC_ENGINES has unknown engine(s): {', '.join(unknown)}. "
            f"Known engines: {', '.join(sorted(KNOWN_ENGINES))}"
        )
