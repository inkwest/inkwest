"""
OAuth2 MCP catalog: admin-managed remote MCP servers reachable via the OAuth 2.1
broker (discover -> DCR -> PKCE -> token).

Source of truth for which OAuth2 MCP servers users may connect to. Replaces the
``MCP_CATALOG_OAUTH2`` env var, which is seeded into this table once on first
startup. ``client_secret`` is never serialized to the browser. See
app.services.mcp_oauth_catalog.
"""

from typing import Any

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class McpOauthCatalog(Base, TimestampMixin):
    """A configured OAuth2 MCP server (admin catalog entry)."""

    __tablename__ = "mcp_oauth_catalog"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # NULL = fall back to the key for display.
    display_name: Mapped[str | None] = mapped_column(String(255))
    # Streamable-HTTP MCP endpoint; https enforced in the service layer.
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    # [] = let the authorization server grant its default scope set.
    scopes: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # NULL => Dynamic Client Registration at connect time.
    client_id: Mapped[str | None] = mapped_column(String(512))
    # NULL for public clients. Never exposed to the browser.
    client_secret: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # created_at / updated_at come from TimestampMixin.
