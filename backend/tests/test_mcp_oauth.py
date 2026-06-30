"""Tests for the OAuth2 MCP broker service (app.services.mcp_oauth).

HTTP is stubbed with ``httpx.MockTransport`` (no new dependency); the SSRF resolver is
overridden so fake public hostnames pass the private-range guard without real DNS. The
service's DB access is exercised against an ``AsyncMock(spec=AsyncSession)`` — the
Postgres-specific ON CONFLICT / DCR race is left to the optional e2e (real-Postgres) tier.
"""

import gzip
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
    ClientDeadError,
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
        mcp_oauth.assert_public_https(url)


def test_ssrf_guard_allows_public_https() -> None:
    mcp_oauth.assert_public_https("https://mcp.example.com/mcp")  # no raise


# --------------------------------------------------------------------------- #
# _http response rebuild
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_http_decodes_gzip_response_then_json() -> None:
    """A gzip-Content-Encoded response must decode cleanly to JSON.

    Regression: `_http` read the already-decoded body via `aiter_bytes()` but rebuilt the
    Response with the original headers (incl. `Content-Encoding: gzip`), so `.json()` tried
    to gunzip the plaintext again → httpx.DecodingError. Real symptom: an MCP server's gzipped
    /token endpoint failing with "POST .../token failed: DecodingError".
    """
    body = gzip.compress(json.dumps({"access_token": "abc", "token_type": "Bearer"}).encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    _use_transport(handler)
    resp = await mcp_oauth._http("POST", "https://as.example.com/token", data={"grant_type": "x"})
    assert resp.json() == {"access_token": "abc", "token_type": "Bearer"}


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
async def test_get_fresh_token_no_dek_is_transient_not_dead() -> None:
    """A missing DEK (browser WS dropped) is a transient broker condition → McpOauthError
    (proxy maps to 503 "try again"), NOT GrantDeadError (401 "reconnect a valid grant")."""
    from app.services.mcp_dek_store import get_dek_store

    await get_dek_store().delete(_USER)  # ensure absent
    db = AsyncMock()
    with pytest.raises(McpOauthError, match="encryption key"):
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
async def test_refresh_single_flight_rotates_once_under_concurrency(monkeypatch) -> None:
    """Two concurrent refreshes on the same (user,key) → exactly ONE token-endpoint rotation
    (single-flight + post-lock reuse). OAuth 2.1 ASes rotate refresh tokens on use, so a
    double rotation would brick the grant."""
    import asyncio

    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: _server())
    mcp_oauth._refresh_failed_at.clear()
    dek = generate_dek()
    blob, iv = seal(dek, json.dumps({"access_token": "old", "refresh_token": "RT"}).encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv = blob, iv
    token.expires_at = datetime.now(UTC) - timedelta(minutes=1)  # expired → both want refresh
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
    db.refresh = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": "new-AT", "refresh_token": "RT2"})

    _use_transport(handler)
    results = await asyncio.gather(
        mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek),
        mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek),
    )
    assert calls["n"] == 1  # only one rotation despite two concurrent callers
    assert results == ["new-AT", "new-AT"]


@pytest.mark.asyncio
async def test_refresh_negative_cache_fails_fast(monkeypatch) -> None:
    """A recent AS failure short-circuits before the lock/network (no retry storm)."""
    import time

    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: _server())
    dek = generate_dek()
    blob, iv = seal(dek, json.dumps({"access_token": "old", "refresh_token": "RT"}).encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv = blob, iv
    token.expires_at = datetime.now(UTC) - timedelta(minutes=1)  # expiring → would refresh
    db = AsyncMock()
    db.get = AsyncMock(return_value=token)
    # Seed the negative cache as if a refresh just failed.
    mcp_oauth._refresh_failed_at[(str(_USER), "notion")] = time.monotonic()
    try:
        # No transport set: if it reached the token endpoint it would fail differently.
        with pytest.raises(McpOauthError, match="backing off"):
            await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek)
    finally:
        mcp_oauth._refresh_failed_at.clear()


