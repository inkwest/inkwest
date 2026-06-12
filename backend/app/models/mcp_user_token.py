import uuid
from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class McpUserToken(Base):
    """A user's OAuth2 grant for one MCP server. Token MATERIAL is never plaintext here.

    ``blob`` is the AES-256-GCM ciphertext of ``{access_token, refresh_token?, …}``
    encrypted under the per-user DEK (see app.core.crypto / app.services.mcp_dek_store);
    ``blob_iv`` is its GCM nonce. ``vault_ref`` is the opaque handle so a future shared
    vault is a backend swap, not a schema change. ``status`` mirrors platform_grants:
    ``active | expired | revoked``.
    """

    __tablename__ = "mcp_user_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    mcp_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    vault_ref: Mapped[str] = mapped_column(Text, nullable=False)
    blob: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    blob_iv: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
