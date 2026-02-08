"""
API dependencies for dependency injection.
"""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import verify_session_token
from app.core.config import get_settings
from app.core.database import async_session_maker
from app.models.user import User


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_or_create_dev_user(db: AsyncSession) -> User:
    """Get or create the default dev user for single-user mode."""
    settings = get_settings()
    result = await db.execute(select(User).where(User.email == "dev@localhost"))
    user = result.scalar_one_or_none()
    if user:
        return user

    user = User(
        oidc_subject="dev-local",
        oidc_issuer="local",
        email="dev@localhost",
        display_name="Developer",
        roles=[settings.oidc_admin_role],
        is_active=True,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
) -> User:
    """
    Get the current authenticated user.

    In single-user mode (OIDC disabled), returns a dev user with admin role.
    In OIDC mode, validates the JWT from the Authorization header.
    """
    settings = get_settings()

    if not settings.oidc_enabled:
        return await get_or_create_dev_user(db)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    payload = verify_session_token(authorization[7:])
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    return user


async def require_admin(
    user: User = Depends(get_current_user),
) -> User:
    """Require the current user to have admin role."""
    if not user.is_admin():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user


async def get_user_allowed_cluster_ids(user: User, db: AsyncSession) -> set[str] | None:
    """
    Get the set of cluster IDs this user can access.

    Returns None if the user has unrestricted access (admin or no mappings configured).
    """
    from app.models.role_mapping import RoleKubeconfigMapping

    if user.is_admin():
        return None

    result = await db.execute(
        select(RoleKubeconfigMapping).where(
            RoleKubeconfigMapping.role_name.in_(user.roles),
            RoleKubeconfigMapping.is_active == True,  # noqa: E712
        )
    )
    mappings = result.scalars().all()

    if not mappings:
        # No mappings exist for user's roles - check if any mappings exist at all
        count_result = await db.execute(select(RoleKubeconfigMapping).limit(1))
        if count_result.scalar_one_or_none() is None:
            return None  # No mappings configured = unrestricted
        return set()  # Mappings exist but none match = no access

    allowed_ids: set[str] = set()
    for mapping in mappings:
        for cluster_id in mapping.allowed_clusters:
            allowed_ids.add(str(cluster_id))
    return allowed_ids


async def check_cluster_access(user: User, cluster_id: str, db: AsyncSession) -> None:
    """Raise 403 if user doesn't have access to the given cluster."""
    allowed = await get_user_allowed_cluster_ids(user, db)
    if allowed is not None and cluster_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this cluster",
        )


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
