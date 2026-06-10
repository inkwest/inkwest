"""
MCP catalog — the set of MCP servers users may enable per conversation.

The catalog is defined by the ``MCP_CATALOG`` env var: a JSON object mapping a
stable key to a **base64-encoded** JSON definition of that server (the value that
goes under Claude's ``mcpServers[<key>]`` — command/args/env/url…). Encoding the
definition keeps env-var escaping sane and lets a definition carry secrets.

Security: the catalog (and any secret a definition embeds) stays server-side.
Only keys are ever exposed to the browser — never the decoded definitions. The
per-conversation merge into ``.claude.json`` lives in app.services.sandbox-side
code; this module only parses and validates the catalog.
"""

import base64
import binascii
import json
import logging

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def parse_catalog(raw: str) -> dict[str, dict]:
    """
    Parse the raw ``MCP_CATALOG`` value into ``{key: server_def}``.

    Tolerant by design: a malformed catalog or a single bad entry is logged and
    skipped rather than crashing startup — a broken catalog must not take the app
    down, it just yields fewer (or no) selectable servers.

    Args:
        raw: The ``MCP_CATALOG`` env value — JSON ``{"<key>": "<base64(json)>"}``.

    Returns:
        Mapping of catalog key to the decoded server definition (a dict).
    """
    if not raw.strip():
        return {}

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("MCP_CATALOG is not valid JSON, ignoring it: %s", e)
        return {}

    if not isinstance(entries, dict):
        logger.error("MCP_CATALOG must be a JSON object {key: base64(def)}, got %s", type(entries))
        return {}

    catalog: dict[str, dict] = {}
    for key, encoded in entries.items():
        if not isinstance(encoded, str):
            logger.error("MCP_CATALOG entry %r must be a base64 string, skipping", key)
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True)
            server_def = json.loads(decoded)
        except (binascii.Error, ValueError) as e:
            logger.error("MCP_CATALOG entry %r is not valid base64(JSON), skipping: %s", key, e)
            continue
        if not isinstance(server_def, dict):
            logger.error("MCP_CATALOG entry %r must decode to a JSON object, skipping", key)
            continue
        catalog[key] = server_def

    return catalog


def get_mcp_catalog() -> dict[str, dict]:
    """Return the parsed catalog ``{key: server_def}`` from settings."""
    return parse_catalog(get_settings().mcp_catalog)


def catalog_keys() -> list[str]:
    """Return the available catalog keys (safe to expose to the browser)."""
    return sorted(get_mcp_catalog().keys())


def build_mcp_servers(keys: list[str]) -> dict[str, dict]:
    """
    Build the ``mcpServers`` object for the given enabled keys.

    Unknown keys are ignored (the catalog is the source of truth); callers should
    validate keys against :func:`catalog_keys` before persisting a selection.
    """
    catalog = get_mcp_catalog()
    return {key: catalog[key] for key in keys if key in catalog}
