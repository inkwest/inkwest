"""
User model for OIDC authentication.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.role_mapping import RoleKubeconfigMapping
    from app.models.session import Session


class User(Base):
    """
    User model representing authenticated users via OIDC.

    Stores user information from OIDC provider and local role assignments.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # OIDC identity
    oidc_subject: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="OIDC 'sub' claim - unique identifier from provider",
    )
    oidc_issuer: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="OIDC issuer URL",
    )

    # User info
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Roles from OIDC claims + local assignments
    roles: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        default=list,
        comment="User roles: admin, operator, user, viewer",
    )

    # Status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    # Timestamps
    last_login: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
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

    # Relationships
    sessions: Mapped[list["Session"]] = relationship(
        "Session",
        back_populates="user",
        lazy="selectin",
    )

    def has_role(self, role: str) -> bool:
        """Check if user has a specific role."""
        return role in self.roles

    def is_admin(self) -> bool:
        """Check if user has the configured admin role."""
        from app.core.config import get_settings

        return self.has_role(get_settings().oidc_admin_role)

    def can_access_cluster(
        self, cluster_id: str, role_mappings: list["RoleKubeconfigMapping"]
    ) -> bool:
        """
        Check if user can access a specific cluster based on role mappings.

        Args:
            cluster_id: The cluster UUID to check
            role_mappings: List of RoleKubeconfigMapping objects

        Returns:
            True if user has access, False otherwise
        """
        # Admins can access everything
        if self.is_admin():
            return True

        # Check role mappings
        for mapping in role_mappings:
            if mapping.role_name in self.roles:
                if cluster_id in [str(c) for c in mapping.allowed_clusters]:
                    return True

        return False

    def __repr__(self) -> str:
        return f"<User {self.email} roles={self.roles}>"
