"""
Conversation sharing / collaboration endpoints.

Invite system users to a session, accept/decline invitations, list participants
and revoke access. Builds on the access helpers in ``sessions.py``
(``check_session_owner``) and the per-user pubsub channel (``notify``) that already
powers the session-sync WebSocket: the invite / accept / revoke events ride it to
the right user's tabs, where the popup and sidebar react.
"""

import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import CurrentUser, get_db
from app.api.routes.chat import close_share_link_connections, close_user_session_connections
from app.models.schemas import (
    ParticipantResponse,
    PendingInviteResponse,
    ShareCreate,
    SharedConversationResponse,
    SharedMessage,
    ShareLinkCreate,
    ShareLinkResponse,
    ShareRespond,
    UserSummary,
)
from app.models.session import Session
from app.models.session_share import SessionShare, ShareRole, ShareStatus
from app.models.session_share_link import SessionShareLink
from app.models.user import User
from app.services.pubsub import notify
from app.services.session_access import check_real_owner, check_session_owner

router = APIRouter(tags=["sharing"])

# Path (same-origin) of the public read-only viewer; the frontend prepends its
# origin. Kept relative so it is correct for the combined prod image and dev alike.
_SHARE_LINK_PATH = "/shared/{token}"


async def _load_session_as_owner(session_id: UUID, user: CurrentUser, db: AsyncSession) -> Session:
    """Fetch a session and enforce owner/admin, or raise 404/403.

    Inviting, listing participants and revoking are all owner-only — participants
    never re-share a conversation.
    """
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found",
        )
    check_session_owner(session, user)
    return session


