"""
Message model for chat messages.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.session import Session


class Message(Base):
    """Chat message model."""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Who sent this user turn, in a shared conversation. NULL = the AI (assistant /
    # tool_result rows), or a message that predates sharing. SET NULL on user delete
    # so history survives. Populated on the WS send path (see chat.py / orchestrators).
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    role: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )  # user, assistant, tool_result, system (UI-only events; never sent to the model)
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
    )
    tool_calls: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    tool_call_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    # Files attached to a user turn: a JSON list of descriptors
    # ({filename, original_name, content_type, size}). The files live in the
    # conversation's /workdir; this lets the chat re-render thumbnails on reload.
    # NULL for messages without attachments.
    attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
    )
    # The model that produced this (assistant) turn, captured at generation time.
    # Per-turn cost is model-dependent and the session model can change mid-chat,
    # so this is the only reliable source for accurate cost attribution. NULL for
    # non-assistant rows and for messages predating this column.
    model: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    # The agentic engine that produced this (assistant) turn (claude-cli |
    # inkwest-agent, or the provider transport for non-CLI deployments), captured
    # at generation time. The session engine can be NULL (resolve per turn) or
    # change mid-chat, so this is the only reliable record of what ran. NULL for
    # non-assistant rows and for messages predating this column.
    engine: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    token_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    input_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    output_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    # Prompt-cache splits for the turn (input-side only; input_tokens above is FRESH
    # input). NULL for rows predating the columns / turns with no reported usage.
    cache_read_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    cache_write_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    thinking_content: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )
    latency_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    # Status tracking for retry functionality
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="success",
        server_default="success",
    )  # pending, success, error
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    is_retryable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    session: Mapped["Session"] = relationship(
        "Session",
        back_populates="messages",
        lazy="selectin",
    )

    # Indexes + constraints. The role CHECK mirrors the DB migration so model-built
    # test schemas (SQLite) enforce it too — a new role must be added in BOTH places
    # (here and a migration), or tests catch the drift.
    __table_args__ = (
        Index("ix_messages_session_created", "session_id", "created_at"),
        CheckConstraint(
            "role IN ('user', 'assistant', 'tool_result', 'system')",
            name="messages_role_check",
        ),
    )
