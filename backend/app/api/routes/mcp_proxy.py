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

import contextlib
import json
import logging
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
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
from app.services import mcp_oauth, mcp_policy
from app.services.mcp_catalog import get_static_http_server
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


def _request_headers(request: Request, inject: dict[str, str]) -> dict[str, str]:
    # Forward only the allowlisted client headers, then layer the upstream credential on top.
    # oauth -> a single ``Authorization: Bearer <real-token>``; static catalog -> the def's real
    # headers (may be ``Authorization`` OR a custom header like ``X-API-Key``). The inbound
    # internal JWT rides ``Authorization``, which is NOT in _FORWARD_REQ, so it is dropped before
    # ``inject`` wins — the sandbox's token never reaches the upstream.
    headers = {k: v for k, v in request.headers.items() if k.lower() in _FORWARD_REQ}
    headers.update(inject)
    return headers


def _upstream_url(base: str, path: str) -> str:
    """Join the catalog base URL with the opaque path segment (never the authority)."""
    base = base.rstrip("/")
    return f"{base}/{path}" if path else base


# JSON-RPC application error for a policy-denied tool call (transported in the body, not HTTP).
_TOOL_DENIED_CODE = -32001
_TOOL_DENIED_MSG = "Tool call denied by permission policy"


def _one_tool_call_name(item: object) -> str | None:
    """The tool name if ``item`` is a single JSON-RPC ``tools/call`` object, else None."""
    if not isinstance(item, dict) or item.get("method") != "tools/call":
        return None
    params = item.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return name if isinstance(name, str) else None


def _tool_call_names(body: bytes) -> list[str]:
    """All ``tools/call`` tool names in ``body`` — handles a single object AND a JSON-RPC batch
    (array); empty for any other request. Batches are covered so a batched call can't bypass the
    gate.

    The proxy sits between the sandbox MCP client and the upstream, so names here are the
    server's RAW tool names (not the ``mcp__server__tool`` advertised form) — matching how tools
    are enumerated and how policy rows are keyed.
    """
    if not body:
        return []
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return []
    items = payload if isinstance(payload, list) else [payload]
    return [n for item in items if (n := _one_tool_call_name(item)) is not None]


async def _resolve_tool_verdict(
    key: str, tool: str, *, user_id: uuid.UUID, session_id: uuid.UUID | None
) -> str:
    """Resolve the tool's verdict in a short-lived DB session.

    Fail-OPEN (allow) on any resolution error: the credential broker is the hard security
    boundary; a policy-table blip must not break every tool call. Denials come only from
    explicit policy rows.
    """
    db = async_session_maker()
    try:
        return await mcp_policy.resolve_verdict(
            db, user_id=user_id, session_id=session_id, server_key=key, tool=tool
        )
    except Exception:
        # error, not warning: a policy-table outage silently allows every tool here.
        logger.error("mcp_policy_resolve_failed", extra={"mcp_key": key, "tool": tool})
        return "allow"
    finally:
        await db.close()


