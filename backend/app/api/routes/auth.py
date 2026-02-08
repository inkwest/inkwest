"""
Authentication API routes.

Handles OIDC login flow and session management.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_db
from app.core.auth import (
    AuthError,
    create_session_token,
    create_ws_ticket,
    get_oidc_client,
    verify_session_token,
)
from app.core.config import get_settings
from app.core.constants import OIDC_STATE_TTL_SECONDS
from app.models.oidc_pending_state import OidcPendingState
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

_MAX_PENDING_STATES = 10000


class TokenResponse(BaseModel):
    """Response with session token."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = 86400  # 24 hours


class WsTicketResponse(BaseModel):
    """Response with short-lived WebSocket ticket."""

    ticket: str


class UserInfoResponse(BaseModel):
    """Response with user information."""

    id: str
    email: str
    display_name: str
    roles: list[str]
    is_admin: bool


class AuthStatusResponse(BaseModel):
    """Response for auth status check."""

    oidc_enabled: bool
    authenticated: bool
    login_redirect_mode: str = "prompt"
    user: UserInfoResponse | None = None


@router.get("/status", response_model=AuthStatusResponse)
async def get_auth_status(
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> AuthStatusResponse:
    """
    Check authentication status.

    Returns whether OIDC is enabled and if the current request is authenticated.
    In single-user mode (OIDC disabled), returns the dev user as authenticated.
    """
    settings = get_settings()
    oidc_client = get_oidc_client()
    mode = settings.login_redirect_mode

    if not oidc_client.is_enabled:
        from app.api.dependencies import get_or_create_dev_user

        user = await get_or_create_dev_user(db)
        await db.commit()
        return AuthStatusResponse(
            oidc_enabled=False,
            authenticated=True,
            login_redirect_mode=mode,
            user=UserInfoResponse(
                id=str(user.id),
                email=user.email,
                display_name=user.display_name,
                roles=user.roles,
                is_admin=user.is_admin(),
            ),
        )

    if not authorization or not authorization.startswith("Bearer "):
        logger.debug("auth_status: no bearer token, unauthenticated")
        return AuthStatusResponse(oidc_enabled=True, authenticated=False, login_redirect_mode=mode)

    token = authorization[7:]
    payload = verify_session_token(token)

    if not payload:
        logger.debug("auth_status: invalid/expired token")
        return AuthStatusResponse(oidc_enabled=True, authenticated=False, login_redirect_mode=mode)

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id))
    db_user = result.scalar_one_or_none()

    if not db_user or not db_user.is_active:
        logger.debug("auth_status: user not found or inactive, id=%s", user_id)
        return AuthStatusResponse(oidc_enabled=True, authenticated=False, login_redirect_mode=mode)

    logger.info(
        "auth_status: authenticated user=%s roles=%s is_admin=%s",
        db_user.email,
        db_user.roles,
        db_user.is_admin(),
    )

    return AuthStatusResponse(
        oidc_enabled=True,
        authenticated=True,
        login_redirect_mode=mode,
        user=UserInfoResponse(
            id=str(db_user.id),
            email=db_user.email,
            display_name=db_user.display_name,
            roles=db_user.roles,
            is_admin=db_user.is_admin(),
        ),
    )


@router.get("/login")
async def login(db: AsyncSession = Depends(get_db)) -> RedirectResponse:
    """
    Initiate OIDC login flow.

    Redirects to the OIDC provider's authorization endpoint.
    """
    oidc_client = get_oidc_client()

    if not oidc_client.is_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OIDC authentication is not enabled",
        )

    cutoff = datetime.now(UTC) - timedelta(seconds=OIDC_STATE_TTL_SECONDS)
    await db.execute(delete(OidcPendingState).where(OidcPendingState.created_at < cutoff))

    state = secrets.token_urlsafe(32)
    # Atomic INSERT guarded by count check to avoid TOCTOU race
    result = await db.execute(
        text("""
            INSERT INTO inkwest.oidc_pending_states (state)
            SELECT :state
            WHERE (SELECT count(*) FROM inkwest.oidc_pending_states) < :max_states
            RETURNING id
        """),
        {"state": state, "max_states": _MAX_PENDING_STATES},
    )
    if result.first() is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many pending login requests",
        )
    await db.commit()

    try:
        auth_url = await oidc_client.get_authorization_url(state)
        return RedirectResponse(url=auth_url)
    except AuthError as e:
        logger.error(f"Failed to get authorization URL: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate login",
        )


