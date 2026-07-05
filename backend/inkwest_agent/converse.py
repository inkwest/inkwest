"""
Bedrock Converse adapter for the standalone agent.

Converse is the universal substrate for every Bedrock vendor (Claude included),
so the custom engine is **Converse-only** — no Messages adapter (decision #1).
This module owns only the format translation at the API boundary: Anthropic
content blocks ↔ Converse blocks, and the tool-definition → ``toolConfig`` shape.
The internal blocks and ``ChatEvent`` stay canonical (no Converse-ism leaks into
the loop), so other hosts (OpenAI/Vertex/…) become additive adapters later.

This is the only Converse implementation: the former in-process
``app.services.orchestrator_converse`` has been retired (the engine is the sole
driver of a turn), so these helpers live here alone.
"""

import hashlib
import os
import re
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.tools.definitions import get_tool_definitions

from .protocol import AttachmentSpec, SkillSpec

# The synthetic skill tool name. A single enum tool (rather than one tool per skill)
# keeps the toolConfig compact regardless of library size while still surfacing every
# skill's short description — the always-visible half of progressive disclosure.
READ_SKILL_TOOL = "read_skill"
# Cap each skill's short description in the always-visible tool description so a large
# skill library can't blow Bedrock's toolSpec description size limit and fail the turn.
_SKILL_DESC_MAX = 200

# The synthetic clock tool. Open-weight Converse models have no reliable sense of "now"
# (they guess the year), so an authoritative on-demand wall clock complements the date
# anchored in the system prompt — accuracy when the model asks, reliability up front.
CURRENT_DATETIME_TOOL = "current_datetime"

# Free-form exec tools (only the sandboxed agent advertises these — to_converse_tools is
# the DB-free worker's own builder, so they never reach the unsandboxed in-portal path).
# Names owned here; the executors + the /workdir jail live in exec.py (skills.py pattern).
BASH_TOOL = "bash"
READ_FILE_TOOL = "read_file"
WRITE_FILE_TOOL = "write_file"

# Bound the call so a hung/credential-stalled Bedrock endpoint can't pin the
# process: boto3 runs in a worker thread and cannot be cancelled mid-call.
_BEDROCK_CONFIG = Config(
    connect_timeout=10, read_timeout=120, retries={"max_attempts": 2, "mode": "standard"}
)

# Image input (P1.8). Converse accepts only these four formats; the value is the literal
# Converse ``format`` token. Detected by content-type first, then file extension — never
# trusting the model with an unsupported byte stream (Converse rejects it and fails the
# whole turn). Anything off this map is skipped for image translation.
_CONVERSE_IMAGE_FORMATS = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
_IMAGE_EXT_FORMATS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".gif": "gif",
    ".webp": "webp",
}
# Per-image byte cap. Bedrock's own Converse image limit is generous; this is a defensive
# adapter-side guard so one oversized upload can't bloat the request and fail the turn.
# The upload route already caps each file (attachment_max_bytes, 10 MiB default); this is
# the same order of magnitude, enforced again at the adapter boundary (defense in depth).
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _image_format(attachment: AttachmentSpec) -> str | None:
    """The Converse image format for an attachment, or None if it isn't a supported image.

    Content-type wins (the uploader set it); the file extension is the fallback when the
    type is generic/missing. Returns None for any non-image / unsupported type so the
    caller skips it — never sends an unsupported block (Converse would reject the turn).
    """
    ctype = (attachment.content_type or "").split(";", 1)[0].strip().lower()
    fmt = _CONVERSE_IMAGE_FORMATS.get(ctype)
    if fmt:
        return fmt
    ext = os.path.splitext(attachment.filename or attachment.path)[1].lower()
    return _IMAGE_EXT_FORMATS.get(ext)


