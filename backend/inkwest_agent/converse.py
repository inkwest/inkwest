"""
Bedrock Converse adapter for the standalone agent.

Converse is the universal substrate for every Bedrock vendor (Claude included),
so the custom engine is **Converse-only** — no Messages adapter (decision #1).
This module owns only the format translation at the API boundary: Anthropic
content blocks ↔ Converse blocks, and the tool-definition → ``toolConfig`` shape.
The internal blocks and ``ChatEvent`` stay canonical (no Converse-ism leaks into
the loop), so other hosts (OpenAI/Vertex/…) become additive adapters later.

The in-process ``app.services.orchestrator_converse`` keeps its own copy of these
helpers until P1.6 retires it; the duplication is intentional and temporary (the
in-process loop persists inline and shares nothing with this DB-free one).
"""

import os
from typing import Any

import boto3
from botocore.config import Config

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
                            "name": block["name"],
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