@pytest.mark.asyncio
async def test_refresh_reuses_recently_rotated_token(monkeypatch) -> None:
    """force=True but a competitor rotated <30s ago → reuse it, don't rotate again."""
    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: _server())
    dek = generate_dek()
    blob, iv = seal(dek, json.dumps({"access_token": "fresh-AT", "refresh_token": "RT"}).encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv = blob, iv
    token.expires_at = datetime.now(UTC) + timedelta(hours=1)  # not expiring
    token.updated_at = datetime.now(UTC)  # just refreshed
    db = AsyncMock()
    db.get = AsyncMock(return_value=token)
    db.refresh = AsyncMock()
    mcp_oauth._refresh_failed_at.clear()
    # No transport set: proves no token-endpoint call happens.
    out = await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek, force=True)
    assert out == "fresh-AT"


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


# --------------------------------------------------------------------------- #
# SSRF allow-list (is_global) — special-purpose ranges a deny-list would admit
# --------------------------------------------------------------------------- #
def test_ssrf_guard_rejects_cgnat() -> None:
    """RFC 6598 CGNAT 100.64.0.0/10 is not globally routable — an allow-list rejects it
    where the old private/loopback/link-local deny-list would have let it through."""
    mcp_oauth._resolve_ips = lambda host: ["100.64.1.1"]
    with pytest.raises(McpOauthError, match="non-public"):
        mcp_oauth.assert_public_https("https://sneaky.example.com/mcp")


def _allow_settings():
    return MagicMock(mcp_oauth_allow_hosts="internal-mcp.example.com")


def test_ssrf_allowlist_permits_named_private_host(monkeypatch) -> None:
    """MCP_OAUTH_ALLOW_HOSTS waives the public-IP requirement for exact-named hosts
    (internal/VPN MCP behind private DNS) — https + IP-pin still apply, and the waiver is
    keyed on the hostname, so a different private host is still rejected."""
    monkeypatch.setattr(mcp_oauth, "_resolve_ips", lambda host: ["10.1.0.91"])
    monkeypatch.setattr(mcp_oauth, "get_settings", _allow_settings)
    mcp_oauth._warned_allow_hosts.clear()
    # Allowlisted host on a private IP: permitted, and the IP is still returned for pinning.
    assert mcp_oauth._validated_ips("https://internal-mcp.example.com/mcp") == ["10.1.0.91"]
    # A non-listed host on the same range is still rejected (exact-match, not a CIDR opener).
    with pytest.raises(McpOauthError, match="non-public"):
        mcp_oauth.assert_public_https("https://other-internal.example.com/mcp")


def test_ssrf_allowlist_still_requires_https(monkeypatch) -> None:
    """The allowlist waives only the public-IP check — an allowlisted http host is rejected."""
    monkeypatch.setattr(mcp_oauth, "_resolve_ips", lambda host: ["10.1.0.91"])
    monkeypatch.setattr(mcp_oauth, "get_settings", _allow_settings)
    with pytest.raises(McpOauthError, match="non-https"):
        mcp_oauth.assert_public_https("http://internal-mcp.example.com/mcp")


# --------------------------------------------------------------------------- #
# pin_url — the DNS-rebinding mitigation (validate, then pin to the validated IP)
# --------------------------------------------------------------------------- #
def test_pin_url_pins_validated_ip_and_preserves_host_and_sni() -> None:
    """pin_url must connect to the *validated* IP while keeping the original Host header and
    TLS SNI — a refactor that drops the pin or the SNI must fail here, not pass silently."""
    pinned, host_header, extensions = mcp_oauth.pin_url("https://mcp.example.com/mcp?a=1")
    assert pinned == "https://93.184.216.34/mcp?a=1"  # authority swapped to the resolved IP
    assert host_header == "mcp.example.com"  # Host header preserved
    assert extensions == {"sni_hostname": "mcp.example.com"}  # TLS SNI preserved


