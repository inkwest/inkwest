"""
Agentic engines: which loop runs a turn, decoupled from the model backend.

An *engine* is the agentic loop driving a conversation — the single driver of a
turn, decoupled from the inference backend (the CLI/sandbox configures its own):

- ``claude-cli``    — Claude Code CLI in the sandbox; Claude models only.
- ``inkwest-agent`` — the custom Converse loop; every Bedrock vendor (Claude
  included, via Converse). The limiter for ``claude-cli`` is the *client*, not
  Bedrock — see docs/SANDBOXED_AGENT_RUNTIME.md (Engine model).

The set of engines enabled in a deployment comes from ``AGENTIC_ENGINES``
(default ``claude-cli`` → today's behavior unchanged). Two things derive from it:

- the **catalog gate** (``inkwest_agent_enabled``): non-Claude Bedrock models are
  only surfaced when an engine that can run them is enabled — pointless to offer
  models no enabled engine can drive;
- the **per-turn engine** (``resolve_engine`` / ``engine_for_turn``): how
  ``get_orchestrator`` dispatches a turn (sandboxed agent vs CLI), reconciled
  against the resolved model and any explicit ``session.engine`` pin.
"""

from app.core.config import get_settings

CLAUDE_CLI = "claude-cli"
INKWEST_AGENT = "inkwest-agent"
OPENCODE = "opencode"
KNOWN_ENGINES = frozenset({CLAUDE_CLI, INKWEST_AGENT, OPENCODE})

# Engine applied to a Claude model when the choice is ambiguous (multiple engines
# enabled): Claude Code is the native, lowest-overhead path for Claude.
DEFAULT_ENGINE = CLAUDE_CLI

# Agent engines: in-sandbox coding agents that speak Bedrock Converse and can run
# ANY vendor (Claude included). claude-cli is the only Claude-only client, so it is
# the sole non-agent engine. Anything here opens the whole-Bedrock catalog.
AGENT_ENGINES = KNOWN_ENGINES - {CLAUDE_CLI}


def enabled_engines() -> list[str]:
    """Engines enabled in this deployment, from ``AGENTIC_ENGINES``."""
    return get_settings().agentic_engines_list


def inkwest_agent_enabled() -> bool:
    """Whether the custom Converse engine is enabled."""
    return INKWEST_AGENT in enabled_engines()


def opencode_enabled() -> bool:
    """Whether the opencode engine is enabled."""
    return OPENCODE in enabled_engines()


def open_catalog_enabled() -> bool:
    """Whether any agent engine is enabled — gates the whole-Bedrock (all-vendor)
    catalog. Pointless to surface non-Claude models no enabled engine can run.
    """
    return any(e in AGENT_ENGINES for e in enabled_engines())


def _speaks_anthropic_provider(provider: str | None, model_id: str) -> bool:
    """Whether the model talks the Anthropic Messages protocol (i.e. Claude).

    Prefers the registry's authoritative provider tag (a custom row may pin a
    vendor the substring heuristic would misread); falls back to the ID heuristic
    when there's no row. Local import breaks the cycle (model_catalog imports this
    module for the catalog gate).
    """
    from app.services.model_catalog import is_anthropic_model, provider_speaks_messages

    if provider is not None:
        return provider_speaks_messages(provider)
    return is_anthropic_model(model_id)


def _speaks_anthropic(model_id: str) -> bool:
    """Convenience wrapper resolving the registry entry then the protocol."""
    from app.services.model_registry import get_entry

    entry = get_entry(model_id)
    return _speaks_anthropic_provider(entry.provider if entry else None, model_id)


def capable_engines(model_id: str) -> list[str]:
    """The engines *capable* of running ``model_id`` — pure vendor capability, with
    no admin curation and no enabled-set filtering.

    ``claude-cli`` is the only Claude-only client; every other engine is an agent
    that speaks Converse and can run any vendor. So a Claude model is capable on
    **all** known engines; every other vendor is capable on the agent engines (all
    known except ``claude-cli``). Generalizes to N future agents without code
    changes. The admin UI uses this to know which engine sections a model appears
    in (independent of which engines it is *offered* on).
    """
    from app.services.model_registry import get_entry

    entry = get_entry(model_id)
    is_claude = _speaks_anthropic_provider(entry.provider if entry else None, model_id)
    capable = KNOWN_ENGINES if is_claude else (KNOWN_ENGINES - {CLAUDE_CLI})
    ordered = enabled_engines()
    # Enabled engines first (stable display order), then any capable-but-not-enabled.
    return [e for e in ordered if e in capable] + sorted(e for e in capable if e not in ordered)


def _engine_availability(model_id: str) -> list[str] | None:
    """The admin-curated per-model engine offering (``options["engines"]``), or None
    when unset — None means *offered on every capable engine* (default / back-compat).
    """
    from app.services.model_registry import get_entry

    entry = get_entry(model_id)
    avail = entry.options.get("engines") if entry else None
    return avail if isinstance(avail, list) else None


def runnable_engines(model_id: str) -> list[str]:
    """Enabled engines a user may actually pick for ``model_id`` right now.

    Vendor capability ∩ admin per-model offering (``options["engines"]``, unset =
    all capable) ∩ the deployment's enabled set, in enabled order — the selectable
    (engine, model) pairs the UI offers derive from this.
    """
    capable = capable_engines(model_id)
    avail = _engine_availability(model_id)
    offered = capable if avail is None else [e for e in capable if e in avail]
    offered_set = set(offered)
    return [e for e in enabled_engines() if e in offered_set]


def engine_for_turn(model_id: str, pinned: str | None = None) -> str:
    """The engine that should run a turn for ``model_id``.

    Precedence (degrades gracefully so a stale pin / engine toggle / admin curation
    never wedges a turn): an explicit session pin when it can run the model → the
    native Claude path (``claude-cli``) when offered → the first runnable engine →
    the capability default when nothing is offered.
    """
    runnable = runnable_engines(model_id)
    if pinned and pinned in runnable:
        return pinned
    if DEFAULT_ENGINE in runnable:
        return DEFAULT_ENGINE
    if runnable:
        return runnable[0]
    # Nothing offered (engine off / admin disabled every pairing): fall back to the
    # capability default so the turn still resolves to a sane engine.
    if _speaks_anthropic(model_id):
        return DEFAULT_ENGINE
    # Non-Claude needs an agent engine. Pick the operator's first-declared enabled agent
    # (AGENTIC_ENGINES order — deterministic, the deployment's primary) so the downstream
    # "engine unavailable" error names a real engine (not inkwest-agent on an opencode-only
    # deployment). Fall back to INKWEST_AGENT only when no agent engine is enabled at all.
    enabled_agents = [e for e in enabled_engines() if e in AGENT_ENGINES]
    return enabled_agents[0] if enabled_agents else INKWEST_AGENT


def resolve_engine(model_id: str) -> str:
    """The engine for ``model_id`` with no explicit session pin.

    Thin wrapper over :func:`engine_for_turn`; kept as the stable name the design
    doc references. New callers should prefer ``engine_for_turn`` so a session's
    explicit choice is honored.
    """
    return engine_for_turn(model_id)


def configured_default_engine() -> str | None:
    """The admin-chosen engine for the default model (the Engine·Model default), or
    None when unset → the engine is then resolved heuristically (engine_for_turn).
    """
    from app.services.app_settings import DEFAULT_ENGINE_KEY, get_setting

    return get_setting(DEFAULT_ENGINE_KEY) or None


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
