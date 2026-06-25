"""
Session sharing / collaboration model.

A session keeps its single owner (``sessions.user_id``). This table records the
extra participants invited to collaborate on it; a session is "shared" iff it has
at least one ``accepted`` row. Kept relationship-free on purpose — callers query
it directly (and join ``users`` when they need invitee/inviter display names) so
nothing lazy-loads under the async session.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ShareRole(str, Enum):
    """What an accepted participant may do in the conversation."""

    VIEWER = "viewer"  # read-only: watches the live conversation, cannot send
    EDITOR = "editor"  # can send messages / drive the AI


class ShareStatus(str, Enum):
    """Lifecycle of an invitation."""

    PENDING = "pending"  # invited, awaiting the invitee's response (drives the popup)
    ACCEPTED = "accepted"  # invitee joined — grants access per role
    DECLINED = "declined"  # invitee declined; kept as a record, no access


class SessionShare(Base):
    """A user invited to collaborate on a session."""

    __tablename__ = "session_shares"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Inviter kept for provenance; SET NULL so deleting them never revokes access.
    invited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ShareRole.EDITOR.value,
        server_default=ShareRole.EDITOR.value,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ShareStatus.PENDING.value,
        server_default=ShareStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # CHECKs mirror the migration so model-built (SQLite) test schemas enforce them
    # too — a new role/status must be added in BOTH places or tests catch the drift.
    __table_args__ = (
        UniqueConstraint("session_id", "user_id", name="uq_session_shares_session_user"),
        CheckConstraint("role IN ('viewer', 'editor')", name="session_shares_role_check"),
        CheckConstraint(
            "status IN ('pending', 'accepted', 'declined')",
            name="session_shares_status_check",
        ),
    )
