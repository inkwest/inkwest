"""
MCP catalog + OAuth2 brokering routes.

Two catalogs feed the panel:
- **static** (``MCP_CATALOG``): stdio/static-header servers (Coralogix, kubectl, …),
  merged verbatim into the per-conversation ``.claude.json``. Always usable.
- **oauth2** (``MCP_CATALOG_OAUTH2``): remote servers reached via the MCP Authorization
  spec; Inkwest brokers the OAuth flow per user (see app.services.mcp_oauth).

Only keys + a per-user ``auth_status`` ever leave the backend — never server definitions
or token material. The OAuth callback is deliberately unauthenticated (it is reached by an
AS redirect that carries no Authorization header); the single-use pending-state row is the
sole binding between the callback and a user.
"""

import html
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_db
from app.core.config import get_settings
from app.core.constants import MCP_OAUTH_STATE_TTL_SECONDS
from app.models.connector_audit import ConnectorAuditLog
from app.models.mcp_oauth_pending_state import McpOauthPendingState
from app.models.mcp_user_token import McpUserToken
from app.models.user import User
from app.services import mcp_oauth
from app.services.mcp_catalog import catalog_keys
from app.services.mcp_oauth import GrantDeadError, McpOauthError
from app.services.mcp_oauth_catalog import get_oauth_server, oauth_catalog_keys

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])

# Cap on a user's concurrent in-flight authorizations. Each auth/start runs live discovery
# (outbound egress) + inserts a pending-state row; without a cap a client could drive both
# unbounded. Generous enough that normal multi-server connecting never trips it.
_MAX_PENDING_PER_USER = 20

# auth_status vocabulary (see docs/mcp-oauth2-proxy.md work item 7):
#   static       — non-oauth entry, always usable
#   unavailable  — oauth entry but sandbox disabled (no per-conversation .claude.json)
#   not_connected— oauth entry, user has no grant row → panel shows "Connect"
#   connected    — grant active
#   needs_auth   — grant expired/revoked → panel shows "Reconnect"


class McpServerEntry(BaseModel):
    key: str
    kind: str  # static | oauth2
    auth_status: str


class McpCatalogResponse(BaseModel):
    # ``keys`` is the pre-OAuth contract (static, injectable catalog keys) — kept so the
    # existing panel/clients are untouched. ``servers`` is the OAuth-aware superset.
    keys: list[str]
    servers: list[McpServerEntry]


class AuthStartResponse(BaseModel):
    authorize_url: str


def _redirect_uri() -> str:
    s = get_settings()
    base = s.mcp_oauth_redirect_base or s.frontend_url
    return f"{base.rstrip('/')}/api/mcp/oauth/callback"


async def _audit(db: AsyncSession, *, user_id: uuid.UUID, mcp_key: str, action: str) -> None:
    # MCP grants are user-level: session_id is NULL (see migration 20260612140000).
    # connector="mcp" keeps the 32-char column valid; the key (up to 255) goes in scope.
    db.add(ConnectorAuditLog(user_id=user_id, connector="mcp", action=action, scope=mcp_key))
    await db.commit()


async def _build_catalog(db: AsyncSession, user: User) -> McpCatalogResponse:
    """Build the catalog response (static keys + oauth servers with per-user auth status)."""
    sandbox = get_settings().sandbox_enabled
    rows = (
        await db.execute(
            select(McpUserToken.mcp_key, McpUserToken.status).where(McpUserToken.user_id == user.id)
        )
    ).all()
    statuses = {key: status for key, status in rows}

    static_keys = catalog_keys()
    servers: list[McpServerEntry] = [
        McpServerEntry(key=key, kind="static", auth_status="static") for key in static_keys
    ]
    for key in oauth_catalog_keys():
        if not sandbox:
            status = "unavailable"
        else:
            row = statuses.get(key)
            if row is None:
                status = "not_connected"
            elif row == "active":
                status = "connected"
            else:  # expired | revoked
                status = "needs_auth"
        servers.append(McpServerEntry(key=key, kind="oauth2", auth_status=status))

    servers.sort(key=lambda s: s.key)
    return McpCatalogResponse(keys=static_keys, servers=servers)