@router.get("/callback")
async def callback(
    code: str = Query(..., description="Authorization code from OIDC provider"),
    state: str = Query(..., description="State for CSRF protection"),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """
    Handle OIDC callback.

    Exchanges authorization code for tokens and redirects to the frontend
    with the session token in the URL fragment.
    """
    settings = get_settings()
    oidc_client = get_oidc_client()
    frontend_callback = f"{settings.frontend_url}/auth/callback"

    if not oidc_client.is_enabled:
        return RedirectResponse(url=f"{frontend_callback}#error=OIDC+not+enabled")

    # Atomic pop: DELETE RETURNING to consume and validate in one query
    state_result = await db.execute(
        delete(OidcPendingState)
        .where(OidcPendingState.state == state)
        .returning(OidcPendingState.created_at)
    )
    row = state_result.first()

    cutoff = datetime.now(UTC) - timedelta(seconds=OIDC_STATE_TTL_SECONDS)
    await db.execute(delete(OidcPendingState).where(OidcPendingState.created_at < cutoff))
    await db.commit()

    if row is None or row[0] < cutoff:
        return RedirectResponse(url=f"{frontend_callback}#error=Invalid+or+expired+login+state")

    try:
        token_response = await oidc_client.exchange_code(code)
        id_token = token_response.get("id_token")

        if not id_token:
            raise AuthError("No ID token in response")

        claims = await oidc_client.validate_id_token(id_token)
        user_info = oidc_client.extract_user_info(claims)

        user_result = await db.execute(
            select(User).where(User.oidc_subject == user_info["oidc_subject"])
        )
        user = user_result.scalar_one_or_none()

        if user:
            user.email = user_info["email"]
            user.display_name = user_info["display_name"]
            user.roles = user_info["roles"]
            user.last_login = datetime.now(UTC)
        else:
            user = User(
                oidc_subject=user_info["oidc_subject"],
                oidc_issuer=settings.oidc_issuer_url,
                email=user_info["email"],
                display_name=user_info["display_name"],
                roles=user_info["roles"],
                last_login=datetime.now(UTC),
            )
            db.add(user)

        await db.commit()
        await db.refresh(user)

        if not user.is_active:
            return RedirectResponse(url=f"{frontend_callback}#error=Account+disabled")

        session_token = create_session_token(uuid.UUID(str(user.id)), user.roles)

        logger.info(f"User logged in: {user.email}")

        return RedirectResponse(url=f"{frontend_callback}#token={session_token}")

    except AuthError as e:
        logger.error(f"Authentication failed: {e}")
        from urllib.parse import quote

        return RedirectResponse(url=f"{frontend_callback}#error={quote(str(e))}")


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    """
    Logout the current user.

    Clears the session token. For full logout, the frontend should also
    redirect to the OIDC provider's logout endpoint.
    """
    # In a cookie-based implementation, we would clear the cookie here
    # For bearer token auth, the frontend just discards the token
    return {"message": "Logged out successfully"}


@router.post("/ws-ticket", response_model=WsTicketResponse)
async def get_ws_ticket(current_user: CurrentUser) -> WsTicketResponse:
    """Issue a short-lived JWT ticket for WebSocket authentication."""
    ticket = create_ws_ticket(uuid.UUID(str(current_user.id)))
    return WsTicketResponse(ticket=ticket)


@router.get("/me", response_model=UserInfoResponse)
async def get_me(current_user: CurrentUser) -> UserInfoResponse:
    """Get current user information."""
    return UserInfoResponse(
        id=str(current_user.id),
        email=current_user.email,
        display_name=current_user.display_name,
        roles=current_user.roles,
        is_admin=current_user.is_admin(),
    )