def image_blocks_for_attachments(
    attachments: list[AttachmentSpec], *, image_capable: bool
) -> tuple[list[dict[str, Any]], list[str]]:
    """Translate IMAGE attachments into Converse image content blocks (P1.8).

    Returns ``(blocks, skipped)`` — the image blocks to prepend to the user turn, plus a
    human-readable reason per skipped attachment (for a status/log line). Strictly gated:

    - ``image_capable=False`` (a text-only model): **no** image blocks at all — Converse
      rejects images for such a model and would fail the whole turn. Every image is
      reported skipped so the broker can surface a friendly note.
    - non-image / unsupported format → skipped (kept text-only).
    - oversized / unreadable file → skipped (never crashes the turn).

    The bytes are read here, at the adapter boundary, from the in-sandbox /workdir path the
    broker projected — no Converse-ism leaks into the canonical TurnSpec/ChatEvent.
    """
    blocks: list[dict[str, Any]] = []
    skipped: list[str] = []
    for att in attachments:
        label = att.filename or os.path.basename(att.path) or att.path
        fmt = _image_format(att)
        if fmt is None:
            # Non-image attachments are simply not image-translated (a text file may still
            # be referenced by the user's prompt) — note it only for an apparent image type.
            if (att.content_type or "").lower().startswith("image/"):
                skipped.append(f"{label}: unsupported image format")
            continue
        if not image_capable:
            skipped.append(f"{label}: model does not accept image input")
            continue
        try:
            with open(att.path, "rb") as f:
                data = f.read(MAX_IMAGE_BYTES + 1)
        except OSError as e:
            skipped.append(f"{label}: cannot read ({e.strerror or e})")
            continue
        if not data:
            skipped.append(f"{label}: empty file")
            continue
        if len(data) > MAX_IMAGE_BYTES:
            skipped.append(f"{label}: exceeds {MAX_IMAGE_BYTES // (1024 * 1024)} MiB image limit")
            continue
        blocks.append({"image": {"format": fmt, "source": {"bytes": data}}})
    return blocks, skipped


def make_bedrock_client(
    region: str | None,
    access_key: str | None = None,
    secret_key: str | None = None,
    endpoint_url: str | None = None,
) -> Any:
    """Create a bedrock-runtime client (blocking — call via ``asyncio.to_thread``).

    Explicit credentials when given; otherwise boto3's default chain (the
    sandbox projects per-session creds onto tmpfs/env, so the chain resolves
    them without the portal ever handing them in argv).

    ``endpoint_url`` (LLM auth broker): when set, point boto3 at the broker instead of
    real Bedrock. boto3 then builds ``{endpoint_url}/model/{id}/converse[-stream]`` and,
    because ``AWS_BEARER_TOKEN_BEDROCK`` is set, signs with ``Authorization: Bearer …``
    (no SigV4) — so the broker is a pure header swap. None = direct Bedrock (today's path).

    Egress is proxy-only: botocore honors the container's ``HTTPS_PROXY`` (set at
    sandbox provision) and the ``--internal`` network leaves no other route, so this
    client reaches Bedrock (or the broker) only through the per-conversation squid allowlist
    (P1.5). We deliberately do not set ``proxies`` on the Config — the env is the single
    source, matching the claude-cli path."""
    kwargs: dict[str, Any] = {"region_name": region, "config": _BEDROCK_CONFIG}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    return boto3.client("bedrock-runtime", **kwargs)


