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
    them without the portal ever handing them in argv)."""
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


def to_converse_tools() -> dict[str, Any]:
    """Translate the Anthropic tool definitions into a Converse toolConfig."""
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "inputSchema": {"json": tool["input_schema"]},
                }
            }
            for tool in get_tool_definitions()
        ]
    }
