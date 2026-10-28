"""
Announcement banner model (spec 004): an admin-written message shown at the top of
every page for every signed-in user. Bounds are CHECKs in the migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Announcement(Base):
    """One banner. ``enabled`` and ``ends_at`` decide whether the read route serves it."""

    __tablename__ = "announcements"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL = the level's own background color.
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    # NULL = automatic (black or white by background luminance).
    text_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    opacity: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="100")
    dismissible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    # Bumped on every edit: the front keys per-browser dismissal on (id, updated_at).
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
