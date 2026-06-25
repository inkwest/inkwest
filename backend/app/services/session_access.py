"""
Shared session access-control helpers.

Used by the sessions + sharing routes and the chat WebSocket. Lives outside the
route modules so the chat WS can reuse it without the ``sessions.py`` ↔
``chat.py`` import cycle (sessions imports chat for connection teardown).

Roles: ``owner`` (the session owner), ``editor`` / ``viewer`` (an accepted
participant — see SessionShare). Only an *accepted* share grants access;
pending/declined never do. Admins get read-only (``viewer``) access to any
conversation for inspection (e.g. the usage report); management stays owner-only.
"""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session
from app.models.session_share import SessionShare, ShareStatus
from app.models.user import User


def is_real_owner(session: Session, user: User) -> bool:
    """True only if the user is the session's actual owner — admins excluded.

    Ownership drives read/write capability in the chat UI, where an admin is a
    read-only inspector, not the owner. Use this (not ``is_owner_or_admin``)
    wherever the distinction is "can drive / is this mine", e.g. the ``is_owner``
    flag. Management (delete/settings) is where admins act as owners — see
    ``check_session_owner``.
    """
    return session.user_id is not None and str(session.user_id) == str(user.id)


def is_owner_or_admin(session: Session, user: User) -> bool:
    """True if the user owns the session, or is an admin (admins access any session)."""
    return bool(user.is_admin()) or is_real_owner(session, user)


async def get_session_role(session: Session, user: User, db: AsyncSession) -> str | None:
    """The user's effective role: ``owner`` | ``editor`` | ``viewer`` | None (no access).

    Only the real owner gets ``owner``; an accepted participant gets their share
    role. An admin who is neither falls back to ``viewer`` — admins may inspect any
    conversation (e.g. from the admin usage report) but read-only: they watch turns,
    never drive them. Session *management* (delete/settings) is a separate concern
    kept with ``check_session_owner``/``is_owner_or_admin``, which still admit admins.
    """
    if is_real_owner(session, user):
        return "owner"
    # No User.is_active filter here on purpose: `user` is always the authenticated
    # caller, whose active status is already enforced upstream (get_current_user /
    # validate_websocket_auth reject inactive users before this runs). A join would
    # be a redundant query for a condition that can't be false at this point.
    role = await db.scalar(
        select(SessionShare.role).where(
            SessionShare.session_id == session.id,
            SessionShare.user_id == str(user.id),
            SessionShare.status == ShareStatus.ACCEPTED.value,
        )
    )
    if role is not None:
        return role
    # Admin read-only fallback: inspect any conversation, but never as its owner.
    return "viewer" if user.is_admin() else None


async def check_session_access(session: Session, user: User, db: AsyncSession) -> str:
    """Read access: owner, admin, or accepted participant. Returns the role; 403 if none."""
    role = await get_session_role(session, user, db)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this session",
        )
    return role


def check_session_owner(session: Session, user: User) -> None:
    """Owner/admin only — for session settings and lifecycle mutations.

    Participants (even editors) may send messages but never change a session's
    configuration, archive it, or delete it; those stay with the owner.
    """
    if not is_owner_or_admin(session, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the session owner can perform this action",
        )