@router.get("/catalog", response_model=McpCatalogResponse)
async def get_catalog(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> McpCatalogResponse:
    """List selectable MCP servers with the current user's per-key auth status."""
    return await _build_catalog(db, current_user)


@router.post("/{key}/auth/start", response_model=AuthStartResponse)
async def auth_start(
    key: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AuthStartResponse:
    """Begin the OAuth2 flow for an oauth catalog key: return the AS authorize URL."""
    server = get_oauth_server(key)
    if server is None:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth2 MCP server: {key}")
    if not get_settings().sandbox_enabled:
        raise HTTPException(status_code=409, detail="OAuth2 MCP requires sandbox mode")

    # Opportunistic sweep of expired pending states (the repo pattern; a background sweep
    # also runs in connector_cleanup). Keeps the table from growing on abandoned popups.
    cutoff = datetime.now(UTC) - timedelta(seconds=MCP_OAUTH_STATE_TTL_SECONDS)
    await db.execute(delete(McpOauthPendingState).where(McpOauthPendingState.created_at < cutoff))
    await db.commit()

    # Cap concurrent in-flight authorizations per user (each runs outbound discovery + a row
    # insert) so a client cannot drive unbounded egress / table growth.
    inflight = (
        await db.execute(
            select(func.count())
            .select_from(McpOauthPendingState)
            .where(McpOauthPendingState.user_id == current_user.id)
        )
    ).scalar_one()
    if inflight >= _MAX_PENDING_PER_USER:
        raise HTTPException(status_code=429, detail="Too many pending MCP authorizations")

    try:
        url = await mcp_oauth.build_authorize_url(
            db, server, user_id=current_user.id, redirect_uri=_redirect_uri()
        )
    except McpOauthError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return AuthStartResponse(authorize_url=url)


@router.get("/oauth/callback")
async def oauth_callback(
    state: str = Query(...),
    code: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """OAuth redirect target (unauthenticated). Identity comes from the consumed state row."""
    # Never reflect the AS-supplied `error` / raw exception text into the page — it is
    # attacker-controllable on this unauthenticated endpoint (reflected-XSS surface). Show a
    # fixed message; the detail goes to the logs only.
    if error or not code:
        logger.info("mcp_oauth_callback_error", extra={"reason": error or "no_code"})
        return _popup_html(
            "Authorization was cancelled or failed. You can close this window.", ok=False
        )
    try:
        row = await mcp_oauth.exchange_code(
            db, state=state, code=code, redirect_uri=_redirect_uri()
        )
    except (McpOauthError, GrantDeadError) as e:
        logger.warning("mcp_oauth_exchange_failed", extra={"reason": str(e)})
        return _popup_html("Connection failed. Please close this window and try again.", ok=False)
    await _audit(db, user_id=row.user_id, mcp_key=row.mcp_key, action="grant")
    logger.info("mcp_connected", extra={"mcp_key": row.mcp_key})
    return _popup_html(f"Connected to {row.mcp_key}. You can close this window.", ok=True)


@router.delete("/{key}/auth", response_model=McpCatalogResponse)
async def disconnect(
    key: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> McpCatalogResponse:
    """Revoke a user's grant for an oauth MCP server (best-effort upstream revoke)."""
    if get_oauth_server(key) is None:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth2 MCP server: {key}")
    # revoke_grant reads the DEK from the in-RAM store itself (best-effort upstream revoke).
    await mcp_oauth.revoke_grant(db, user_id=current_user.id, mcp_key=key)
    await _audit(db, user_id=current_user.id, mcp_key=key, action="revoke")
    return await _build_catalog(db, current_user)


def _popup_html(message: str, *, ok: bool) -> HTMLResponse:
    """Tiny page that closes the OAuth popup. The opener polls the catalog for the result."""
    color = "#16a34a" if ok else "#dc2626"
    body = (
        f"<!doctype html><html><head><meta charset='utf-8'><title>Inkwest MCP</title></head>"
        f"<body style='font-family:sans-serif;background:#111;color:{color};"
        f"display:flex;align-items:center;justify-content:center;height:100vh'>"
        f"<p>{html.escape(message)}</p>"
        f"<script>setTimeout(function(){{window.close()}},1200)</script>"
        f"</body></html>"
    )
    return HTMLResponse(content=body)