def tool_result_text(content: Any) -> str:
    """Flatten a stored tool_result's content into plain text for Converse."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content)


# Bedrock Converse constrains every tool name to the ``ToolName`` shape — ``[a-zA-Z0-9_-]``
# AND max 64 chars — applied to BOTH ``toolConfig`` ToolSpecification.name AND history
# ToolUseBlock.name, and fails the whole turn on a violation of *either* half (the Anthropic
# Messages API has no such limit, so this bites only the Converse engine). A namespaced
# ``mcp__<server>__<tool>`` name from an upstream server we don't control can break both:
# a ``.``/``:``/space in the upstream tool name (illegal char) and/or an over-64 length. So
# ``fit_tool_name`` makes any name a legal ToolName — sanitize illegal chars to ``_`` first,
# then fold overflow to exactly 64 (a readable head + short sha256 of the ORIGINAL name, so
# distinct names never collide after truncation). Both steps are deterministic on the full
# string, so the SAME name is produced whether built for the toolConfig or replayed from
# history another engine persisted (Claude Code uses the same ``mcp__…`` names) — history and
# toolConfig agree. The route map — not the name's shape — drives dispatch, so this never
# breaks routing. Idempotent: a fitted name is already legal+≤64 and passes through unchanged.
_MAX_TOOL_NAME_LEN = 64
_NAME_HASH_LEN = 7
# Anything outside Converse's ToolName charset; folded to ``_`` so an upstream ``get.user`` /
# ``list:all`` can't fail the pattern (same "we don't control upstream names" rationale as length).
_ILLEGAL_TOOL_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def fit_tool_name(name: str) -> str:
    """Fold a tool name to Converse's ``ToolName`` shape (``[a-zA-Z0-9_-]{1,64}``).

    Sanitizes illegal chars to ``_`` then caps length at 64 with a hash suffix. Deterministic
    and idempotent — a name already within the shape passes through unchanged (Claude Code
    parity for the common case). The hash keys off the ORIGINAL name so two names that only
    differ in an illegal char still fold apart when they overflow. Below the length cap the
    sanitize is lossy, so two short names differing only by an illegal char (``a.b``/``a:b``)
    collide to one advertised name — resolved at connect time by ``MCPManager`` dedupe (skip +
    ``mcp_tool_name_collision`` warn), never a silent mis-route.
    """
    # ToolName is ``{1,64}`` — an empty/all-nothing name must not fold to ``""`` (which fails
    # the pattern's min-length, the very 400 this guards against). No live path feeds empty,
    # but keep the invariant local rather than trusting every caller.
    safe = _ILLEGAL_TOOL_NAME_CHARS.sub("_", name) or "_"
    if len(safe) <= _MAX_TOOL_NAME_LEN:
        return safe
    digest = hashlib.sha256(name.encode()).hexdigest()[:_NAME_HASH_LEN]
    head = safe[: _MAX_TOOL_NAME_LEN - _NAME_HASH_LEN - 1]
    return f"{head}-{digest}"


def to_converse_message(message: dict[str, Any]) -> dict[str, Any]:
    """Translate one Anthropic-shaped history message into a Converse message."""
    content = message["content"]
    blocks: list[dict[str, Any]] = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"text": content})
    else:
        for block in content:
            btype = block.get("type")
            if btype == "text":
                if block.get("text", "").strip():
                    blocks.append({"text": block["text"]})
            elif btype == "tool_use":
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": block["id"],
                            # Fold here too: history may carry a >64 name persisted by another
                            # engine (Claude Code), which Converse rejects on ToolUseBlock.name
                            # exactly as on the toolSpec. Same fold ⇒ matches the advertised name.
                            "name": fit_tool_name(block["name"]),
                            "input": block.get("input") or {},
                        }
                    }
                )
            elif btype == "tool_result":
                blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": block["tool_use_id"],
                            "content": [
                                {"text": tool_result_text(block.get("content")) or "(no output)"}
                            ],
                            "status": "error" if block.get("is_error") else "success",
                        }
                    }
                )
            # 'thinking' blocks (Claude-only) are dropped — open models reject them.
    if not blocks:
        # Converse rejects empty content; keep alternation intact with a stub.
        blocks.append({"text": "(no content)"})
    return {"role": message["role"], "content": blocks}


def _tool_spec(name: str, description: str, input_schema: dict[str, Any]) -> dict[str, Any]:
    """Wrap one tool in the Converse ``toolSpec`` envelope."""
    return {
        "toolSpec": {
            "name": name,
            "description": description,
            "inputSchema": {"json": input_schema},
        }
    }


def _short_desc(description: str) -> str:
    """A skill's short description, truncated for the always-visible tool catalog."""
    text = description or "(no description)"
    return text if len(text) <= _SKILL_DESC_MAX else text[:_SKILL_DESC_MAX].rstrip() + "…"


