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

from typing import Any

import boto3
from botocore.config import Config

from app.tools.definitions import get_tool_definitions

from .protocol import SkillSpec

# The synthetic skill tool name. A single enum tool (rather than one tool per skill)
# keeps the toolConfig compact regardless of library size while still surfacing every
# skill's short description — the always-visible half of progressive disclosure.
READ_SKILL_TOOL = "read_skill"
# Cap each skill's short description in the always-visible tool description so a large
# skill library can't blow Bedrock's toolSpec description size limit and fail the turn.
_SKILL_DESC_MAX = 200

# Bound the call so a hung/credential-stalled Bedrock endpoint can't pin the
# process: boto3 runs in a worker thread and cannot be cancelled mid-call.
_BEDROCK_CONFIG = Config(
    connect_timeout=10, read_timeout=120, retries={"max_attempts": 2, "mode": "standard"}
)


def make_bedrock_client(
    region: str | None,
    access_key: str | None = None,
    secret_key: str | None = None,
) -> Any:
    """Create a bedrock-runtime client (blocking — call via ``asyncio.to_thread``).

    Explicit credentials when given; otherwise boto3's default chain (the
    sandbox projects per-session creds onto tmpfs/env, so the chain resolves
    them without the portal ever handing them in argv).

    Egress is proxy-only: botocore honors the container's ``HTTPS_PROXY`` (set at
    sandbox provision) and the ``--internal`` network leaves no other route, so this
    client reaches Bedrock only through the per-conversation squid allowlist (P1.5).
    We deliberately do not set ``proxies`` on the Config — the env is the single source,
    matching the claude-cli path."""
    kwargs: dict[str, Any] = {"region_name": region, "config": _BEDROCK_CONFIG}
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


def to_converse_tools(
    skills: list[SkillSpec] | None = None,
    mcp_tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Translate the tool surfaces into one Converse toolConfig.

    Base read-only tools (kubectl/connectors) + an optional synthetic ``read_skill`` tool
    (P1.7 skills) + optional MCP tools already in Converse ``toolSpec`` shape (P1.7 MCP).
    Skills/MCP re-express claude-cli's native capability surfaces as Converse tools — the
    loop has no native skill/MCP system.
    """
    tools: list[dict[str, Any]] = [
        _tool_spec(t["name"], t["description"], t["input_schema"]) for t in get_tool_definitions()
    ]
    skill_tool = skill_tool_spec(skills or [])
    if skill_tool is not None:
        tools.append(skill_tool)
    if mcp_tools:
        tools.extend(mcp_tools)
    return {"tools": tools}
