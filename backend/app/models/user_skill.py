"""
Personal (per-user) skill model.

Mirrors the admin :class:`app.models.skill.Skill` shape (name/content/description/
is_active) so the SKILL.md parsing (``app.core.skill_md``) and the CRUD UI are reused
verbatim. A personal skill is global to its owner (active in all their sessions) and,
when its slug matches an admin skill, **shadows** it ("personal always wins").
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class UserSkill(Base, TimestampMixin):
    """A user-authored SKILL.md, owned by one user."""

    __tablename__ = "user_skills"
    __table_args__ = (UniqueConstraint("user_id", "name", name="user_skills_user_id_name_key"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # True => shadows an admin skill of the same name; drives the "override user" badge.
    is_override: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Provenance when forked from an admin skill (SET NULL on that skill's delete).
    source_admin_skill_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("skills.id", ondelete="SET NULL"),
        nullable=True,
    )