def test_pin_url_rejects_non_public() -> None:
    mcp_oauth._resolve_ips = lambda host: ["10.0.0.5"]
    with pytest.raises(McpOauthError):
        mcp_oauth.pin_url("https://internal.example.com/x")


# --------------------------------------------------------------------------- #
# _http — response-size cap and transport-fault mapping
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_http_caps_oversized_response(monkeypatch) -> None:
    """A body over the cap is rejected (recoverable McpOauthError), not buffered to OOM."""
    monkeypatch.setattr(mcp_oauth, "MCP_OAUTH_MAX_RESPONSE_BYTES", 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1024)

    _use_transport(handler)
    with pytest.raises(McpOauthError, match="exceeds"):
        await mcp_oauth._http("GET", "https://mcp.example.com/big")


@pytest.mark.asyncio
async def test_http_transport_fault_is_recoverable() -> None:
    """A connect/read fault surfaces as McpOauthError (recoverable), never an uncaught error —
    so in the refresh path a transient blip can't be mistaken for a dead grant."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _use_transport(handler)
    with pytest.raises(McpOauthError, match="failed"):
        await mcp_oauth._http("GET", "https://mcp.example.com/x")


# --------------------------------------------------------------------------- #
# _token_request — 503-vs-401 contract (transient vs terminal)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_token_request_500_is_recoverable() -> None:
    """A 5xx from the token endpoint is a transient broker fault → McpOauthError (→503),
    distinct from a 400/401 grant rejection → GrantDeadError (→401)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server_error"})

    _use_transport(handler)
    with pytest.raises(McpOauthError):
        await mcp_oauth._token_request("https://as.example.com/token", {"grant_type": "x"}, None)


@pytest.mark.asyncio
async def test_token_request_400_is_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    _use_transport(handler)
    with pytest.raises(GrantDeadError):
        await mcp_oauth._token_request("https://as.example.com/token", {"grant_type": "x"}, None)


# --------------------------------------------------------------------------- #
# exchange_code ordering — the DEK is checked BEFORE the single-use state is consumed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_exchange_code_no_dek_does_not_consume_state(monkeypatch) -> None:
    """Connecting with no DEK (browser not yet handshaked) must NOT delete the pending state
    or call the token endpoint — the auth code stays spendable for a retry."""
    from app.services.mcp_dek_store import get_dek_store

    await get_dek_store().delete(_USER)
    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: _server())

    pending = MagicMock()
    pending.user_id = _USER
    pending.created_at = datetime.now(UTC)
    executed: list = []

    async def _execute(stmt):
        executed.append(stmt)
        return MagicMock(first=lambda: pending)

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()

    # No transport set → reaching the token endpoint would raise a different error.
    with pytest.raises(McpOauthError, match="encryption key"):
        await mcp_oauth.exchange_code(db, state="s", code="c", redirect_uri="x")  # dek=None
    # Exactly one execute (the peek SELECT); the DELETE-RETURNING never ran.
    assert len(executed) == 1


# --------------------------------------------------------------------------- #
# _load_active — an unusable grant is DEAD (→401 reconnect), never an uncaught error
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_get_fresh_token_undecryptable_blob_is_grant_dead() -> None:
    """A blob sealed under a different DEK (stale/mismatched key) must surface as
    GrantDeadError → 401 reconnect, NOT bubble VaultDecryptError to the proxy's 503 loop."""
    blob, iv = seal(generate_dek(), json.dumps({"access_token": "x"}).encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv = blob, iv
    db = AsyncMock()
    db.get = AsyncMock(return_value=token)
    with pytest.raises(GrantDeadError):
        await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=generate_dek())


