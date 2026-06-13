"""Tests for the OAuth2 MCP reverse proxy route (app.api.routes.mcp_proxy).

Upstream is stubbed with httpx.MockTransport; the route's DB + token refresh are patched so
the focus is the proxy contract: JWT auth, header swap/passthrough, body-intact-on-retry,
and the 401 / 503 error split.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.services import mcp_proxy_token
from app.services.mcp_oauth import GrantDeadError, McpOauthError

_U = uuid.UUID("00000000-0000-4000-8000-000000000001")
_S = uuid.UUID("00000000-0000-4000-8000-000000000002")
SERVER_URL = "https://mcp.example.com/mcp"


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.close = AsyncMock()
    return db


def _streaming(status: int, body: bytes = b"") -> httpx.Response:
    """A genuinely streaming response — the route uses aiter_raw(), which a content=bytes
    MockTransport response would report as already-consumed."""

    async def gen():
        yield body

    return httpx.Response(status, content=gen())


def _bearer(key: str = "notion") -> dict[str, str]:
    return {"Authorization": f"Bearer {mcp_proxy_token.mint(_U, _S, key)}"}


async def _client() -> AsyncClient:
    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _patches(handler, *, fresh_token):
    """Patch the route's collaborators; set the upstream MockTransport handler."""
    from app.api.routes import mcp_proxy

    server = MagicMock()
    server.url = SERVER_URL
    mcp_proxy._transport = httpx.MockTransport(handler)
    return [
        patch("app.api.routes.mcp_proxy.get_oauth_server", return_value=server),
        patch("app.api.routes.mcp_proxy.async_session_maker", return_value=_fake_db()),
        # Resolve the fake host to a public IP so both the SSRF guard AND pin_url pass without
        # real DNS; the request is then pinned to that IP (MockTransport routes by path).
        patch("app.services.mcp_oauth._resolve_ips", lambda host: ["93.184.216.34"]),
        patch("app.api.routes.mcp_proxy.mcp_oauth.get_fresh_token", fresh_token),
        patch("app.api.routes.mcp_proxy.mcp_oauth.mark_expired", new=AsyncMock()),
    ]


