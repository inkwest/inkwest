"""Tests for the OAuth2 MCP broker service (app.services.mcp_oauth).

HTTP is stubbed with ``httpx.MockTransport`` (no new dependency); the SSRF resolver is
overridden so fake public hostnames pass the private-range guard without real DNS. The
service's DB access is exercised against an ``AsyncMock(spec=AsyncSession)`` — the
Postgres-specific ON CONFLICT / DCR race is left to the optional e2e (real-Postgres) tier.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.crypto import generate_dek, seal
from app.models.mcp_oauth_client import McpOauthClient
from app.models.mcp_user_token import McpUserToken
from app.services import mcp_oauth
from app.services.mcp_oauth import (
    Discovery,
    GrantDeadError,
    McpOauthError,
    discover,
)
from app.services.mcp_oauth_catalog import OAuthMcpServer

_USER = uuid.UUID("00000000-0000-4000-8000-000000000001")

SERVER_URL = "https://mcp.example.com/mcp"
PRM_URL = "https://mcp.example.com/.well-known/oauth-protected-resource"
AS_META_URL = "https://as.example.com/.well-known/oauth-authorization-server"


def _server(client_id: str | None = "cid-123") -> OAuthMcpServer:
    return OAuthMcpServer(
        "notion",
        {"type": "http", "url": SERVER_URL, "scopes": ["mcp:read"], "client_id": client_id},
    )


@pytest.fixture(autouse=True)
def _public_resolver():
    """Make every hostname resolve to a public IP so the SSRF guard passes in tests."""
    original = mcp_oauth._resolve_ips
    mcp_oauth._resolve_ips = lambda host: ["93.184.216.34"]
    yield
    mcp_oauth._resolve_ips = original
    mcp_oauth._transport = None


def _use_transport(handler) -> None:
    mcp_oauth._transport = httpx.MockTransport(handler)


# --------------------------------------------------------------------------- #
# SSRF guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",  # non-https
        "https://127.0.0.1/mcp",  # loopback
        "https://10.0.0.1/mcp",  # RFC 1918
        "https://169.254.169.254/latest",  # link-local (cloud metadata)
        "https://[::1]/mcp",  # IPv6 loopback
        "https://0.0.0.0/mcp",  # unspecified
        "https://[fc00::1]/mcp",  # IPv6 ULA (private)
    ],
)
def test_ssrf_guard_rejects(url: str) -> None:
    with pytest.raises(McpOauthError):
        mcp_oauth._assert_public_https(url)


def test_ssrf_guard_allows_public_https() -> None:
    mcp_oauth._assert_public_https("https://mcp.example.com/mcp")  # no raise


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_discovery_via_401_challenge() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/mcp":
            return httpx.Response(
                401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{PRM_URL}"'}
            )
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://as.example.com",
                    "authorization_endpoint": "https://as.example.com/authorize",
                    "token_endpoint": "https://as.example.com/token",
                    "registration_endpoint": "https://as.example.com/register",
                },
            )
        return httpx.Response(404)

    _use_transport(handler)
    disco = await discover(SERVER_URL)
    assert disco.token_endpoint == "https://as.example.com/token"
    assert disco.authorization_endpoint == "https://as.example.com/authorize"
    assert disco.registration_endpoint == "https://as.example.com/register"


@pytest.mark.asyncio
async def test_discovery_well_known_fallback_without_401() -> None:
    """Server doesn't challenge: probe the resource origin's well-known paths directly."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/mcp":
            return httpx.Response(200, json={"ok": True})  # no 401 challenge
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(404)  # no PRM → issuer falls back to the origin
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://mcp.example.com/authorize",
                    "token_endpoint": "https://mcp.example.com/token",
                },
            )
        return httpx.Response(404)

    _use_transport(handler)
    disco = await discover(SERVER_URL)
    assert disco.token_endpoint == "https://mcp.example.com/token"