async def _load_session_as_real_owner(
    session_id: UUID, user: CurrentUser, db: AsyncSession
) -> Session:
    """Like ``_load_session_as_owner`` but excludes admins — for GRANTING access.

    Sharing a conversation (inviting a user, minting a public link) must be the real
    owner's decision; an admin inspector re-sharing someone else's private session would be
    a confidentiality escalation. Revoke/list remain owner-or-admin for break-glass.
    """
    result = await db.execute(select(Session).where(Session.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session with ID '{session_id}' not found",
        )
    check_real_owner(session, user)
    return session


@router.get("/users", response_model=list[UserSummary])
async def list_shareable_users(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    """Active users the caller can share a conversation with (excludes self).

    Available to any authenticated user — the share picker needs it, and Inkwest
    targets trusted single-org deployments where display names/emails are not sensitive.
    """
    result = await db.execute(
        select(User)
        .where(User.is_active == True, User.id != str(current_user.id))  # noqa: E712
        .order_by(User.display_name)
    )
    return list(result.scalars().all())


@router.post(
    "/sessions/{session_id}/shares",
    response_model=ParticipantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_share(
    session_id: UUID,
    body: ShareCreate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ParticipantResponse:
    """Invite a user to collaborate, as viewer or editor. Real owner only (not admins)."""
    session = await _load_session_as_real_owner(session_id, current_user, db)

    if str(body.user_id) == str(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already own this session",
        )

    target = await db.scalar(select(User).where(User.id == str(body.user_id)))
    if target is None or not target.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # One standing invite/membership per (session, user): re-inviting a declined or
    # role-changed participant updates the row back to pending rather than erroring.
    share = await db.scalar(
        select(SessionShare).where(
            SessionShare.session_id == session_id,
            SessionShare.user_id == str(body.user_id),
        )
    )
    if share is not None:
        if share.status == ShareStatus.ACCEPTED.value:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User already has access to this session",
            )
        # Shares are read-only: only the owner sends. Re-inviting resets to pending.
        share.role = ShareRole.VIEWER.value
        share.status = ShareStatus.PENDING.value
        share.invited_by_user_id = current_user.id
        share.responded_at = None
    else:
        share = SessionShare(
            session_id=session_id,
            user_id=body.user_id,
            invited_by_user_id=current_user.id,
            role=ShareRole.VIEWER.value,
            status=ShareStatus.PENDING.value,
        )
        db.add(share)

    await db.flush()
    await db.refresh(share)
    await db.commit()

    # Invite popup: rides the per-user sync channel to the invitee's tabs.
    await notify(
        {
            "type": "share_invite",
            "share_id": str(share.id),
            "session_id": str(session_id),
            "session_title": session.title,
            "role": share.role,
            "invited_by": current_user.display_name,
        },
        user_id=str(body.user_id),
    )

    return ParticipantResponse(
        id=share.id,
        session_id=session_id,
        user_id=target.id,
        display_name=target.display_name,
        email=target.email,
        role=share.role,
        status=share.status,
        created_at=share.created_at,
        responded_at=share.responded_at,
    )


@router.get("/sessions/{session_id}/shares", response_model=list[ParticipantResponse])
async def list_shares(
    session_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ParticipantResponse]:
    """List the participants and pending invitees of a session. Owner only."""
    await _load_session_as_owner(session_id, current_user, db)

    result = await db.execute(
        select(SessionShare, User)
        .join(User, User.id == SessionShare.user_id)
        .where(SessionShare.session_id == session_id)
        .order_by(SessionShare.created_at)
    )
    return [
        ParticipantResponse(
            id=share.id,
            session_id=share.session_id,
            user_id=user.id,
            display_name=user.display_name,
            email=user.email,
            role=share.role,
            status=share.status,
            created_at=share.created_at,
            responded_at=share.responded_at,
        )
        for share, user in result.all()
    ]


@router.delete(
    "/sessions/{session_id}/shares/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_share(
    session_id: UUID,
    user_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a participant or pending invitee. Owner only.

    Drops the row, closes any live socket the revoked user holds on this session,
    and refreshes their sidebar.
    """
    session = await _load_session_as_owner(session_id, current_user, db)

    share = await db.scalar(
        select(SessionShare).where(
            SessionShare.session_id == session_id,
            SessionShare.user_id == str(user_id),
        )
    )
    if share is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")

    await db.delete(share)
    await db.commit()

    # Kick their open conversation socket(s), then drop it from their sidebar and
    # tell them (the title lets the client show a polite "no longer shared" notice).
    await close_user_session_connections(str(session_id), str(user_id))
    await notify(
        {"type": "share_revoked", "session_id": str(session_id), "session_title": session.title},
        user_id=str(user_id),
    )


@router.delete("/sessions/{session_id}/share", status_code=status.HTTP_204_NO_CONTENT)
async def leave_share(
    session_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Leave a conversation that was shared with you (remove your own access).

    Self-only by construction (acts on the caller's own share), so no owner check —
    this is the participant-side counterpart to the owner's delete. The owner's copy
    is untouched. Emits ``session_left`` (not ``share_revoked``) so the leaver's own
    tabs just drop it from the sidebar without a "your access was removed" popup.
    """
    share = await db.scalar(
        select(SessionShare).where(
            SessionShare.session_id == session_id,
            SessionShare.user_id == str(current_user.id),
        )
    )
    if share is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You are not a participant of this conversation",
        )

    await db.delete(share)
    await db.commit()

    await notify(
        {"type": "session_left", "session_id": str(session_id)},
        user_id=str(current_user.id),
    )


@router.get("/shares/pending", response_model=list[PendingInviteResponse])
async def list_pending_invites(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[PendingInviteResponse]:
    """Pending invitations addressed to the caller — popup recovery on load."""
    result = await db.execute(
        select(SessionShare, Session, User)
        .join(Session, Session.id == SessionShare.session_id)
        # Outer join: the inviter may have been deleted (invited_by SET NULL).
        .join(User, User.id == SessionShare.invited_by_user_id, isouter=True)
        .where(
            SessionShare.user_id == str(current_user.id),
            SessionShare.status == ShareStatus.PENDING.value,
        )
        .order_by(SessionShare.created_at.desc())
    )
    return [
        PendingInviteResponse(
            share_id=share.id,
            session_id=session.id,
            session_title=session.title,
            role=share.role,
            invited_by_display_name=inviter.display_name if inviter else None,
            created_at=share.created_at,
        )
        for share, session, inviter in result.all()
    ]


@router.post("/shares/{share_id}/respond")
async def respond_to_invite(
    share_id: UUID,
    body: ShareRespond,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Accept or decline an invitation. Only the invitee may respond."""
    share = await db.scalar(select(SessionShare).where(SessionShare.id == share_id))
    # A foreign user's invite is hidden behind 404 (don't confirm its existence).
    if share is None or str(share.user_id) != str(current_user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    if share.status != ShareStatus.PENDING.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Invitation already responded to",
        )

    share.status = ShareStatus.ACCEPTED.value if body.accept else ShareStatus.DECLINED.value
    share.responded_at = func.now()
    await db.commit()

    # Notify the owner (participant list refresh) and the invitee's other tabs
    # (the session now appears in / disappears from their sidebar).
    session = await db.scalar(select(Session).where(Session.id == share.session_id))
    owner_id = str(session.user_id) if session and session.user_id else None
    event = {
        "type": "share_responded",
        "session_id": str(share.session_id),
        "status": share.status,
    }
    if owner_id:
        await notify(event, user_id=owner_id)
    await notify(event, user_id=str(current_user.id))

    return {"status": share.status, "session_id": str(share.session_id)}


# ---------------------------------------------------------------------------
# Revokable anonymous share links
# ---------------------------------------------------------------------------


def _link_response(link: SessionShareLink) -> ShareLinkResponse:
    return ShareLinkResponse(
        id=link.id,
        token=link.token,
        url=_SHARE_LINK_PATH.format(token=link.token),
        label=link.label,
        is_active=link.is_active,
        created_at=link.created_at,
    )


@router.post(
    "/sessions/{session_id}/share-links",
    response_model=ShareLinkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_share_link(
    session_id: UUID,
    body: ShareLinkCreate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ShareLinkResponse:
    """Mint a revokable anonymous read-only link to this conversation. Real owner only.

    Admins are excluded here on purpose: a public link is an unauthenticated exposure of the
    full transcript, so only the conversation's actual owner may create one.
    """
    await _load_session_as_real_owner(session_id, current_user, db)
    link = SessionShareLink(
        session_id=session_id,
        token=secrets.token_urlsafe(32),
        created_by_user_id=current_user.id,
        label=body.label,
    )
    db.add(link)
    await db.flush()
    await db.refresh(link)
    await db.commit()
    return _link_response(link)


@router.get("/sessions/{session_id}/share-links", response_model=list[ShareLinkResponse])
async def list_share_links(
    session_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ShareLinkResponse]:
    """List the active share links for a conversation. Owner only."""
    await _load_session_as_owner(session_id, current_user, db)
    result = await db.execute(
        select(SessionShareLink)
        .where(
            SessionShareLink.session_id == session_id,
            SessionShareLink.is_active == True,  # noqa: E712
        )
        .order_by(SessionShareLink.created_at.desc())
    )
    return [_link_response(link) for link in result.scalars().all()]


@router.delete(
    "/sessions/{session_id}/share-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_share_link(
    session_id: UUID,
    link_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a share link and disconnect any anonymous viewers using it. Owner only."""
    await _load_session_as_owner(session_id, current_user, db)
    link = await db.scalar(
        select(SessionShareLink).where(
            SessionShareLink.id == link_id,
            SessionShareLink.session_id == session_id,
        )
    )
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share link not found")

    if link.is_active:
        link.is_active = False
        link.revoked_at = func.now()
        await db.commit()

    # Kick any anonymous live viewers holding this token.
    await close_share_link_connections(link.token)


@router.get("/shared/{token}", response_model=SharedConversationResponse)
async def get_shared_conversation(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> SharedConversationResponse:
    """Anonymous read-only view of a conversation via a share-link token.

    Public — no auth: the token is the credential. Returns the conversation
    transcript via a deliberately narrow projection (see ``SharedMessage``): internal
    fields (thinking, token counts, model/engine, author ids, error/retry) are never
    serialized, and 'system' bookkeeping lines are dropped. Note the transcript still
    includes the assistant's answers and the tool calls/results the owner ran — i.e.
    the investigation's findings, connector output included. Sharing a link therefore
    exposes those to anyone holding it; it is not a redacted view.
    """
    link = await db.scalar(
        select(SessionShareLink).where(
            SessionShareLink.token == token,
            SessionShareLink.is_active == True,  # noqa: E712
        )
    )
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This share link is invalid or has been revoked",
        )

    result = await db.execute(
        select(Session)
        .where(Session.id == link.session_id)
        .options(selectinload(Session.messages), selectinload(Session.user))
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    return SharedConversationResponse(
        session_id=session.id,
        title=session.title,
        owner_display_name=session.user.display_name if session.user else None,
        # Minimal projection only (see SharedMessage): never serialize internal fields
        # to the unauthenticated viewer. 'system' event lines are excluded too.
        messages=[
            SharedMessage(id=m.id, role=m.role, content=m.content, created_at=m.created_at)
            for m in session.messages
            if m.role != "system"
        ],
    )
