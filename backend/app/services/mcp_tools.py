"""
Backend-side MCP tool enumeration + cache.

Connects to an MCP server's real upstream (``initialize`` + ``tools/list`` over
streamable-HTTP) with the real credential resolved exactly like the proxy does — an OAuth
vault token (refreshed) or a static catalog's headers — and caches the result in
``mcp_server_tools``. This drives the per-tool permission UI: the user sees every tool up
front and pre-sets permissions instead of being prompted at first use.

The sandbox never participates here — enumeration is a backend→upstream call, so the real
credential stays server-side (same guarantee as the proxy). No stdio servers (all catalog
MCP is HTTP/SSE).
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_server_tools import McpServerTools
from app.services import mcp_oauth
from app.services.mcp_catalog import get_static_http_server
from app.services.mcp_oauth import McpOauthError, assert_public_https
from app.services.mcp_oauth_catalog import get_oauth_server

logger = logging.getLogger(__name__)

# Whole handshake + list budget. Generous — a cold MCP server (lambda cold-start, DCR) can be
# slow, but the UI awaits this so it can't hang forever.
_ENUM_TIMEOUT = 20.0


class McpEnumError(Exception):
    """Enumeration failed: unknown key, dead grant, or upstream/transport error."""


def _annotations(tool: Any) -> dict[str, Any] | None:
    """The tool's MCP behavior hints (read-only / destructive / …), or None if unannotated.

    Drives the UI's capability grouping (Read-only / Write-delete / Interactive) — the same
    axis Claude's own tool-permission UI groups on. Keys are snake_cased for our wire shape.
    """
    a = getattr(tool, "annotations", None)
    if a is None:
        return None
    return {
        "title": getattr(a, "title", None),
        "read_only": getattr(a, "readOnlyHint", None),
        "destructive": getattr(a, "destructiveHint", None),
        "idempotent": getattr(a, "idempotentHint", None),
        "open_world": getattr(a, "openWorldHint", None),
    }


def _normalize(tool: Any) -> dict[str, Any]:
    """One tool → the stable shape we cache and serve ({name, description, input_schema,
    annotations})."""
    raw = getattr(tool, "inputSchema", None)
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": raw if isinstance(raw, dict) else {},
        "annotations": _annotations(tool),
    }


async def _resolve_upstream(
    db: AsyncSession, key: str, user_id: UUID
) -> tuple[str, dict[str, str]]:
    """Resolve (upstream URL, headers-with-real-credential) for an oauth or static server.

    Mirrors ``mcp_proxy``'s credential swap so enumeration and live calls hit the same
    endpoint with the same auth. Raises ``McpEnumError`` for an unknown key or a dead grant.
    """
    server = get_oauth_server(key)
    if server is not None:
        try:
            token = await mcp_oauth.get_fresh_token(db, user_id=user_id, mcp_key=key)
        except McpOauthError as e:
            # Dead/expired grant or broker degraded — surface as an enum failure; the caller
            # maps it to "reconnect required" for the panel.
            raise McpEnumError(f"credential unavailable for {key}") from e
        return server.url, {"Authorization": f"Bearer {token}"}

    static_def = get_static_http_server(key)
    if static_def is not None:
        return static_def["url"], dict(static_def.get("headers") or {})

    raise McpEnumError(f"unknown MCP server: {key}")


async def _list_tools(url: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """Open a streamable-HTTP MCP session, initialize, and return the normalized tool list."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    # headers or None: an empty dict is falsy-equivalent for the SDK's optional headers arg.
    async with streamablehttp_client(url, headers=headers or None) as transport:
        read, write = transport[0], transport[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            return [_normalize(t) for t in listed.tools]


async def enumerate_tools(db: AsyncSession, *, key: str, user_id: UUID) -> list[dict[str, Any]]:
    """Connect to the server and return its tools. Does NOT persist. Raises McpEnumError."""
    url, headers = await _resolve_upstream(db, key, user_id)
    try:
        # SSRF guard. NOTE: unlike the proxy (mcp_oauth.pin_url), the mcp SDK opens its own
        # connection so we can't IP-pin the resolved address — a DNS-rebinding TOCTOU gap
        # remains between this check and connect. Accepted for now: the URL is admin-set (oauth
        # catalog / MCP_CATALOG), same trust as the proxy's static path; there is no
        # user-supplied URL here. Hardening (pinned httpx_client_factory) is a follow-up.
        assert_public_https(url)
    except McpOauthError as e:
        raise McpEnumError("MCP server URL rejected") from e
    try:
        return await asyncio.wait_for(_list_tools(url, headers), timeout=_ENUM_TIMEOUT)
    except McpEnumError:
        raise
    except Exception as e:  # any transport/protocol/timeout error → uniform enum failure
        logger.warning("mcp_enumerate_failed", extra={"mcp_key": key, "error": str(e)})
        raise McpEnumError(f"enumeration failed for {key}") from e


async def get_cached_tools(db: AsyncSession, key: str) -> McpServerTools | None:
    """The cached row for a server, or None if never enumerated."""
    return await db.get(McpServerTools, key)


async def refresh_tools(db: AsyncSession, *, key: str, user_id: UUID) -> list[dict[str, Any]]:
    """Enumerate then upsert the cache; returns the fresh tool list. Raises McpEnumError."""
    tools = await enumerate_tools(db, key=key, user_id=user_id)
    stmt = (
        pg_insert(McpServerTools)
        .values(server_key=key, tools=tools)
        .on_conflict_do_update(
            index_elements=[McpServerTools.server_key],
            set_={"tools": tools, "updated_at": func.now()},
        )
    )
    await db.execute(stmt)
    await db.commit()
    return tools