@pytest.mark.asyncio
async def test_happy_path_swaps_auth_and_forwards_headers() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["proto"] = request.headers.get("mcp-protocol-version")
        seen["body"] = request.content
        return _streaming(200, b'{"ok":true}')

    fresh = AsyncMock(return_value="upstream-token")
    ps = _patches(handler, fresh_token=fresh)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                "/api/mcp-proxy/notion/",
                headers={**_bearer(), "MCP-Protocol-Version": "2025-06-18"},
                content=b'{"jsonrpc":"2.0"}',
            )
        assert resp.status_code == 200
        assert resp.content == b'{"ok":true}'
        assert seen["auth"] == "Bearer upstream-token"  # internal JWT swapped for real token
        assert seen["proto"] == "2025-06-18"  # MCP-Protocol-Version forwarded
        assert seen["body"] == b'{"jsonrpc":"2.0"}'
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_missing_or_bad_jwt_is_401() -> None:
    fresh = AsyncMock(return_value="upstream-token")
    ps = _patches(lambda r: httpx.Response(200), fresh_token=fresh)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            no_auth = await c.post("/api/mcp-proxy/notion/", content=b"{}")
            wrong_key = await c.post(
                "/api/mcp-proxy/notion/", headers=_bearer("slack"), content=b"{}"
            )
        assert no_auth.status_code == 401
        assert wrong_key.status_code == 401  # token minted for another server
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_body_intact_on_401_retry() -> None:
    calls = {"n": 0, "bodies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls["bodies"].append(request.content)
        return httpx.Response(401) if calls["n"] == 1 else _streaming(200, b"ok")

    # First load returns a token; the forced refresh returns a rotated token.
    fresh = AsyncMock(side_effect=["tok1", "tok2"])
    ps = _patches(handler, fresh_token=fresh)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post("/api/mcp-proxy/notion/", headers=_bearer(), content=b"BODY")
        assert resp.status_code == 200
        assert calls["n"] == 2  # retried once
        assert calls["bodies"] == [b"BODY", b"BODY"]  # body re-sent intact
        assert fresh.await_args_list[1].kwargs.get("force") is True  # forced on retry
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_double_401_marks_expired_and_returns_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)  # always rejects

    fresh = AsyncMock(side_effect=["tok1", "tok2"])
    ps = _patches(handler, fresh_token=fresh)
    mark = ps[-1]
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post("/api/mcp-proxy/notion/", headers=_bearer(), content=b"{}")
        assert resp.status_code == 401
        mark.new.assert_awaited()  # grant marked expired after the second 401
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_grant_dead_is_401() -> None:
    fresh = AsyncMock(side_effect=GrantDeadError("no active grant"))
    ps = _patches(lambda r: httpx.Response(200), fresh_token=fresh)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post("/api/mcp-proxy/notion/", headers=_bearer(), content=b"{}")
        assert resp.status_code == 401
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_broker_error_is_503() -> None:
    fresh = AsyncMock(side_effect=McpOauthError("AS backing off"))
    ps = _patches(lambda r: httpx.Response(200), fresh_token=fresh)
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post("/api/mcp-proxy/notion/", headers=_bearer(), content=b"{}")
        assert resp.status_code == 503  # transient broker degradation, not a dead grant
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_ssrf_private_upstream_is_502() -> None:
    """A catalog URL resolving to a private address is rejected before any fetch."""
    fresh = AsyncMock(return_value="t")
    ps = _patches(lambda r: _streaming(200), fresh_token=fresh)
    # Override the resolver so the SSRF guard sees a private IP.
    ps[2] = patch("app.services.mcp_oauth._resolve_ips", lambda host: ["10.0.0.5"])
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post("/api/mcp-proxy/notion/", headers=_bearer(), content=b"{}")
        assert resp.status_code == 502
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_forwards_sse_headers_and_strips_hop_by_hop() -> None:
    seen = {}

    async def _agen():
        yield b"data: x\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sid"] = request.headers.get("mcp-session-id")
        seen["last"] = request.headers.get("last-event-id")
        # Upstream sets a hop-by-hop header that must NOT reach the client.
        return httpx.Response(
            200,
            headers={"transfer-encoding": "chunked", "content-type": "text/event-stream"},
            content=_agen(),
        )

    ps = _patches(handler, fresh_token=AsyncMock(return_value="t"))
    for p in ps:
        p.start()
    try:
        async with await _client() as c:
            resp = await c.post(
                "/api/mcp-proxy/notion/",
                headers={**_bearer(), "Mcp-Session-Id": "sess-9", "Last-Event-ID": "42"},
                content=b"{}",
            )
        assert resp.status_code == 200
        assert seen["sid"] == "sess-9"  # Mcp-Session-Id forwarded
        assert seen["last"] == "42"  # Last-Event-ID forwarded (SSE resume)
        assert "transfer-encoding" not in {k.lower() for k in resp.headers}  # hop-by-hop stripped
        assert resp.headers.get("content-type") == "text/event-stream"
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_oversized_body_is_413() -> None:
    """A request body over the cap is rejected with 413 before any token work / upstream call,
    so a large/streamed payload can't OOM the single worker."""
    fresh = AsyncMock(return_value="t")
    ps = _patches(lambda r: _streaming(200), fresh_token=fresh)
    for p in ps:
        p.start()
    try:
        with patch("app.api.routes.mcp_proxy.MCP_PROXY_MAX_BODY_BYTES", 4):
            async with await _client() as c:
                resp = await c.post(
                    "/api/mcp-proxy/notion/", headers=_bearer(), content=b"WAY-TOO-LONG-BODY"
                )
        assert resp.status_code == 413
        fresh.assert_not_awaited()  # rejected before the token refresh / upstream fetch
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_unknown_key_is_404() -> None:
    from app.api.routes import mcp_proxy

    with patch("app.api.routes.mcp_proxy.get_oauth_server", return_value=None):
        async with await _client() as c:
            resp = await c.post("/api/mcp-proxy/ghost/", headers=_bearer("ghost"), content=b"{}")
    assert resp.status_code == 404
    mcp_proxy._transport = None
