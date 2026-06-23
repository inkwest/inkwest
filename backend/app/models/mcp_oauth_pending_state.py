import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class McpOauthPendingState(Base):
    """In-flight OAuth2 MCP handshake, consumed once on callback.

    Mirrors :class:`app.models.connector_pending_state.ConnectorPendingState` with
    ``mcp_key`` in place of the connector domain. Holds the CSRF ``state`` and the PKCE
    ``code_verifier`` so the (unauthenticated) AS redirect callback can be correlated to
    a user without a session token. Short-lived (TTL constant) and single-use.
    """

    __tablename__ = "mcp_oauth_pending_states"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    mcp_key: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