def skill_tool_spec(skills: list[SkillSpec]) -> dict[str, Any] | None:
    """Build the synthetic ``read_skill`` tool for the active skills (progressive disclosure).

    The tool's description carries every skill's (truncated) short description — the
    always-visible half; the SKILL.md body is loaded only when the model calls the tool
    with a ``name`` (the loaded half). Returns None when there are no skills, so the tool
    isn't advertised needlessly.
    """
    if not skills:
        return None
    names = [s.name for s in skills]
    catalog = "\n".join(f"- {s.name}: {_short_desc(s.description)}" for s in skills)
    description = (
        "Load a reusable investigation skill (a procedure/playbook). Call this with the "
        "skill's `name` to read its full instructions, THEN follow them. Available skills:\n"
        f"{catalog}"
    )
    schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "enum": names,
                "description": "The skill to load (one of the listed names).",
            }
        },
        "required": ["name"],
    }
    return _tool_spec(READ_SKILL_TOOL, description, schema)


def datetime_tool_spec() -> dict[str, Any]:
    """Build the synthetic ``current_datetime`` tool (authoritative wall clock).

    No input; returns the agent's current UTC datetime so the model can ground relative
    ranges and absolute timestamps instead of guessing the date. Always advertised — it's
    free and every connector (kubectl/AWS/Coralogix) benefits from a correct "now".
    """
    description = (
        "Return the current UTC date and time (ISO 8601). Call this whenever you need "
        "the current time, today's date, or to build a relative time range (last 24h, "
        "this week). Use the returned value verbatim — never assume the date or year."
    )
    return _tool_spec(CURRENT_DATETIME_TOOL, description, {"type": "object", "properties": {}})


def exec_tool_specs() -> list[dict[str, Any]]:
    """Build the free-form exec tools (``bash`` / ``read_file`` / ``write_file``).

    Always advertised for the sandboxed agent (the sandbox is the containment boundary,
    not the tool surface). They let the model write and run analysis code rather than only
    calling the typed connector tools; the executors + the /workdir jail live in exec.py.
    """
    bash = _tool_spec(
        BASH_TOOL,
        "Run a shell command in /workdir (persists across turns). Use it to write+run "
        "Python/bash for analysis, or to pipe connector CLIs (kubectl, aws, glab — "
        "read-only). stdout and stderr are merged; large output is truncated.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."}
            },
            "required": ["command"],
        },
    )
    read_file = _tool_spec(
        READ_FILE_TOOL,
        "Read a UTF-8 text file from /workdir. Returns raw content (no line-number "
        "prefixes). Page large files with `offset` (0-based start line) and `limit`.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to /workdir)."},
                "offset": {"type": "integer", "description": "0-based start line (default 0)."},
                "limit": {"type": "integer", "description": "Max lines to return (default 2000)."},
            },
            "required": ["path"],
        },
    )
    write_file = _tool_spec(
        WRITE_FILE_TOOL,
        "Write a UTF-8 text file to /workdir (overwrites). Use it to save a script before "
        "running it with bash, or to persist an analysis artifact across turns.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to /workdir)."},
                "content": {"type": "string", "description": "The full file content."},
            },
            "required": ["path", "content"],
        },
    )
    return [bash, read_file, write_file]


