"""
Per-user preferences document.

A light JSONB document holding UI settings (migrated off localStorage) and the set
of admin-skill names the user turned OFF. Heavy SKILL.md bodies live in
:class:`app.models.user_skill.UserSkill`, not here — so a UI toggle never rewrites a
fat blob. ``schema_version`` makes the document forward-migratable on import.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserPreferences(Base):
    """One preferences row per user (PK is the user id)."""

    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    document: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