def _jsonrpc_error_response(body: bytes, code: int, message: str) -> JSONResponse:
    """A JSON-RPC error echoing the request id, so the model sees a clean tool failure."""
    req_id = None
    with contextlib.suppress(ValueError, TypeError):
        payload = json.loads(body)
        if isinstance(payload, dict):
            req_id = payload.get("id")
    return JSONResponse(
        content={"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
    )


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
async def mcp_proxy(key: str, path: str, request: Request) -> Response:
    # Resolve the key: an OAuth2 server (per-user vault token, refreshed) OR a static catalog
    # HTTP server (shared server-side headers from MCP_CATALOG). Either way the sandbox holds
    # only the internal JWT; the real credential is injected here. stdio catalog servers carry
    # no URL (get_static_http_server returns None), so they are never brokered through this route.
    server = get_oauth_server(key)
    static_def = get_static_http_server(key) if server is None else None
    if server is None and static_def is None:
        raise HTTPException(status_code=404, detail="Unknown MCP server")
    try:
        claims: ProxyClaims = verify(_bearer(request), mcp_key=key)
    except ProxyAuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    if server is not None:
        upstream_base = server.url
    else:
        assert static_def is not None  # narrowed by the 404 guard above
        upstream_base = static_def["url"]
    # SSRF defence-in-depth: the catalog URL is admin-set, but verify it before fetching.
    try:
        mcp_oauth.assert_public_https(upstream_base)
    except McpOauthError as e:
        raise HTTPException(status_code=502, detail="MCP server URL rejected") from e

    # The authority can't be crossed (pin_url pins to the catalog base host), but reject dot
    # segments so a crafted path can't traverse the upstream's own URL space.
    if any(seg in ("..", ".") for seg in path.split("/") if seg):
        raise HTTPException(status_code=400, detail="Invalid proxy path")

    body = await read_capped_body(request, MCP_PROXY_MAX_BODY_BYTES)  # buffer once for 401-retry
    url = _upstream_url(upstream_base, path)
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # Per-tool permission gate (engine-agnostic — every engine's MCP traffic funnels here). A
    # `tools/call` (single or batched) names the tool(s); resolve each verdict and deny the
    # request before it reaches the upstream if ANY tool is denied. 'allow' and 'ask' fall
    # through; the runtime 'ask' hold is layered on in P6.
    for tool in _tool_call_names(body):
        verdict = await _resolve_tool_verdict(
            key, tool, user_id=claims.user_id, session_id=claims.session_id
        )
        if verdict == "deny":
            logger.info("mcp_tool_denied", extra={"mcp_key": key, "tool": tool})
            return _jsonrpc_error_response(body, _TOOL_DENIED_CODE, _TOOL_DENIED_MSG)

    # Static catalog HTTP server: inject the def's real headers (the shared credential the
    # sandbox never sees) and stream — no vault, no token refresh, no DB session.
    if static_def is not None:
        return await _proxy_static(
            request,
            url=url,
            headers=static_def.get("headers") or {},
            body=body,
            key=key,
            session_id=claims.session_id,
        )

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
            headers=_request_headers(request, {"Authorization": f"Bearer {token}"}),
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
                headers=_request_headers(request, {"Authorization": f"Bearer {token}"}),
                body=body,
            )
            if upstream.status_code == 401:
                await upstream.aclose()
                upstream = None
                await mcp_oauth.mark_expired(db, user_id=claims.user_id, mcp_key=key)
                raise HTTPException(status_code=401, detail=_EXPIRED)
    except GrantDeadError:
        # The grant is dead (refresh/exchange rejected, undecryptable blob, or a dropped DCR
        # client). Downgrade the local row so the next catalog poll flips the "connected" badge
        # to "Reconnect" — otherwise the optimistic active row keeps the UI showing connected
        # even though every call fails. Idempotent + best-effort (don't mask the 401).
        with contextlib.suppress(Exception):
            await mcp_oauth.mark_expired(db, user_id=claims.user_id, mcp_key=key)
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


async def _proxy_static(
    request: Request,
    *,
    url: str,
    headers: dict[str, str],
    body: bytes,
    key: str,
    session_id: object,
) -> StreamingResponse:
    """Forward to a static catalog HTTP server, injecting its real headers server-side.

    Simpler than the oauth path: the credential is a shared, static catalog secret, so there is
    no vault lookup, no token refresh, and no 401-retry — and no DB session to manage. Same
    single-teardown contract: on success the client + stream outlive this handler (closed by the
    BackgroundTask); on a pre-handoff failure, close the client here."""
    client = httpx.AsyncClient(transport=_transport, timeout=TIMEOUT)
    upstream: httpx.Response | None = None
    try:
        upstream = await _forward(
            client,
            method=request.method,
            url=url,
            headers=_request_headers(request, headers),
            body=body,
        )
    except httpx.HTTPError:
        logger.exception(
            "mcp_proxy_static_upstream_error",
            extra={"mcp_key": key, "session_id": str(session_id)},
        )
        raise HTTPException(status_code=502, detail="MCP upstream error") from None
    finally:
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
