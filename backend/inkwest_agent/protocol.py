"""
Turn-spec + NDJSON wire protocol for the standalone ``inkwest_agent``.

The broker (portal) hands the agent a fully-resolved **turn-spec** on stdin and
reads a newline-delimited JSON ``ChatEvent`` stream from stdout; the process
exit code is the terminal status (0 = clean ``done``, non-zero = ``error``).

The agent is DB-free: everything it needs for one turn is in the spec, already
resolved by the broker (system prompt, model, history, projected creds).
Persistence and resume are the broker's job (it persists from the event stream);
the broker is the durable source of truth.

This is the **formalized** contract (P1.4), matching the service
contract in docs/SANDBOXED_AGENT_RUNTIME.md:

- ``context{mode, history, resume_token}`` — ``replay`` (broker seeds history,
  the only live mode today: the custom agent keeps no transcript of its own) vs
  ``resume`` (agent-side ``--resume``-style transcript, **reserved**).
- ``limits{max_iterations, max_tokens, max_conversation_messages,
  budget_remaining, wallclock_seconds}`` — the loop honors iterations/tokens/
  messages; ``budget_remaining``/``wallclock_seconds`` are **broker-enforced**
  ceilings (the broker kills on breach), carried for observability + future use.
- ``tools[{name, exec_profile}]`` — ``exec_profile`` is ``agent`` (Profile A,
  env-enforced; the only profile today) with ``broker`` (Profile B, broker-
  mediated) **reserved** for a later RW lot. The agent uses its own tool
  definitions; this descriptor is the contract handle for future routing.
- ``creds_path`` — the tmpfs dir holding the session's projected creds (the
  broker pre-provides them; the agent never mints/renews — see the broker's
  one-way handoff). Credentials reach the tool executors via the connector env
  the broker injects at spawn, exactly like the claude-cli path.
"""

import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.core.constants import (
    MAX_CONVERSATION_MESSAGES,
    MAX_ORCHESTRATION_ITERATIONS,
    MAX_RESPONSE_TOKENS,
)
from app.services.events import ChatEvent

# Bumped when the turn-spec / event wire shape changes incompatibly so the broker
# can refuse a mismatched agent build. P1.4 formalized context/limits/tools/creds.
PROTOCOL_VERSION = 2

# Engine tag default (the only engine that runs this agent).
INKWEST_AGENT = "inkwest-agent"


class TurnSpecError(ValueError):
    """Raised when the turn-spec is missing required fields or malformed."""


def _reject_unknown(raw: dict[str, Any], known: Iterable[str], where: str) -> None:
    unknown = [k for k in raw if k not in known]
    if unknown:
        raise TurnSpecError(f"turn-spec {where} has unknown field(s): {', '.join(sorted(unknown))}")


@dataclass(frozen=True)
class TurnContext:
    """Conversation context: how the agent gets its prior turns.

    ``mode=replay`` (default, the only live mode): the broker seeds the full
    ``history``. ``mode=resume`` + ``resume_token`` is **reserved** for an
    agent that keeps its own durable transcript (a later lot); the loop treats
    anything that isn't ``replay`` as replay and uses ``history`` regardless.
    """

    mode: str = "replay"
    history: list[dict[str, Any]] = field(default_factory=list)
    resume_token: str | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "TurnContext":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TurnSpecError("turn-spec 'context' must be an object")
        _reject_unknown(raw, cls.__dataclass_fields__.keys(), "'context'")
        history = raw.get("history", [])
        if not isinstance(history, list):
            raise TurnSpecError("turn-spec 'context.history' must be a list")
        return cls(
            mode=raw.get("mode", "replay"),
            history=history,
            resume_token=raw.get("resume_token"),
        )


@dataclass(frozen=True)
class TurnLimits:
    """Per-turn ceilings. The loop honors iterations/tokens/messages; the broker
    enforces ``budget_remaining``/``wallclock_seconds`` by killing on breach."""

    max_iterations: int = MAX_ORCHESTRATION_ITERATIONS
    max_tokens: int = MAX_RESPONSE_TOKENS
    max_conversation_messages: int = MAX_CONVERSATION_MESSAGES
    budget_remaining: int | None = None
    wallclock_seconds: int | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "TurnLimits":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise TurnSpecError("turn-spec 'limits' must be an object")
        _reject_unknown(raw, cls.__dataclass_fields__.keys(), "'limits'")
        # Validate at the boundary (symmetry with TurnContext/ToolSpec): a non-int limit
        # would otherwise surface as an opaque boto error deep in the Converse call.
        # bool is an int subclass — reject it explicitly (a JSON true/false is not a limit).
        for k in ("max_iterations", "max_tokens", "max_conversation_messages"):
            if k in raw and (isinstance(raw[k], bool) or not isinstance(raw[k], int)):
                raise TurnSpecError(f"turn-spec 'limits.{k}' must be an int")
        for k in ("budget_remaining", "wallclock_seconds"):
            v = raw.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise TurnSpecError(f"turn-spec 'limits.{k}' must be an int or null")
        return cls(**raw)


