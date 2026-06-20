"""
Session model for chat sessions.
"""

import uuid
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.cluster import Cluster
    from app.models.message import Message
    from app.models.user import User


class ConfirmationMode(str, Enum):
    """User confirmation modes for interactive flow."""

    NONE = "none"  # Fully automatic (current behavior)
    TOOLS_ONLY = "tools"  # Confirm each tool call
    EXPENSIVE = "expensive"  # Confirm when cost > threshold
    ITERATIONS = "iterations"  # Confirm every N iterations
    ALL = "all"  # Confirm everything


class Session(Base, TimestampMixin):
    """Chat session model."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Optional: a session may start without any Kubernetes access. Kube access
    # can also arrive later via AWS (EKS kubeconfigs), so a null cluster does
    # not mean kubectl is permanently unavailable for the session.
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("clusters.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # System prompt chosen at session creation. Both null → resolve the global
    # default. Custom free-text (admin only) takes precedence over the FK so an
    # admin can override without touching the managed library.
    system_prompt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_prompts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    system_prompt_custom: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    title: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )
    # Sidebar ordering: an explicit slot (ascending = top) changed only by an
    # explicit reorder, never by activity. New sessions get the smallest value
    # (see create_session) so they land on top.
    position: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Token usage statistics (aggregated)
    total_input_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    total_output_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    total_tool_calls: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )
    total_iterations: Mapped[int] = mapped_column(
        Integer,
        default=0,
        nullable=False,
    )

    # Claude CLI session ID for --resume functionality
    cli_session_id: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Claude CLI session ID for conversation resumption",
    )

    # Per-session model choice (from the admin-enabled list); NULL = use the
    # global default resolved at turn time. Changeable mid-session.
    model: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="AI model for this session; NULL = global default",
    )

    # Agentic engine for this session (claude-cli | inkwest-agent); NULL = resolve
    # per turn from AGENTIC_ENGINES + the model (see app.services.engines). Reserved
    # first-class now; a per-session engine picker is a next step (today: model-only
    # UI). See docs/SANDBOXED_AGENT_RUNTIME.md (decision #7).
    engine: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Agentic engine for this session; NULL = resolve per turn",
    )

    # Per-conversation MCP selection: catalog keys (see app.services.mcp_catalog)
    # the user enabled for this session. Merged into the conversation's
    # .claude.json mcpServers; persisted so the choice survives reload / --resume.
    enabled_mcp_servers: Mapped[list[str]] = mapped_column(
        JSONB,
        default=list,
        nullable=False,
        server_default="[]",
        comment="Catalog keys of MCP servers enabled for this conversation",
    )

    # Interactive flow settings
    confirmation_mode: Mapped[str] = mapped_column(
        String(50),
        default=ConfirmationMode.EXPENSIVE.value,
        nullable=False,
    )
    cost_confirmation_threshold: Mapped[int] = mapped_column(
        Integer,
        default=10,  # cents, so $0.10
        nullable=False,
        comment="Cost threshold in cents for confirmation",
    )
    iteration_confirmation_interval: Mapped[int] = mapped_column(
        Integer,
        default=5,
        nullable=False,
        comment="Request confirmation every N iterations",
    )

    # Relationships
    cluster: Mapped["Cluster | None"] = relationship(
        "Cluster",
        back_populates="sessions",
        lazy="selectin",
    )
    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="session",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    user: Mapped["User | None"] = relationship(
        "User",
        back_populates="sessions",
        lazy="selectin",
    )
