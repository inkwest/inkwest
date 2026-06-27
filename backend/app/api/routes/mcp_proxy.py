"""
OAuth2 MCP reverse proxy (Increment 2).

One catch-all route fronts every oauth MCP server. The headless CLI (inside the sandbox,
egress only via squid) reaches `/api/mcp-proxy/{key}/…` carrying a short-lived internal JWT;
the proxy verifies it, loads + refreshes the user's real upstream token from the vault,
swaps the ``Authorization`` header, and streams the response back. Real tokens never touch
sandbox disk — only the internal JWT does.

Auth on this route IS the internal JWT (``app.services.mcp_proxy_token``); there is no
``get_current_user`` dependency — the AS-redirect / sandbox callers carry no session.

Error semantics the caller/panel must distinguish:
- grant dead (needs reconnect) → 401
- broker degraded (DB/vault/transient AS) → 503
"""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.api.routes._proxy_stream import (
    HOP_BY_HOP,
    TIMEOUT,
    close_stream,
    filter_headers,
    read_capped_body,
    split_bearer,
)
from app.core.constants import MCP_PROXY_MAX_BODY_BYTES
from app.core.database import async_session_maker
from app.services import mcp_oauth
from app.services.mcp_oauth import GrantDeadError, McpOauthError
from app.services.mcp_oauth_catalog import get_oauth_server
from app.services.mcp_proxy_token import ProxyAuthError, ProxyClaims, verify

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-proxy"])

# Injectable for tests (httpx.MockTransport). Timeout shared with the LLM proxy; see _proxy_stream.
_transport: httpx.AsyncBaseTransport | None = None

# Request headers we forward to the upstream MCP server (besides the swapped Authorization).
_FORWARD_REQ = {
    "accept",
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",  # required on post-initialize requests since the 2025-06-18 spec
    "last-event-id",  # SSE stream resumption
}


def _bearer(request: Request) -> str:
    token = split_bearer(request)
    if token is None:
        raise ProxyAuthError("missing bearer token")
    return token


def _request_headers(request: Request, upstream_token: str) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQ}
    headers["Authorization"] = f"Bearer {upstream_token}"
    return headers


def _upstream_url(base: str, path: str) -> str:
    """Join the catalog base URL with the opaque path segment (never the authority)."""
    base = base.rstrip("/")
    return f"{base}/{path}" if path else base


async def _forward(
    client: httpx.AsyncClient, *, method: str, url: str, headers: dict[str, str], body: bytes
) -> httpx.Response:
    # IP-pin the upstream connection (Host + SNI preserved) so the validated address is the
    # one we connect to — closes the DNS-rebinding TOCTOU between validation and fetch.
    pinned, host_header, extensions = mcp_oauth.pin_url(url)
    req = client.build_request(
        method,
        pinned,
        headers={**headers, "Host": host_header},
        content=body,
        extensions=extensions,
    )
    return await client.send(req, stream=True)


@router.api_route("/mcp-proxy/{key}/{path:path}", methods=["GET", "POST", "DELETE"])
async def mcp_proxy(key: str, path: str, request: Request) -> StreamingResponse:
    server = get_oauth_server(key)
    if server is None:
        raise HTTPException(status_code=404, detail="Unknown OAuth2 MCP server")
    try:
        claims: ProxyClaims = verify(_bearer(request), mcp_key=key)
    except ProxyAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    # SSRF defence-in-depth: the catalog URL is admin-set, but verify it before fetching.
    try:
        mcp_oauth.assert_public_https(server.url)
    except McpOauthError as e:
        raise HTTPException(status_code=502, detail="MCP server URL rejected") from e

    # The authority can't be crossed (pin_url pins to the catalog base host), but reject dot
    # segments so a crafted path can't traverse the upstream's own URL space.
    if any(seg in ("..", ".") for seg in path.split("/") if seg):
        raise HTTPException(status_code=400, detail="Invalid proxy path")

    body = await read_capped_body(request, MCP_PROXY_MAX_BODY_BYTES)  # buffer once for 401-retry
    url = _upstream_url(server.url, path)
    if request.url.query:
        url = f"{url}?{request.url.query}"

    db: AsyncSession = async_session_maker()
    client = httpx.AsyncClient(transport=_transport, timeout=TIMEOUT)
    upstream: httpx.Response | None = None
    log_extra = {"mcp_key": key, "session_id": str(claims.session_id)}

    # Single teardown contract: the `finally` always closes db (only needed for the token
    # work) and, when we did NOT hand a live stream to StreamingResponse (`upstream is None`),
    # also closes the client. On success the client + stream outlive this handler and are
    # closed by `close_stream` (shared, _proxy_stream) after the body is consumed (or disconnect).
    try:
        token = await mcp_oauth.get_fresh_token(db, user_id=claims.user_id, mcp_key=key)
        upstream = await _forward(
            client,
            method=request.method,
            url=url,
            headers=_request_headers(request, token),
            body=body,
        )
        if upstream.status_code == 401:
            # Skew or early revocation upstream — force a refresh and retry exactly once.
            await upstream.aclose()
            upstream = None
            token = await mcp_oauth.get_fresh_token(
                db, user_id=claims.user_id, mcp_key=key, force=True
            )
            upstream = await _forward(
                client,
                method=request.method,
                url=url,
                headers=_request_headers(request, token),
                body=body,
            )
            if upstream.status_code == 401:
                await upstream.aclose()
                upstream = None
                await mcp_oauth.mark_expired(db, user_id=claims.user_id, mcp_key=key)
                raise HTTPException(status_code=401, detail=_EXPIRED)
    except GrantDeadError:
        raise HTTPException(status_code=401, detail=_EXPIRED) from None
    except McpOauthError:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from None
    except HTTPException:
        raise
    except Exception:
        logger.exception("mcp_proxy_unexpected", extra=log_extra)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from None
    finally:
        await db.close()  # token work is done; never pin a DB conn for the stream lifetime
        if upstream is None:
            await client.aclose()

    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=filter_headers(upstream.headers, HOP_BY_HOP),
        background=BackgroundTask(close_stream, upstream, client),
    )


_EXPIRED = "MCP authorization expired — reconnect in the panel"
_UNAVAILABLE = "MCP broker temporarily unavailable"
