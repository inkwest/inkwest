"""
OAuth2 MCP catalog — remote MCP servers users may connect via OAuth 2.1.

Defined by the ``MCP_CATALOG_OAUTH2`` env var, which has the *same outer shape* as
``MCP_CATALOG`` (a JSON object ``{"<key>": "<base64(json)>"}``) but whose decoded
definitions are the oauth kind rather than a static stdio/header server::

    {"type": "http", "url": "https://mcp.example.com/mcp", "scopes": ["mcp:read"],
     "client_id": "optional", "client_secret": "optional"}

Kept deliberately separate from :mod:`app.services.mcp_catalog`: the static catalog
(Coralogix, kubectl, …) is injected verbatim into ``.claude.json`` and never touches
the OAuth broker, while these entries drive the discover → DCR → PKCE → token flow.

Security: like the static catalog, definitions stay server-side. Only keys (and a
per-user auth status) ever reach the browser — never the decoded definitions.
"""

import base64
import binascii
import json
import logging
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class OAuthMcpServer:
    """A validated OAuth2 MCP catalog entry. Lightweight, secret-free to expose by key."""

    __slots__ = ("key", "url", "scopes", "client_id", "client_secret")

    def __init__(self, key: str, definition: dict[str, Any]) -> None:
        self.key = key
        self.url: str = definition["url"]
        # Scopes are optional; some authorization servers grant a default set.
        scopes = definition.get("scopes") or []
        self.scopes: list[str] = [str(s) for s in scopes]
        # client_id absent => Dynamic Client Registration (RFC 7591) at connect time.
        self.client_id: str | None = definition.get("client_id")
        self.client_secret: str | None = definition.get("client_secret")


def _is_valid_def(key: str, definition: dict[str, Any]) -> bool:
    """Validate an oauth entry: streamable-HTTP transport with an https URL."""
    if definition.get("type") != "http":
        logger.error("MCP_CATALOG_OAUTH2 entry %r must have type 'http', skipping", key)
        return False
    url = definition.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        logger.error("MCP_CATALOG_OAUTH2 entry %r must have an https url, skipping", key)
        return False
    return True


def parse_oauth_catalog(raw: str) -> dict[str, OAuthMcpServer]:
    """
    Parse ``MCP_CATALOG_OAUTH2`` into ``{key: OAuthMcpServer}``.

    Tolerant by design (same contract as :func:`app.services.mcp_catalog.parse_catalog`):
    a malformed catalog or a single bad entry is logged and skipped, never fatal — a
    broken catalog yields fewer selectable servers, it does not take the app down.
    """
    if not raw.strip():
        return {}

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("MCP_CATALOG_OAUTH2 is not valid JSON, ignoring it: %s", e)
        return {}

    if not isinstance(entries, dict):
        logger.error(
            "MCP_CATALOG_OAUTH2 must be a JSON object {key: base64(def)}, got %s", type(entries)
        )
        return {}

    catalog: dict[str, OAuthMcpServer] = {}
    for key, encoded in entries.items():
        if not isinstance(encoded, str):
            logger.error("MCP_CATALOG_OAUTH2 entry %r must be a base64 string, skipping", key)
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True)
            definition = json.loads(decoded)
        except (binascii.Error, ValueError) as e:
            logger.error(
                "MCP_CATALOG_OAUTH2 entry %r is not valid base64(JSON), skipping: %s", key, e
            )
            continue
        if not isinstance(definition, dict) or not _is_valid_def(key, definition):
            continue
        catalog[key] = OAuthMcpServer(key, definition)

    return catalog


def get_oauth_catalog() -> dict[str, OAuthMcpServer]:
    """Return the parsed OAuth2 MCP catalog ``{key: OAuthMcpServer}`` from settings."""
    return parse_oauth_catalog(get_settings().mcp_catalog_oauth2)


def oauth_catalog_keys() -> list[str]:
    """Return the available OAuth2 MCP keys (safe to expose to the browser)."""
    return sorted(get_oauth_catalog().keys())


def get_oauth_server(key: str) -> OAuthMcpServer | None:
    """Return a single OAuth2 MCP catalog entry, or None if the key is unknown."""
    return get_oauth_catalog().get(key)