@pytest.mark.asyncio
async def test_discovery_rejects_cross_origin_resource_metadata() -> None:
    """RFC 9728 binds the PRM doc to the resource origin — a cross-origin pointer is an attack."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://evil.example.net/.well-known/oauth-protected-resource"'
                },
            )
        return httpx.Response(200, json={})

    _use_transport(handler)
    with pytest.raises(McpOauthError, match="cross-origin"):
        await discover(SERVER_URL)


# --------------------------------------------------------------------------- #
# Token exchange + refresh (DB mocked)
# --------------------------------------------------------------------------- #
def _mock_db_for_exchange(client: McpOauthClient, existing_token: McpUserToken | None = None):
    """An AsyncMock session whose state-consume returns a fresh pending row."""
    db = AsyncMock()
    pending = MagicMock()
    pending.code_verifier = "verifier"
    pending.mcp_key = "notion"
    pending.user_id = _USER
    pending.created_at = datetime.now(UTC)
    result = MagicMock()
    result.first.return_value = pending
    db.execute = AsyncMock(return_value=result)

    async def _get(model, pk):
        if model is McpOauthClient:
            return client
        if model is McpUserToken:
            return existing_token
        return None

    db.get = AsyncMock(side_effect=_get)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_exchange_code_stores_sealed_token(monkeypatch) -> None:
    server = _server()
    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: server)
    client = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id="cid-123",
    )
    db = _mock_db_for_exchange(client)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token"
        return httpx.Response(
            200, json={"access_token": "AT-xyz", "refresh_token": "RT-abc", "expires_in": 3600}
        )

    _use_transport(handler)
    dek = generate_dek()
    row = await mcp_oauth.exchange_code(
        db, state="s", code="c", redirect_uri="https://app/cb", dek=dek
    )
    assert row.status == "active"
    assert row.blob is not None and row.blob_iv is not None
    # Token material is sealed, not plaintext.
    assert b"AT-xyz" not in row.blob
    from app.core.crypto import unseal

    assert json.loads(unseal(dek, row.blob, row.blob_iv))["access_token"] == "AT-xyz"


@pytest.mark.asyncio
async def test_exchange_code_unknown_state_raises() -> None:
    db = AsyncMock()
    result = MagicMock()
    result.first.return_value = None
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    with pytest.raises(McpOauthError, match="state"):
        await mcp_oauth.exchange_code(
            db, state="bad", code="c", redirect_uri="x", dek=generate_dek()
        )


@pytest.mark.asyncio
async def test_get_fresh_token_null_expiry_returns_without_network(monkeypatch) -> None:
    dek = generate_dek()
    blob, iv = seal(dek, json.dumps({"access_token": "AT-live"}).encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv, token.expires_at = blob, iv, None
    db = AsyncMock()
    db.get = AsyncMock(return_value=token)
    # No transport set → any HTTP call would raise; proves none happens.
    out = await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek)
    assert out == "AT-live"


@pytest.mark.asyncio
async def test_get_fresh_token_invalid_grant_marks_expired(monkeypatch) -> None:
    server = _server()
    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: server)
    dek = generate_dek()
    blob, iv = seal(dek, json.dumps({"access_token": "old", "refresh_token": "RT"}).encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv = blob, iv
    token.expires_at = datetime.now(UTC) - timedelta(minutes=5)  # expired → refresh path
    client = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id="cid-123",
    )

    async def _get(model, pk):
        return token if model is McpUserToken else client

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    _use_transport(handler)
    with pytest.raises(GrantDeadError):
        await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek)
    assert token.status == "expired"


@pytest.mark.asyncio
async def test_discovery_result_dataclass_frozen() -> None:
    d = Discovery("i", "a", "t", None, None)
    with pytest.raises(Exception):
        d.token_endpoint = "x"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_discovery_rejects_issuer_mismatch() -> None:
    """RFC 8414 §3.3: metadata whose `issuer` differs from the fetched issuer is rejected."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/mcp":
            return httpx.Response(
                401, headers={"WWW-Authenticate": f'Bearer resource_metadata="{PRM_URL}"'}
            )
        if path == "/.well-known/oauth-protected-resource":
            return httpx.Response(200, json={"authorization_servers": ["https://as.example.com"]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://evil.example.net",  # != requested issuer
                    "authorization_endpoint": "https://as.example.com/authorize",
                    "token_endpoint": "https://as.example.com/token",
                },
            )
        return httpx.Response(404)

    _use_transport(handler)
    with pytest.raises(McpOauthError, match="issuer mismatch"):
        await discover(SERVER_URL)


# --------------------------------------------------------------------------- #
# WWW-Authenticate parsing (regression for the "Bearer " prefix bug)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "header,expected",
    [
        ('Bearer resource_metadata="https://x/y"', "https://x/y"),
        ('Bearer error="invalid_token", resource_metadata="https://x/y"', "https://x/y"),
        ("Bearer resource_metadata=https://x/y", "https://x/y"),  # unquoted
        ('resource_metadata="https://x/y"', "https://x/y"),  # no scheme token
        ("Bearer realm=foo", None),  # absent
        ("", None),
    ],
)
def test_parse_resource_metadata_url(header: str, expected: str | None) -> None:
    assert mcp_oauth._parse_resource_metadata_url(header) == expected


