"""
Per-tool MCP permission policy (two scopes).

``UserMcpToolPolicy`` holds a user's global defaults; ``SessionMcpToolPolicy`` holds
per-conversation overrides. A row with ``tool == SERVER_DEFAULT_TOOL`` ('*') is the
server-wide default for that scope; specific tool rows override it. See
``app.services.mcp_policy`` for the resolution order.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Sentinel tool name for the server-wide default row.
SERVER_DEFAULT_TOOL = "*"

# Allowed verdicts (mirrors the DB CHECK).
VERDICTS = ("allow", "deny", "ask")
# System default when no policy row matches at any scope.
DEFAULT_VERDICT = "ask"

_VERDICT_CHECK = "verdict IN ('allow', 'deny', 'ask')"


class UserMcpToolPolicy(Base, TimestampMixin):
    """A user's global per-tool permission default (persists across conversations)."""

    __tablename__ = "user_mcp_tool_policy"
    __table_args__ = (CheckConstraint(_VERDICT_CHECK, name="ck_user_mcp_tool_policy_verdict"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    server_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Tool name, or SERVER_DEFAULT_TOOL ('*') for the server-wide default.
    tool: Mapped[str] = mapped_column(String(255), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)


class SessionMcpToolPolicy(Base, TimestampMixin):
    """A per-conversation per-tool permission override."""

    __tablename__ = "session_mcp_tool_policy"
    __table_args__ = (CheckConstraint(_VERDICT_CHECK, name="ck_session_mcp_tool_policy_verdict"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    server_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    tool: Mapped[str] = mapped_column(String(255), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(8), nullable=False)
