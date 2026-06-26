"""
Budget and usage tracking models.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BudgetType(str, Enum):
    """Budget scope types."""

    GLOBAL = "global"  # Applies to entire system
    USER = "user"  # Applies to specific user
    ROLE = "role"  # Applies to all users with a role


class BudgetPeriod(str, Enum):
    """Budget period types."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Budget(Base):
    """
    Budget configuration for token/cost limits.

    Can be global, per-user, or per-role.
    """

    __tablename__ = "budgets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Budget identification
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable budget name",
    )
    description: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    # Budget scope.
    # values_callable: store/bind the lowercase enum *values* ("user") — the DB enum
    # type was created with those, not the member names ("USER"). Without this,
    # SQLAlchemy binds the name and every insert/filter fails with
    # InvalidTextRepresentationError ("invalid input value for enum budgettype").
    budget_type: Mapped[BudgetType] = mapped_column(
        SQLEnum(BudgetType, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        index=True,
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="User ID for user budget, null for global",
    )
    target_role: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
        comment="Role name for role-based budget",
    )

    # Token limits
    max_input_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Max input tokens per period",
    )
    max_output_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Max output tokens per period",
    )
    max_total_tokens: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Max total tokens per period",
    )

    # Cost limits (in USD)
    max_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4),
        nullable=True,
        comment="Max cost in USD per period",
    )

    # Period configuration (same lowercase-value mapping as budget_type above).
    period: Mapped[BudgetPeriod] = mapped_column(
        SQLEnum(BudgetPeriod, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=BudgetPeriod.DAILY,
    )

    # Warning threshold (percentage)
    soft_limit_percent: Mapped[int] = mapped_column(
        Integer,
        default=80,
        nullable=False,
        comment="Warn at this percentage of limit",
    )

    # Actions at limit
    hard_limit_action: Mapped[str] = mapped_column(
        String(50),
        default="block",
        nullable=False,
        comment="Action at 100%: block, warn, notify",
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<Budget {self.name} type={self.budget_type.value}>"


class UsageRecord(Base):
    """
    Records token usage for budget tracking.

    Captures every API call with token counts and costs.
    """

    __tablename__ = "usage_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Who and what
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="User who made the request (null if unauthenticated)",
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Token usage
    input_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Model info
    model: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="anthropic, bedrock, azure, etc.",
    )

    # Calculated cost
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6),
        nullable=False,
        default=Decimal("0"),
    )

    # Timestamp
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    def __repr__(self) -> str:
        return f"<UsageRecord user={self.user_id} tokens={self.input_tokens + self.output_tokens}>"
