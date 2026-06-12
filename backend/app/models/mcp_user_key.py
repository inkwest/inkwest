import uuid
from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class McpUserKey(Base):
    """Browser-wrapped per-user DEK (audit-doc Phase C key custody).

    ``wrapped_dek`` is the per-user data-encryption key encrypted by a non-extractable
    WebCrypto key that lives ONLY in the user's browser (IndexedDB, per origin+profile).
    The server stores the wrapped form + IV (``wrap_iv``) and never the unwrapping key, so a
    DB+disk+env dump taken while nobody is connected cannot decrypt any token blob. The
    raw DEK is reconstructed in server RAM only while the user's WS is connected (see
    app.services.mcp_dek_store).
    """

    __tablename__ = "mcp_user_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrap_alg: Mapped[str] = mapped_column(String(32), nullable=False)
    wrap_iv: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
