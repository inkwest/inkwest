"""
Cached tool enumeration for an MCP server.

The backend enumerates a server's tools server-side (MCP ``initialize`` + ``tools/list``
over streamable-HTTP, with proxy-injected creds — see ``app.services.mcp_tools``) so the UI
can show every tool up front and the user can pre-set per-tool permissions before any turn
runs (avoids per-first-use prompting). The list is cached here, refreshed on connect or on
demand.

Scope: keyed by ``server_key`` only (server-global). If a server's advertised tool set ever
becomes user-specific (OAuth-scope divergence), re-key by (user_id, server_key) — deferred.
"""

from typing import Any

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class McpServerTools(Base, TimestampMixin):
    """Cached ``tools/list`` result for one MCP catalog server."""

    __tablename__ = "mcp_server_tools"

    # Catalog key (oauth or static); matches mcpServers[<key>].
    server_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # List of {name, description, input_schema} dicts, verbatim from the server.
    tools: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # created_at (first enumeration) / updated_at (last refresh) come from TimestampMixin;
    # updated_at is the "fetched_at" the UI shows as freshness.