@pytest.mark.asyncio
async def test_get_fresh_token_non_dict_blob_is_grant_dead() -> None:
    """A blob that decrypts to valid-but-non-object JSON must fail closed as GrantDeadError,
    not crash on blob["access_token"]."""
    dek = generate_dek()
    blob, iv = seal(dek, json.dumps("not-an-object").encode())
    token = McpUserToken(user_id=_USER, mcp_key="notion", vault_ref="r", status="active")
    token.blob, token.blob_iv = blob, iv
    db = AsyncMock()
    db.get = AsyncMock(return_value=token)
    with pytest.raises(GrantDeadError):
        await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek)


# --------------------------------------------------------------------------- #
# Expired/invalid client registration (invalid_client) self-heal
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_token_request_invalid_client_raises_client_dead() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_client", "error_description": "client registration expired"},
        )

    _use_transport(handler)
    with pytest.raises(ClientDeadError):
        await mcp_oauth._token_request(
            "https://as.example.com/token", {"grant_type": "refresh_token"}, None
        )


@pytest.mark.asyncio
async def test_token_request_invalid_grant_stays_grant_dead() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    _use_transport(handler)
    with pytest.raises(GrantDeadError):
        await mcp_oauth._token_request("https://as.example.com/token", {}, None)


@pytest.mark.asyncio
async def test_get_fresh_token_invalid_client_drops_client_and_reconnects(monkeypatch) -> None:
    server = _server()
    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: server)
    drop = AsyncMock()
    monkeypatch.setattr(mcp_oauth, "_drop_client", drop)
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
        return httpx.Response(401, json={"error": "invalid_client"})

    _use_transport(handler)
    # Surfaced as a dead grant so the proxy maps it to a 401 reconnect (same UX as invalid_grant).
    with pytest.raises(GrantDeadError):
        await mcp_oauth.get_fresh_token(db, user_id=_USER, mcp_key="notion", dek=dek)
    assert token.status == "expired"
    drop.assert_awaited_once_with(db, "notion", client_id="cid-123")


@pytest.mark.asyncio
async def test_exchange_code_invalid_client_drops_client(monkeypatch) -> None:
    server = _server()
    monkeypatch.setattr(mcp_oauth, "get_oauth_server", lambda key: server)
    drop = AsyncMock()
    monkeypatch.setattr(mcp_oauth, "_drop_client", drop)
    client = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id="cid-123",
    )
    db = _mock_db_for_exchange(client)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_client"})

    _use_transport(handler)
    with pytest.raises(McpOauthError, match="registration has expired"):
        await mcp_oauth.exchange_code(
            db, state="s", code="c", redirect_uri="https://app/cb", dek=generate_dek()
        )
    drop.assert_awaited_once_with(db, "notion", client_id="cid-123")


@pytest.mark.asyncio
async def test_ensure_client_reregisters_expired_client(monkeypatch) -> None:
    server = _server(client_id=None)  # forces DCR
    disco = Discovery(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint="https://as.example.com/register",
        revocation_endpoint=None,
    )
    expired = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id="old-cid",
    )
    expired.client_expires_at = datetime.now(UTC) - timedelta(days=1)
    fresh = McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id="new-cid",
    )
    drop = AsyncMock()
    monkeypatch.setattr(mcp_oauth, "_drop_client", drop)
    gets = [expired, expired, fresh]  # existing-check, under-lock re-check, persisted re-read

    async def _get(model, pk):
        return gets.pop(0) if gets else fresh

    db = AsyncMock()
    db.get = AsyncMock(side_effect=_get)
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/register"
        return httpx.Response(201, json={"client_id": "new-cid", "client_secret_expires_at": 0})

    _use_transport(handler)
    row = await mcp_oauth.ensure_client(db, server, redirect_uri="https://app/cb", disco=disco)
    assert row.client_id == "new-cid"  # re-registered, not the lapsed one
    drop.assert_awaited_once_with(db, "notion", client_id="old-cid")


