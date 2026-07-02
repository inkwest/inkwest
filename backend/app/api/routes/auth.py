"""
Authentication API routes.

Handles OIDC login flow and session management.
"""

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_db
from app.core import metrics
from app.core.auth import (
    OIDC_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthError,
    clear_oidc_state_cookie,
    clear_session_cookie,
    create_session_token,
    create_ws_ticket,
    get_oidc_client,
    set_oidc_state_cookie,
    set_session_cookie,
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
    request: Request,
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
        # Cookie-aware so the reported identity matches dev user switching
        # (/auth/dev-switch) — otherwise the header would still show the default
        # dev user while the rest of the app acts as the impersonated one.
        from app.api.dependencies import get_current_user

        user = await get_current_user(request, db)
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

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        logger.debug("auth_status: no session cookie, unauthenticated")
        return AuthStatusResponse(oidc_enabled=True, authenticated=False, login_redirect_mode=mode)

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
        redirect = RedirectResponse(url=auth_url)
        # Bind the state to this browser (CSRF): the callback requires the cookie to match
        # the state query param, so a stolen (code, state) can't be replayed in a victim's
        # browser.
        set_oidc_state_cookie(redirect, state, OIDC_STATE_TTL_SECONDS)
        return redirect
    except AuthError as e:
        logger.error(f"Failed to get authorization URL: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initiate login",
        )


@router.get("/callback")
async def callback(
    request: Request,
    code: str = Query(..., description="Authorization code from OIDC provider"),
    state: str = Query(..., description="State for CSRF protection"),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """
    Handle OIDC callback.

    Exchanges authorization code for tokens, sets the session JWT as an HttpOnly
    cookie, and redirects to the frontend. Errors redirect to the callback page
    with an ``#error=`` fragment.
    """
    settings = get_settings()
    oidc_client = get_oidc_client()
    frontend_callback = f"{settings.frontend_url}/auth/callback"

    if not oidc_client.is_enabled:
        return RedirectResponse(url=f"{frontend_callback}#error=OIDC+not+enabled")

    # CSRF: the state must match the cookie set at /login on THIS browser (constant-time
    # compare). Blocks a login-CSRF where an attacker's (code, state) is fed to a victim.
    cookie_state = request.cookies.get(OIDC_STATE_COOKIE_NAME)
    if not cookie_state or not secrets.compare_digest(cookie_state, state):
        resp = RedirectResponse(url=f"{frontend_callback}#error=Invalid+or+expired+login+state")
        clear_oidc_state_cookie(resp)
        return resp

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
        resp = RedirectResponse(url=f"{frontend_callback}#error=Invalid+or+expired+login+state")
        clear_oidc_state_cookie(resp)
        return resp

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
            metrics.auth_logins_total.labels(result="failure").inc()
            return RedirectResponse(url=f"{frontend_callback}#error=Account+disabled")

        session_token = create_session_token(uuid.UUID(str(user.id)), user.roles)

        logger.info(f"User logged in: {user.email}")
        metrics.auth_logins_total.labels(result="success").inc()

        # Token rides in an HttpOnly cookie, not the URL fragment — keeps it out
        # of browser history and unreadable by JS. Land on the SPA root directly.
        redirect = RedirectResponse(url=f"{settings.frontend_url}/")
        set_session_cookie(redirect, session_token)
        clear_oidc_state_cookie(redirect)  # single-use: state consumed
        return redirect

    except AuthError as e:
        logger.error(f"Authentication failed: {e}")
        metrics.auth_logins_total.labels(result="failure").inc()
        from urllib.parse import quote

        return RedirectResponse(url=f"{frontend_callback}#error={quote(str(e))}")


@router.post("/logout")
async def logout(response: Response, current_user: CurrentUser) -> dict[str, str]:
    """
    Logout the current user.

    Clears the HttpOnly session cookie. Requires the session cookie so a
    cross-site forged request (which SameSite=Lax strips of the cookie) can't
    log a victim out. For full logout, the frontend should also redirect to the
    OIDC provider's logout endpoint.
    """
    clear_session_cookie(response)
    return {"message": "Logged out successfully"}


@router.post("/ws-ticket", response_model=WsTicketResponse)
async def get_ws_ticket(current_user: CurrentUser) -> WsTicketResponse:
    """Issue a short-lived JWT ticket for WebSocket authentication."""
    ticket = create_ws_ticket(uuid.UUID(str(current_user.id)))
    return WsTicketResponse(ticket=ticket)


@router.get("/dev-switch")
async def dev_switch(response: Response, user: str = Query("")) -> dict[str, str]:
    """DEV ONLY (OIDC off): impersonate a local user via a cookie, for testing the
    multi-user features without an IdP.

    ``?user=bob`` → act as ``bob@localhost`` (a non-admin local account, created on
    first use); ``?user=`` (empty) → back to the default ``dev@localhost``. Open it
    in a separate browser profile / incognito window to hold a second identity.
    Returns 404 when OIDC is enabled, so it never exists in production.
    """
    settings = get_settings()
    if settings.oidc_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    name = re.sub(r"[^a-z0-9_-]", "", user.lower())[:32]
    secure = settings.app_env == "production"  # always False here (dev), kept for parity
    if name and name != "dev":
        response.set_cookie(
            "inkwest_dev_user", name, httponly=True, secure=secure, samesite="lax", path="/"
        )
        return {"acting_as": f"{name}@localhost"}
    response.delete_cookie("inkwest_dev_user", path="/")
    return {"acting_as": "dev@localhost"}


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