def to_converse_tools(
    skills: list[SkillSpec] | None = None,
    mcp_tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Translate the tool surfaces into one Converse toolConfig.

    Base read-only tools (kubectl/connectors) + the always-on synthetic ``current_datetime``
    clock + the always-on free-form exec tools (``bash``/``read_file``/``write_file``, the
    sandbox is the containment boundary) + an optional synthetic ``read_skill`` tool (P1.7
    skills) + optional MCP tools already in Converse ``toolSpec`` shape (P1.7 MCP).
    Skills/MCP/clock/exec re-express capabilities the Converse loop has no native system for.
    """
    tools: list[dict[str, Any]] = [
        _tool_spec(t["name"], t["description"], t["input_schema"]) for t in get_tool_definitions()
    ]
    tools.append(datetime_tool_spec())
    tools.extend(exec_tool_specs())
    skill_tool = skill_tool_spec(skills or [])
    if skill_tool is not None:
        tools.append(skill_tool)
    if mcp_tools:
        tools.extend(mcp_tools)
    return {"tools": tools}


# --- Opportunistic prompt caching --------------------------------------------
# A cachePoint is a Converse content block: everything before a marker is a cacheable
# prefix. Placement + rejection signatures were captured live (2026-07-04/05, gitignored
# local record misc/bedrock-fixtures-verbatim/round3-cachepoint-placement-probe.md; the
# messages are quoted verbatim in tests). Matching a CODE alone is never enough — a plain
# AccessDeniedException is a real IAM failure and must surface, and ValidationException
# covers many malformed-request causes; both matchers therefore require the code AND a
# message half.
_CACHE_REJECTION_CODE = "AccessDeniedException"
_CACHE_REJECTION_MARKER = "prompt caching"
# Nova (cache-capable) refuses ONLY the toolConfig placement, with a different signature —
# so this rejection downgrades to system+message markers instead of disabling caching.
_PLACEMENT_REJECTION_CODE = "ValidationException"
_PLACEMENT_REJECTION_MARKER = "extraneous key [cachePoint]"


def cache_point() -> dict[str, Any]:
    """A fresh cachePoint content block (fresh dict per insertion — a shared module
    constant would corrupt every occurrence if a kwargs consumer ever mutated one)."""
    return {"cachePoint": {"type": "default"}}


def _error_parts(e: ClientError) -> tuple[str, str]:
    err = e.response.get("Error", {})
    return err.get("Code") or "", err.get("Message") or ""


def is_cache_unsupported_rejection(e: ClientError) -> bool:
    """True iff Bedrock rejected the call because the model allows no prompt caching.

    The rejection is pre-inference (bills zero tokens), so a marker-free retry is free.
    """
    code, message = _error_parts(e)
    return code == _CACHE_REJECTION_CODE and _CACHE_REJECTION_MARKER in message


def is_cache_placement_rejection(e: ClientError) -> bool:
    """True iff Bedrock refused a cachePoint's PLACEMENT (not caching itself).

    Captured on Nova: the tools marker is "extraneous", while system/message markers
    cache fine — also pre-inference, so retrying without the refused marker is free.
    """
    code, message = _error_parts(e)
    return code == _PLACEMENT_REJECTION_CODE and _PLACEMENT_REJECTION_MARKER in message


def add_cache_points(kwargs: dict[str, Any], *, tools_marker: bool = True) -> dict[str, Any]:
    """Return a copy of built converse kwargs with cachePoint markers appended.

    Pure — never mutates the input (persisted history must stay marker-free; markers are
    a wire concern on the converse-shaped copies only). Markers land after the system
    blocks, as the last ``toolConfig.tools`` member (skippable via ``tools_marker=False``
    for models like Nova that cache but refuse that placement), and after the last
    message's content.
    """
    out = {**kwargs}
    out["system"] = [*kwargs["system"], cache_point()]
    tools = kwargs["toolConfig"].get("tools") or []
    if tools and tools_marker:
        out["toolConfig"] = {**kwargs["toolConfig"], "tools": [*tools, cache_point()]}
    messages = kwargs["messages"]
    if messages:
        last = messages[-1]
        out["messages"] = [
            *messages[:-1],
            {**last, "content": [*last["content"], cache_point()]},
        ]
    return out


def strip_cache_points(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of converse kwargs with every cachePoint block removed.

    Pure inverse of ``add_cache_points`` for the free retry on an unsupported model —
    sweeps system, toolConfig.tools and every message so the rebuilt call is marker-free
    regardless of where markers were placed.
    """
    out = {**kwargs}
    out["system"] = [b for b in kwargs["system"] if "cachePoint" not in b]
    tools = kwargs["toolConfig"].get("tools")
    if tools:
        out["toolConfig"] = {
            **kwargs["toolConfig"],
            "tools": [b for b in tools if "cachePoint" not in b],
        }
    out["messages"] = [
        {**m, "content": [b for b in m["content"] if "cachePoint" not in b]}
        for m in kwargs["messages"]
    ]
    return out
