"""
Role to Kubeconfig mapping model.

Maps user roles to allowed clusters and namespace restrictions.
"""

import uuid
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RoleKubeconfigMapping(Base):
    """
    Maps OIDC roles to cluster access permissions.

    Defines which clusters a role can access and optional namespace restrictions.
    """

    __tablename__ = "role_kubeconfig_mappings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Role identifier (matches OIDC roles)
    role_name: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="Role name from OIDC claims",
    )

    # Description for admin UI
    description: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )

    # Allowed clusters (UUIDs of clusters this role can access)
    allowed_clusters: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        default=list,
        comment="List of cluster IDs this role can access",
    )

    # Optional: Override kubeconfig path for this role
    kubeconfig_override: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
        comment="Optional kubeconfig path override for this role",
    )

    # Namespace restrictions (empty = all namespaces)
    allowed_namespaces: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        default=list,
        comment="Namespace restrictions (empty = all)",
    )

    # Denied namespaces (takes precedence over allowed)
    denied_namespaces: Mapped[list[str]] = mapped_column(
        ARRAY(String),
        nullable=False,
        default=list,
        comment="Explicitly denied namespaces",
    )

    can_web_search: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    # Token budget for this role (per day, 0 = use global default)
    daily_token_budget: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
        comment="Daily token budget (0 = use default)",
    )

    # Is this mapping active?
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

    def can_access_namespace(self, namespace: str) -> bool:
        """
        Check if this role can access a specific namespace.

        Args:
            namespace: The namespace to check

        Returns:
            True if access is allowed, False otherwise
        """
        # Denied takes precedence
        if namespace in self.denied_namespaces:
            return False

        # If allowed list is empty, allow all (except denied)
        if not self.allowed_namespaces:
            return True

        # Otherwise, must be in allowed list
        return namespace in self.allowed_namespaces

    def __repr__(self) -> str:
        return f"<RoleKubeconfigMapping {self.role_name} clusters={len(self.allowed_clusters)}>"
