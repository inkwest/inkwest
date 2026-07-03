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
from typing import Any, Literal

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
from app.services import mcp_ask_store, mcp_oauth, mcp_tools
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


class McpToolInfo(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    # MCP behavior hints ({read_only, destructive, idempotent, open_world, title}) or None.
    # Drives the UI's capability grouping. Absent on older cached rows until a refresh.
    annotations: dict[str, Any] | None = None


class McpServerToolsResponse(BaseModel):
    key: str
    tools: list[McpToolInfo]
    # None until the first successful enumeration.
    fetched_at: datetime | None = None


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


@router.get("/{key}/tools", response_model=McpServerToolsResponse)
async def get_server_tools(
    key: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    refresh: bool = Query(False),
) -> McpServerToolsResponse:
    """The tools a server exposes, for the per-tool permission UI.

    Served from the ``mcp_server_tools`` cache; enumerated live (MCP initialize + tools/list
    with proxy-injected creds) on first open or when ``refresh=true``. A dead grant /
    unreachable server falls back to any stale cache, else 502 — so a transient failure can't
    block the rest of the config UI.
    """
    if key not in (set(catalog_keys()) | set(oauth_catalog_keys())):
        raise HTTPException(status_code=404, detail="Unknown MCP server")

    cached = await mcp_tools.get_cached_tools(db, key)
    if cached is not None and not refresh:
        return McpServerToolsResponse(
            key=key,
            tools=[McpToolInfo(**t) for t in cached.tools],
            fetched_at=cached.updated_at,
        )
    try:
        tools = await mcp_tools.refresh_tools(db, key=key, user_id=current_user.id)
    except mcp_tools.McpEnumError as e:
        if cached is not None:  # serve stale rather than fail the whole panel
            return McpServerToolsResponse(
                key=key,
                tools=[McpToolInfo(**t) for t in cached.tools],
                fetched_at=cached.updated_at,
            )
        # No cache and enumeration failed (server unreachable / not connected / cold). Raise 500
        # — a genuine application error the UI surfaces as such. Deliberately NOT 502/503/504:
        # those are the gateway/backend-unavailable class the client retries (~12x) and shows as
        # "backend reconnecting", which would both flood an unreachable server and mislead.
        raise HTTPException(
            status_code=500,
            detail="Couldn't list this server's tools (it may be unreachable or not connected).",
        ) from e
    row = await mcp_tools.get_cached_tools(db, key)
    return McpServerToolsResponse(
        key=key,
        tools=[McpToolInfo(**t) for t in tools],
        fetched_at=row.updated_at if row else None,
    )


class McpAskDecision(BaseModel):
    verdict: Literal["allow", "deny"]


class McpPendingAsk(BaseModel):
    ask_id: str
    mcp_key: str
    tool: str


@router.get("/ask/pending", response_model=list[McpPendingAsk])
async def pending_asks(
    current_user: CurrentUser,
    session_id: uuid.UUID = Query(...),
) -> list[McpPendingAsk]:
    """Live tool-permission asks held for this user on one conversation.

    Rehydrates the inline ask banners after a page reload — the WS ``mcp_tool_ask`` frame
    is fire-and-forget, so a tab that (re)connects mid-hold would otherwise never see it.
    """
    return [
        McpPendingAsk(ask_id=a.ask_id, mcp_key=a.server_key, tool=a.tool)
        for a in mcp_ask_store.pending_for(user_id=str(current_user.id), session_id=str(session_id))
    ]


@router.post("/ask/{ask_id}")
async def decide_ask(
    ask_id: str,
    decision: McpAskDecision,
    current_user: CurrentUser,
) -> dict[str, str]:
    """Resolve a held tool-permission ask (the proxy is parked on its Future).

    404 covers unknown, expired (the proxy's timeout already denied it), already-decided,
    AND another user's ask — indistinguishable on purpose, so ask_ids can't be probed.
    """
    ok = mcp_ask_store.resolve(ask_id, decision.verdict, user_id=str(current_user.id))
    if not ok:
        raise HTTPException(status_code=404, detail="Ask not found or already resolved")
    return {"status": "ok"}


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
        # 424 Failed Dependency: this is deterministic — the upstream authorization server this
        # connector depends on couldn't be discovered/registered (bad URL, missing metadata) — not
        # a transient gateway blip (502) nor a server fault (500). Choosing a 4xx keeps a connector
        # misconfiguration OUT of the 5xx crash bucket (no false pages/SLO burn, indistinguishable
        # from a real outage) and OFF the SPA's retry path (fetchWithRetry only retries 502/503/504
        # and shows the global "backend unavailable" banner meanwhile). So the real reason surfaces
        # immediately instead of after ~12 retries behind a wrong banner.
        raise HTTPException(status_code=424, detail=str(e)) from e
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
