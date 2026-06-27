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
- ``skills[{name, description}]`` + ``skills_root`` (P1.7) — the active skills as
  *progressive disclosure*: only name + short description ride the spec (the
  always-visible half); the SKILL.md **bodies** stay on sandbox files under
  ``skills_root`` (the broker materialized them, parity with the claude-cli
  ``apply_session_skills``) and are read on demand by the ``read_skill`` tool.
- ``mcp_config_path`` (P1.7) — in-container path to a JSON file the broker
  materialized in the sandbox holding the enabled MCP server definitions
  (``.claude.json``-shaped ``{name: {command/args/env | url/headers/type}}``).
  Server creds/headers live in that file (projected one-way like connector
  creds, cleaned up on turn end) — never on argv. The agent's lazy MCP client
  reads it, connects the servers, and merges their tools into the Converse loop.
- ``attachments[{path, content_type, filename, size}]`` (P1.8) — files uploaded
  to this turn, already in the sandbox /workdir (the broker wrote them via the
  attachments service; only the in-container path + metadata ride the spec, not
  the bytes). The Converse adapter reads each file and, **only for an
  IMAGE-capable model**, turns a supported image (jpeg/png/gif/webp, within a size
  cap) into a Converse image content block on the user turn; non-image / oversized
  / unsupported attachments are skipped for image translation. Empty (the default)
  keeps the pre-P1.8 text-only behavior (back-compat).
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
# can refuse a mismatched agent build. P1.4 formalized context/limits/tools/creds;
# P1.7 added skills (progressive disclosure) + the MCP config path; P1.8 added image
# attachments (the Converse adapter turns IMAGE files into image content blocks); the
# LLM auth broker added bedrock_endpoint_url (route the model call through the broker);
# v6 added limits.bash_timeout_seconds (the free-form bash exec tool's per-call timeout).
PROTOCOL_VERSION = 6

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
    # Per-call timeout for the free-form ``bash`` exec tool (v6). The broker carries its
    # ``claude_cli_timeout`` here (the agent is settings-free); None ⇒ the agent falls back
    # to its own default. Still bounded by the broker's whole-turn wall-clock kill.
    bash_timeout_seconds: int | None = None

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
        for k in ("budget_remaining", "wallclock_seconds", "bash_timeout_seconds"):
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
class SkillSpec:
    """One active skill, advertised as a tool (progressive disclosure, P1.7).

    Only ``name`` + ``description`` ride the spec — the always-visible half. The
    SKILL.md **body** stays on a sandbox file under ``TurnSpec.skills_root`` and is
    loaded only when the model triggers the skill (the ``read_skill`` tool returns
    it as the tool_result), so a large skill library never inflates every prompt.
    """

    name: str
    description: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "SkillSpec":
        if not isinstance(raw, dict):
            raise TurnSpecError("turn-spec 'skills' entries must be objects")
        _reject_unknown(raw, cls.__dataclass_fields__.keys(), "'skills' entry")
        if "name" not in raw:
            raise TurnSpecError("turn-spec 'skills' entry missing 'name'")
        return cls(name=raw["name"], description=raw.get("description") or "")


