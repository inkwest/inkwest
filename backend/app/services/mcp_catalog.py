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

Credential brokering: an HTTP/SSE server's real ``headers`` (its API key/token) are NOT
injected into the sandbox either — the materializers rewrite it to an in-app proxy entry
(``/api/mcp-proxy/{key}/`` + an internal JWT) and :mod:`app.api.routes.mcp_proxy` swaps the
real headers back in server-side, exactly like the OAuth2 path. See
:func:`is_http_server` / :func:`get_static_http_server` and
``mcp_config.build_session_static_servers``. A **stdio** server (``command``/``args``/``env``)
cannot be brokered — it runs a local process in the sandbox, so its secret env is injected
verbatim (the one remaining in-container cred path; requires ``mcp_proxy_base_url`` to broker
the HTTP ones, else everything falls back to verbatim).
"""

import base64
import binascii
import json
import logging
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# A Claude ``mcpServers[<key>]`` definition (command/args/env or url/headers/type).
ServerDef = dict[str, Any]


def parse_catalog(raw: str) -> dict[str, ServerDef]:
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

    catalog: dict[str, ServerDef] = {}
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


def get_mcp_catalog() -> dict[str, ServerDef]:
    """Return the parsed catalog ``{key: server_def}`` from settings."""
    return parse_catalog(get_settings().mcp_catalog)


def catalog_keys() -> list[str]:
    """Return the available catalog keys (safe to expose to the browser)."""
    return sorted(get_mcp_catalog().keys())


def build_mcp_servers(keys: list[str]) -> dict[str, ServerDef]:
    """
    Build the ``mcpServers`` object for the given enabled keys.

    Unknown keys are ignored (the catalog is the source of truth); callers should
    validate keys against :func:`catalog_keys` before persisting a selection.
    """
    catalog = get_mcp_catalog()
    return {key: catalog[key] for key in keys if key in catalog}


def is_http_server(server_def: ServerDef) -> bool:
    """True for an HTTP/SSE MCP server definition (carries a ``url``), False for stdio.

    The two MCP transports are mutually exclusive in a Claude ``mcpServers`` entry: HTTP/SSE
    uses ``url`` (+ optional ``headers``), stdio uses ``command``/``args``/``env``. Only the
    HTTP shape can be brokered (there is an upstream URL to reverse-proxy); a stdio server runs
    a local process inside the sandbox, so its secret env can't be lifted out.
    """
    return isinstance(server_def, dict) and bool(server_def.get("url"))


def get_static_http_server(key: str) -> ServerDef | None:
    """The static catalog definition for ``key`` if it is an HTTP/SSE server, else None.

    Used by the broker route to resolve a static key's real upstream ``url`` + ``headers``
    (the credential the sandbox never sees). Returns None for unknown keys and for stdio
    servers (not brokerable)."""
    server_def = get_mcp_catalog().get(key)
    return server_def if (server_def is not None and is_http_server(server_def)) else None
