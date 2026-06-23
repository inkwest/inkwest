from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class McpOauthClient(Base):
    """OAuth client + discovered endpoints for one OAuth2 MCP catalog key.

    One row per ``mcp_key``. ``client_id`` is either catalog-pinned or the result of
    RFC 7591 Dynamic Client Registration. ``token_endpoint`` / ``revocation_endpoint``
    are persisted at connect time so the refresh hot path never re-runs discovery.
    ``client_secret_ref`` is a vault handle (NULL for public clients).
    """

    __tablename__ = "mcp_oauth_clients"

    mcp_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    token_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    revocation_endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