@dataclass(frozen=True)
class ToolSpec:
    """A tool the agent may use, with its execution profile.

    ``exec_profile=agent`` (Profile A): the agent runs it locally, policy enforced
    by the environment. ``broker`` (Profile B): broker-mediated, **reserved** for a
    later RW lot. The agent uses its own tool definitions today; this is the
    contract handle for future per-tool routing.
    """

    name: str
    exec_profile: str = "agent"

    @classmethod
    def from_raw(cls, raw: Any) -> "ToolSpec":
        if not isinstance(raw, dict):
            raise TurnSpecError("turn-spec 'tools' entries must be objects")
        _reject_unknown(raw, cls.__dataclass_fields__.keys(), "'tools' entry")
        if "name" not in raw:
            raise TurnSpecError("turn-spec 'tools' entry missing 'name'")
        return cls(name=raw["name"], exec_profile=raw.get("exec_profile", "agent"))


@dataclass(frozen=True)
class TurnSpec:
    """One turn's fully-resolved inputs, handed to the agent on stdin.

    The broker resolves all DB-bound values (``system_prompt``, ``model``,
    history) and projects creds before spawning the agent; the agent never
    touches the DB, never mints/renews creds, never calls back to the portal.
    """

    session_id: str
    user_message: str
    model: str
    system_prompt: str
    # Which agentic loop the broker selected (informational; this binary IS the
    # inkwest-agent engine — see app.services.engines).
    engine: str = INKWEST_AGENT
    context: TurnContext = field(default_factory=TurnContext)
    limits: TurnLimits = field(default_factory=TurnLimits)
    # Advertised tools + per-tool exec_profile (reserved routing handle; the agent
    # uses its own definitions today).
    tools: list[ToolSpec] = field(default_factory=list)
    # Bedrock invoke target (ARN for a provisioned/imported model); defaults to
    # ``model`` when unset.
    model_target: str | None = None
    region: str | None = None
    # Tool-execution context (Profile A; env-enforced). user_id labels the turn;
    # connector creds reach the executors via the broker-injected env, sourced from
    # the tmpfs ``creds_path`` (the agent never reads the portal credential store).
    user_id: str | None = None
    kubeconfig_path: str | None = None
    context_name: str | None = None
    creds_path: str | None = None

    @property
    def invoke_target(self) -> str:
        """The Bedrock modelId to invoke (ARN override or the model ID)."""
        return self.model_target or self.model


# Top-level keys parse_turn_spec accepts (sub-objects validate their own keys).
_TOP_LEVEL_KEYS = frozenset(TurnSpec.__dataclass_fields__.keys())
# Scalar fields = everything that isn't one of the nested objects. Derived (not a hand-
# kept list) so adding a scalar field to TurnSpec needs no second edit here.
_NESTED_KEYS = frozenset({"context", "limits", "tools"})
_SCALAR_KEYS = _TOP_LEVEL_KEYS - _NESTED_KEYS
_REQUIRED_KEYS = ("session_id", "user_message", "model", "system_prompt")


def parse_turn_spec(raw: Any) -> TurnSpec:
    """Build a TurnSpec from the decoded stdin JSON, validating required fields.

    Unknown keys (at any level) are a protocol mismatch, not silently ignored —
    forward-compat is gated by PROTOCOL_VERSION.
    """
    if not isinstance(raw, dict):
        raise TurnSpecError("turn-spec must be a JSON object")
    missing = [k for k in _REQUIRED_KEYS if k not in raw]
    if missing:
        raise TurnSpecError(f"turn-spec missing required field(s): {', '.join(missing)}")
    _reject_unknown(raw, _TOP_LEVEL_KEYS, "top level")

    tools_raw = raw.get("tools", [])
    if not isinstance(tools_raw, list):
        raise TurnSpecError("turn-spec 'tools' must be a list")

    scalar = {k: raw[k] for k in _SCALAR_KEYS if k in raw}
    return TurnSpec(
        context=TurnContext.from_raw(raw.get("context")),
        limits=TurnLimits.from_raw(raw.get("limits")),
        tools=[ToolSpec.from_raw(t) for t in tools_raw],
        **scalar,
    )


def emit(event: ChatEvent, stream: Any = None) -> None:
    """Write one ChatEvent as a single NDJSON line, flushed immediately.

    Flushing per event keeps the broker's stream parse real-time (the agent's
    stdout is the only progress channel)."""
    out = stream if stream is not None else sys.stdout
    out.write(event.to_json())
    out.write("\n")
    out.flush()