@dataclass(frozen=True)
class AttachmentSpec:
    """One uploaded file projected into the sandbox /workdir (P1.8).

    The broker already wrote the bytes to the conversation's durable workdir (the
    upload route / attachments service); only the in-container ``path`` + metadata ride
    the spec — never the bytes. The Converse adapter reads the file from ``path`` and,
    for an IMAGE-capable model, translates a supported image into a Converse image block;
    a non-image (or oversized/unsupported) attachment is skipped for image translation.
    """

    path: str
    content_type: str = ""
    filename: str = ""
    size: int | None = None

    @classmethod
    def from_raw(cls, raw: Any) -> "AttachmentSpec":
        if not isinstance(raw, dict):
            raise TurnSpecError("turn-spec 'attachments' entries must be objects")
        _reject_unknown(raw, cls.__dataclass_fields__.keys(), "'attachments' entry")
        if "path" not in raw:
            raise TurnSpecError("turn-spec 'attachments' entry missing 'path'")
        size = raw.get("size")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int)):
            raise TurnSpecError("turn-spec 'attachments.size' must be an int or null")
        return cls(
            path=raw["path"],
            content_type=raw.get("content_type") or "",
            filename=raw.get("filename") or "",
            size=size,
        )


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
    # Bedrock endpoint override (LLM auth broker): when set, boto3 targets the broker
    # instead of real Bedrock — it builds ``{url}/model/{id}/converse[-stream]`` and signs
    # with the bearer grant (AWS_BEARER_TOKEN_BEDROCK), no SigV4. None = direct Bedrock.
    bedrock_endpoint_url: str | None = None
    # Tool-execution context (Profile A; env-enforced). user_id labels the turn;
    # connector creds reach the executors via the broker-injected env, sourced from
    # the tmpfs ``creds_path`` (the agent never reads the portal credential store).
    user_id: str | None = None
    kubeconfig_path: str | None = None
    context_name: str | None = None
    creds_path: str | None = None
    # Skills as tools (P1.7): name+description only; bodies live under skills_root.
    skills: list[SkillSpec] = field(default_factory=list)
    skills_root: str | None = None
    # In-container path to the materialized MCP server config (P1.7); None disables MCP.
    mcp_config_path: str | None = None
    # Files uploaded to this turn, already in the sandbox /workdir (P1.8). The Converse
    # adapter turns IMAGE files into image content blocks on the user turn when the model
    # is IMAGE-capable; empty (the default) keeps the pre-P1.8 text-only behavior.
    attachments: list[AttachmentSpec] = field(default_factory=list)
    # The resolved model's input modalities (P1.8), from the broker's live catalog
    # (Bedrock terms: TEXT/IMAGE/VIDEO/SPEECH). The agent is DB-free, so the broker hands
    # it the capability; the Converse adapter sends image blocks only when "IMAGE" is here.
    # Empty (the default) = text-only/unknown ⇒ no image blocks (back-compat).
    input_modalities: list[str] = field(default_factory=list)

    @property
    def invoke_target(self) -> str:
        """The Bedrock modelId to invoke (ARN override or the model ID)."""
        return self.model_target or self.model

    @property
    def image_capable(self) -> bool:
        """Whether the resolved model accepts image input (P1.8 gate).

        Case-insensitive over ``input_modalities`` so a differently-cased Bedrock token
        ("Image"/"IMAGE") still matches. Empty modalities ⇒ False (text-only/unknown)."""
        return any(m.upper() == "IMAGE" for m in self.input_modalities)


# Top-level keys parse_turn_spec accepts (sub-objects validate their own keys).
_TOP_LEVEL_KEYS = frozenset(TurnSpec.__dataclass_fields__.keys())
# Scalar fields = everything that isn't one of the nested objects. Derived (not a hand-
# kept list) so adding a scalar field to TurnSpec needs no second edit here.
_NESTED_KEYS = frozenset({"context", "limits", "tools", "skills", "attachments"})
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
    skills_raw = raw.get("skills", [])
    if not isinstance(skills_raw, list):
        raise TurnSpecError("turn-spec 'skills' must be a list")
    attachments_raw = raw.get("attachments", [])
    if not isinstance(attachments_raw, list):
        raise TurnSpecError("turn-spec 'attachments' must be a list")

    scalar = {k: raw[k] for k in _SCALAR_KEYS if k in raw}
    return TurnSpec(
        context=TurnContext.from_raw(raw.get("context")),
        limits=TurnLimits.from_raw(raw.get("limits")),
        tools=[ToolSpec.from_raw(t) for t in tools_raw],
        skills=[SkillSpec.from_raw(s) for s in skills_raw],
        attachments=[AttachmentSpec.from_raw(a) for a in attachments_raw],
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
