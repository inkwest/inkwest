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

from app.core.constants import MCP_PROXY_MAX_BODY_BYTES
from app.core.database import async_session_maker
from app.services import mcp_oauth
from app.services.mcp_oauth import GrantDeadError, McpOauthError
from app.services.mcp_oauth_catalog import get_oauth_server
from app.services.mcp_proxy_token import ProxyAuthError, ProxyClaims, verify

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp-proxy"])

# Injectable for tests (httpx.MockTransport). Connect/write bounded; read unbounded so a
# long-lived SSE stream is not severed, but the initial connect can't hang forever.
_transport: httpx.AsyncBaseTransport | None = None
_TIMEOUT = httpx.Timeout(connect=5.0, write=30.0, read=None, pool=5.0)

# Hop-by-hop headers (RFC 7230 §6.1) plus framing headers httpx/Starlette must own.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
# Request headers we forward to the upstream MCP server (besides the swapped Authorization).
_FORWARD_REQ = {
    "accept",
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",  # required on post-initialize requests since the 2025-06-18 spec
    "last-event-id",  # SSE stream resumption
}


async def _read_capped_body(request: Request) -> bytes:
    """Buffer the request body, rejecting bodies over the cap with 413.

    The body is held in RAM so it can be replayed on the 401-refresh retry; without a cap a
    large or chunked tool-call payload (or many concurrent ones) is an unbounded-memory blast
    radius on the single worker. Reject on the declared Content-Length when present, and guard
    again while streaming for chunked requests that omit it.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MCP_PROXY_MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MCP_PROXY_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise ProxyAuthError("missing bearer token")
    return header[7:]


def _request_headers(request: Request, upstream_token: str) -> dict[str, str]:
    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQ}
    headers["Authorization"] = f"Bearer {upstream_token}"
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP}


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

    body = await _read_capped_body(request)  # buffer once — needed again on the 401-retry
    url = _upstream_url(server.url, path)
    if request.url.query:
        url = f"{url}?{request.url.query}"

    db: AsyncSession = async_session_maker()
    client = httpx.AsyncClient(transport=_transport, timeout=_TIMEOUT)
    upstream: httpx.Response | None = None
    log_extra = {"mcp_key": key, "session_id": str(claims.session_id)}

    # Single teardown contract: the `finally` always closes db (only needed for the token
    # work) and, when we did NOT hand a live stream to StreamingResponse (`upstream is None`),
    # also closes the client. On success the client + stream outlive this handler and are
    # closed by `_close_stream` after the body is consumed (or the client disconnects).
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
        headers=_response_headers(upstream),
        background=BackgroundTask(_close_stream, upstream, client),
    )


_EXPIRED = "MCP authorization expired — reconnect in the panel"
_UNAVAILABLE = "MCP broker temporarily unavailable"


async def _close_stream(upstream: httpx.Response, client: httpx.AsyncClient) -> None:
    """Release the upstream stream + its client once the response body is done (or the client
    disconnects mid-stream — Starlette runs the background task either way)."""
    await upstream.aclose()
    await client.aclose()