def test_client_expired_helper() -> None:
    c = McpOauthClient(mcp_key="k", issuer="i", token_endpoint="t", client_id="c")
    c.client_expires_at = None
    assert mcp_oauth._client_expired(c) is False
    c.client_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert mcp_oauth._client_expired(c) is True
    c.client_expires_at = datetime.now(UTC) + timedelta(days=1)
    assert mcp_oauth._client_expired(c) is False


# --------------------------------------------------------------------------- #
# Authorize-liveness preflight (silently-expired DCR client → re-register at connect)
# --------------------------------------------------------------------------- #
def _disco_dcr() -> Discovery:
    return Discovery(
        issuer="https://as.example.com",
        authorization_endpoint="https://as.example.com/authorize",
        token_endpoint="https://as.example.com/token",
        registration_endpoint="https://as.example.com/register",
        revocation_endpoint=None,
    )


def _client(client_id: str) -> McpOauthClient:
    return McpOauthClient(
        mcp_key="notion",
        issuer="https://as.example.com",
        token_endpoint="https://as.example.com/token",
        client_id=client_id,
    )


@pytest.mark.asyncio
async def test_client_alive_at_authorize_signal() -> None:
    az = "https://as.example.com/authorize"
    # invalid_client → dead
    _use_transport(lambda r: httpx.Response(400, json={"error": "invalid_client"}))
    assert await mcp_oauth._client_alive_at_authorize(az, "cid", "https://app/cb") is False
    # a different error (e.g. redirect_uri) → the client itself is fine → alive
    _use_transport(lambda r: httpx.Response(400, json={"error": "invalid_request"}))
    assert await mcp_oauth._client_alive_at_authorize(az, "cid", "https://app/cb") is True
    # login page → alive
    _use_transport(lambda r: httpx.Response(200, text="<html>login</html>"))
    assert await mcp_oauth._client_alive_at_authorize(az, "cid", "https://app/cb") is True
    # non-JSON error body mentioning invalid_client → dead
    _use_transport(lambda r: httpx.Response(400, text="error=invalid_client; expired"))
    assert await mcp_oauth._client_alive_at_authorize(az, "cid", "https://app/cb") is False


@pytest.mark.asyncio
async def test_build_authorize_url_reregisters_when_client_dead(monkeypatch) -> None:
    server = _server(client_id=None)
    monkeypatch.setattr(mcp_oauth, "discover", AsyncMock(return_value=_disco_dcr()))
    ensure = AsyncMock(side_effect=[_client("dead-cid"), _client("new-cid")])
    monkeypatch.setattr(mcp_oauth, "ensure_client", ensure)
    monkeypatch.setattr(mcp_oauth, "_client_alive_at_authorize", AsyncMock(return_value=False))
    drop = AsyncMock()
    monkeypatch.setattr(mcp_oauth, "_drop_client", drop)
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    url = await mcp_oauth.build_authorize_url(
        db, server, user_id=_USER, redirect_uri="https://app/cb"
    )
    assert "client_id=new-cid" in url  # the re-registered client is used in the popup URL
    drop.assert_awaited_once_with(db, server.key, client_id="dead-cid")
    assert ensure.await_count == 2


@pytest.mark.asyncio
async def test_build_authorize_url_keeps_live_client(monkeypatch) -> None:
    server = _server(client_id=None)
    monkeypatch.setattr(mcp_oauth, "discover", AsyncMock(return_value=_disco_dcr()))
    ensure = AsyncMock(return_value=_client("live-cid"))
    monkeypatch.setattr(mcp_oauth, "ensure_client", ensure)
    monkeypatch.setattr(mcp_oauth, "_client_alive_at_authorize", AsyncMock(return_value=True))
    drop = AsyncMock()
    monkeypatch.setattr(mcp_oauth, "_drop_client", drop)
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    url = await mcp_oauth.build_authorize_url(
        db, server, user_id=_USER, redirect_uri="https://app/cb"
    )
    assert "client_id=live-cid" in url
    drop.assert_not_awaited()
    assert ensure.await_count == 1