# --------------------------------------------------------------------------- #
# DCR (client_id absent → Dynamic Client Registration)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ensure_client_runs_dcr_when_no_client_id() -> None:
    server = _server(client_id=None)  # forces DCR
    disco = Discovery(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint="https://as.example.com/register",
        revocation_endpoint=None,
    )
    posted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/register"
        posted.update(json.loads(request.content))
        return httpx.Response(201, json={"client_id": "dcr-cid", "client_secret_expires_at": 0})

    _use_transport(handler)

    persisted = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id="dcr-cid",
    )
    gets = [None, None, persisted]  # existing-check, under-lock re-check, persisted re-read

    async def _get(model, pk):
        return gets.pop(0) if gets else persisted

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    row = await mcp_oauth.ensure_client(db, server, redirect_uri="https://app/cb", disco=disco)
    assert row.client_id == "dcr-cid"
    # public client + PKCE, our redirect registered
    assert posted["token_endpoint_auth_method"] == "none"
    assert posted["redirect_uris"] == ["https://app/cb"]


# --------------------------------------------------------------------------- #
# get_fresh_token refresh-success + DEK-missing
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_fresh_token_refreshes_and_reseals(monkeypatch) -> None:
    server = _server()
    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: server)
    dek = generate_dek()
    blob, iv = seal(dek, json.dumps({"access_token": "old", "refresh_token": "RT0"}).encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv = blob, iv
    token.expires_at = datetime.now(UTC) - timedelta(minutes=1)  # expired → refresh
    client = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id="cid-123",
    )

    async def _get(model, pk):
        return token if model is McpUserToken else client

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock()
    db.add = MagicMock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "new-AT", "refresh_token": "RT1"})

    _use_transport(handler)
    out = await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek)
    assert out == "new-AT"
    # re-sealed under the same DEK with the rotated material
    from app.core.crypto import unseal

    assert json.loads(unseal(dek, token.blob, token.blob_iv))["access_token"] == "new-AT"


@pytest.mark.asyncio
async def test_get_fresh_token_no_dek_raises() -> None:
    from app.services.mcp_dek_store import get_dek_store

    await get_dek_store().delete(_USER)  # ensure absent
    db = AsyncMock()
    with pytest.raises(GrantDeadError):
        await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion")


@pytest.mark.asyncio
async def test_exchange_code_no_dek_raises(monkeypatch) -> None:
    from app.services.mcp_dek_store import get_dek_store

    await get_dek_store().delete(_USER)
    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: _server())
    client = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id="cid-123",
    )
    db = _mock_db_for_exchange(client)
    with pytest.raises(McpOauthError, match="encryption key"):
        await mcp_oauth.exchange_code(db, state="s", code="c", redirect_uri="x")  # dek=None


@pytest.mark.asyncio
async def test_exchange_code_state_expired() -> None:
    db = AsyncMock()
    pending = MagicMock()
    pending.code_verifier = "v"
    pending.mcp_key = "notion"
    pending.user_id = _USER
    pending.created_at = datetime.now(UTC) - timedelta(hours=1)  # stale
    db.execute = AsyncMock(return_value=MagicMock(first=lambda: pending))
    db.commit = AsyncMock()
    with pytest.raises(McpOauthError, match="expired"):
        await mcp_oauth.exchange_code(db, state="s", code="c", redirect_uri="x", dek=generate_dek())


# --------------------------------------------------------------------------- #
# revoke_grant
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_revoke_grant_nulls_blob_and_swallows_upstream_error() -> None:
    dek = generate_dek()
    blob, iv = seal(dek, json.dumps({"access_token": "AT"}).encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv = blob, iv
    client = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        revocation_endpoint="https://as.example.com/revoke",
        client_id="cid-123",
    )

    async def _get(model, pk):
        return token if model is McpUserToken else client

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)  # upstream revoke fails — must be swallowed

    _use_transport(handler)
    await mcp_oauth.revoke_grant(db, user_id=_USER, mcp_key="notion", dek=dek)
    assert token.status == "revoked"
    assert token.blob is None and token.blob_iv is None
