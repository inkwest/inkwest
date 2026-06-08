"""
System prompt model: the instruction prompt loaded before any user interaction
of a session.
"""

import uuid

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SystemPrompt(Base, TimestampMixin):
    """A named system prompt managed by admins.

    System prompts are global (shared across all users). Exactly one row is
    flagged ``is_default`` and is the prompt non-admins always get. Admins may
    pick a specific prompt — or supply free-text "custom" — when creating a
    session (the choice is persisted on the session, not here).

    ``content`` may contain the ``{cluster_name}`` / ``{namespaces}`` blanks
    filled in at run time by ``app.services.prompts.get_system_prompt``.
    """

    __tablename__ = "system_prompts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )
    # At most one row may be true — enforced by a partial unique index in the
    # migration; the API also clears the previous default within a transaction.
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
