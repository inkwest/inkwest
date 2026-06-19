"""
Turn-spec + NDJSON wire protocol for the standalone ``inkwest_agent``.

The broker (portal) hands the agent a fully-resolved **turn-spec** on stdin and
reads a newline-delimited JSON ``ChatEvent`` stream from stdout; the process
exit code is the terminal status (0 = clean ``done``, non-zero = ``error``).

The agent is DB-free: everything it needs for one turn is in the spec, already
resolved by the broker (system prompt, model, history). Persistence and resume
are the broker's job (it persists from the event stream).

This is the **minimal** turn-spec for P1.1 (loop isolation). The full contract —
``context{mode: resume|replay}``, ``creds_path`` (tmpfs), per-tool ``exec_profile``,
budget/wallclock limits — is formalized in P1.4. See
docs/SANDBOXED_AGENT_RUNTIME.md (Portal ↔ Agent service contract).
"""

import sys
from dataclasses import dataclass, field
from typing import Any

from app.core.constants import (
    MAX_CONVERSATION_MESSAGES,
    MAX_ORCHESTRATION_ITERATIONS,
    MAX_RESPONSE_TOKENS,
)
from app.services.events import ChatEvent

# Bumped when the turn-spec / event wire shape changes incompatibly so the broker
# can refuse a mismatched agent build.
PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class TurnSpec:
    """One turn's fully-resolved inputs, handed to the agent on stdin.

    The broker resolves all DB-bound values (``system_prompt``, ``model``,
    ``history``) before spawning the agent; the agent never touches the DB.
    """

    session_id: str
    user_message: str
    model: str
    system_prompt: str
    # Prior conversation in the Anthropic content-block shape used everywhere
    # else (role + str|list[block]); the new ``user_message`` is appended by the
    # loop, not included here.
    history: list[dict[str, Any]] = field(default_factory=list)
    # Bedrock invoke target (ARN for a provisioned/imported model); defaults to
    # ``model`` when unset.
    model_target: str | None = None
    region: str | None = None
    # Tool-execution context (Profile A; env-enforced). user_id keys the RAM
    # credential lookup for connector tools today — creds_path/tmpfs handoff is P1.4.
    user_id: str | None = None
    kubeconfig_path: str | None = None
    context_name: str | None = None
    # Limits (broker-enforced ceilings; the loop honors max_tokens/iterations).
    max_tokens: int = MAX_RESPONSE_TOKENS
    max_iterations: int = MAX_ORCHESTRATION_ITERATIONS
    max_conversation_messages: int = MAX_CONVERSATION_MESSAGES

    @property
    def invoke_target(self) -> str:
        """The Bedrock modelId to invoke (ARN override or the model ID)."""
        return self.model_target or self.model


class TurnSpecError(ValueError):
    """Raised when the turn-spec is missing required fields or malformed."""


def parse_turn_spec(raw: Any) -> TurnSpec:
    """Build a TurnSpec from the decoded stdin JSON, validating required fields."""
    if not isinstance(raw, dict):
        raise TurnSpecError("turn-spec must be a JSON object")
    missing = [k for k in ("session_id", "user_message", "model", "system_prompt") if k not in raw]
    if missing:
        raise TurnSpecError(f"turn-spec missing required field(s): {', '.join(missing)}")
    history = raw.get("history", [])
    if not isinstance(history, list):
        raise TurnSpecError("turn-spec 'history' must be a list")
    # Only known fields are accepted; an unknown key is a protocol mismatch, not
    # silently ignored (forward-compat is gated by PROTOCOL_VERSION).
    known = TurnSpec.__dataclass_fields__.keys()
    unknown = [k for k in raw if k not in known]
    if unknown:
        raise TurnSpecError(f"turn-spec has unknown field(s): {', '.join(sorted(unknown))}")
    return TurnSpec(**raw)


def emit(event: ChatEvent, stream: Any = None) -> None:
    """Write one ChatEvent as a single NDJSON line, flushed immediately.

    Flushing per event keeps the broker's stream parse real-time (the agent's
    stdout is the only progress channel)."""
    out = stream if stream is not None else sys.stdout
    out.write(event.to_json())
    out.write("\n")
    out.flush()
