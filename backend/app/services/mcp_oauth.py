"""
OAuth2 MCP client — backend broker for the MCP Authorization spec (OAuth 2.1).

The headless, keychain-less sandbox CLI cannot run an interactive OAuth flow, so the
backend brokers it (exactly as it does for the GitLab/AWS connectors and OIDC login):

    discover (RFC 9728 + RFC 8414) → ensure_client (catalog client_id or RFC 7591 DCR)
    → build_authorize_url (auth-code + PKCE S256, RFC 8707 ``resource``)
    → exchange_code (seal tokens under the per-user DEK, persist the grant)

Tokens are encrypted at rest under a per-user DEK held in RAM only while the browser is
connected (see :mod:`app.core.crypto`, :mod:`app.services.mcp_dek_store`).

Deliberately self-contained: it does NOT factor shared OAuth plumbing out of the GitLab
connector (``app/services/connectors/gitlab.py``). That unification is a later cleanup
gated on characterization tests — touching the working connector now is needless risk.
``generate_pkce`` IS reused from the connector base (shared, already tested).

The proxy hot path (refresh single-flight, 30 s re-check, persist-before-use) lands in
Increment 2; ``get_fresh_token`` here is the minimal form its tests need.
"""

import asyncio
import ipaddress
import json
import logging
import re
import secrets
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit
from uuid import UUID

import httpx
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MCP_OAUTH_HTTP_TIMEOUT, MCP_OAUTH_STATE_TTL_SECONDS
from app.core.crypto import seal, unseal, vault_ref
from app.models.mcp_oauth_client import McpOauthClient
from app.models.mcp_oauth_pending_state import McpOauthPendingState
from app.models.mcp_user_token import McpUserToken
from app.services.connectors.base import generate_pkce
from app.services.mcp_dek_store import get_dek_store
from app.services.mcp_oauth_catalog import OAuthMcpServer, get_oauth_server

logger = logging.getLogger(__name__)

# Injectable for tests: set to an httpx.MockTransport to stub the AS/MCP server, and
# override the resolver so SSRF checks pass for fake public hostnames without real DNS.
_transport: httpx.AsyncBaseTransport | None = None


def _resolve_ips(host: str) -> list[str]:
    """Resolve a hostname to its IPs. Injectable so tests avoid real DNS."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


# Single-flight registry for Dynamic Client Registration: two concurrent connects for the
# same catalog key must not both POST the AS registration endpoint (orphaned AS clients).
_dcr_locks: dict[str, asyncio.Lock] = {}


def _dcr_lock(mcp_key: str) -> asyncio.Lock:
    return _dcr_locks.setdefault(mcp_key, asyncio.Lock())


class McpOauthError(Exception):
    """A recoverable OAuth2 MCP flow failure (discovery, registration, exchange)."""


class GrantDeadError(Exception):
    """The user's grant is unusable (invalid_grant / revoked) — reconnect required."""


@dataclass(frozen=True)
class Discovery:
    """Discovered authorization-server metadata for one MCP server."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None


# --------------------------------------------------------------------------- #
# SSRF guard — the backend has unrestricted egress (unlike the sandbox)
# --------------------------------------------------------------------------- #
def _validated_ips(url: str) -> list[str]:
    """Validate ``url`` is https and resolves only to public IPs; return those IPs.

    Discovery follows URLs the remote server hands us (resource metadata, issuer); without
    this guard a malicious catalog URL could pivot the backend into the internal network.
    The returned IP is reused by :func:`_http` to *pin* the actual connection — closing the
    DNS-rebinding TOCTOU where the guard sees a public IP and the request re-resolves to a
    private one.
    """
    parts = urlparse(url)
    if parts.scheme != "https":
        raise McpOauthError(f"refusing non-https URL: {parts.scheme}")
    host = parts.hostname
    if not host:
        raise McpOauthError("URL has no host")
    # An IP-literal host is checked directly — no DNS, and no resolver stub can mask it.
    try:
        ips: list[str] = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            ips = _resolve_ips(host)
        except OSError as e:
            raise McpOauthError(f"cannot resolve host {host!r}") from e
    if not ips:
        raise McpOauthError(f"cannot resolve host {host!r}")
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_reserved
            or addr.is_multicast
            or addr.is_unspecified
        ):
            raise McpOauthError(f"refusing URL resolving to non-public address {ip}")
    return ips


def _assert_public_https(url: str) -> None:
    """Raise unless ``url`` is https and resolves only to public addresses."""
    _validated_ips(url)


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


def _origin(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "", "", ""))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport, timeout=MCP_OAUTH_HTTP_TIMEOUT)


async def _http(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Validate + IP-pin + fetch, mapping any transport-level failure to McpOauthError.

    Pinning the connection to the exact IP we just validated closes the DNS-rebinding window
    (the guard and the socket use one resolution); the original hostname is preserved for the
    Host header and TLS SNI so certificate verification is unaffected. A bare httpx failure
    (timeout/connect/read) becomes a *recoverable* McpOauthError — never an uncaught 500, and
    in the refresh path distinct from a terminal GrantDeadError (so a blip can't kill a grant).
    """
    parts = urlsplit(url)
    ip = _validated_ips(url)[0]
    authority = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        authority = f"{authority}:{parts.port}"
    pinned = urlunsplit((parts.scheme, authority, parts.path, parts.query, parts.fragment))
    headers = {**kwargs.pop("headers", {}), "Host": parts.netloc}
    extensions = {"sni_hostname": parts.hostname} if parts.hostname else {}
    try:
        async with _client() as client:
            return await client.request(
                method, pinned, headers=headers, extensions=extensions, **kwargs
            )
    except httpx.HTTPError as e:
        raise McpOauthError(f"{method} {url} failed: {type(e).__name__}") from e


# --------------------------------------------------------------------------- #
# Discovery (RFC 9728 protected-resource metadata → RFC 8414 AS metadata)
# --------------------------------------------------------------------------- #
def _parse_resource_metadata_url(www_authenticate: str) -> str | None:
    """Pull ``resource_metadata="..."`` out of a ``WWW-Authenticate`` header.

    The header carries an auth-scheme token then comma-separated params, e.g.
    ``Bearer resource_metadata="https://…", error="…"`` — match the param anywhere.
    """
    m = re.search(r'resource_metadata\s*=\s*"?([^",\s]+)"?', www_authenticate)
    return m.group(1) if m else None


async def discover(server_url: str) -> Discovery:
    """Discover the authorization server for an MCP server URL.

    Path: unauthenticated request → ``401 WWW-Authenticate: resource_metadata=`` →
    RFC 9728 doc → RFC 8414 AS metadata. Falls back to probing the well-known paths at
    the server origin when the server does not challenge with a metadata pointer.
    """
    _assert_public_https(server_url)
    origin = _origin(server_url)

    resource_metadata_url: str | None = None
    resp = await _http("GET", server_url, headers={"Accept": "application/json"})
    if resp.status_code == 401:
        resource_metadata_url = _parse_resource_metadata_url(
            resp.headers.get("WWW-Authenticate", "")
        )

    # RFC 9728 binds the resource-metadata document to the resource's own origin.
    if resource_metadata_url is None:
        resource_metadata_url = f"{origin}/.well-known/oauth-protected-resource"
    elif not _same_origin(resource_metadata_url, server_url):
        raise McpOauthError("resource_metadata URL is cross-origin to the MCP server")

    _assert_public_https(resource_metadata_url)
    issuer = await _fetch_issuer(resource_metadata_url, origin)
    return await _fetch_as_metadata(issuer)


async def _fetch_issuer(resource_metadata_url: str, origin: str) -> str:
    resp = await _http("GET", resource_metadata_url, headers={"Accept": "application/json"})
    if resp.status_code != 200:
        # No PRM doc: fall back to treating the server origin as the issuer.
        return origin
    servers = resp.json().get("authorization_servers") or []
    if not servers:
        return origin
    issuer = str(servers[0])
    _assert_public_https(issuer)
    return issuer.rstrip("/")


async def _fetch_as_metadata(issuer: str) -> Discovery:
    # Try RFC 8414 then the OIDC discovery document.
    candidates = [
        f"{issuer}/.well-known/oauth-authorization-server",
        f"{issuer}/.well-known/openid-configuration",
    ]
    for url in candidates:
        resp = await _http("GET", url, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            continue
        meta = resp.json()
        token_endpoint = meta.get("token_endpoint")
        authorization_endpoint = meta.get("authorization_endpoint")
        if not token_endpoint or not authorization_endpoint:
            continue
        # RFC 8414 §3.3: the metadata's `issuer` MUST match the issuer we fetched it from,
        # or a PRM-pointed AS could hand back attacker-chosen (still-public) endpoints.
        returned_issuer = str(meta.get("issuer", "")).rstrip("/")
        if returned_issuer and returned_issuer != issuer.rstrip("/"):
            raise McpOauthError(f"AS metadata issuer mismatch: {returned_issuer!r} != {issuer!r}")
        for ep in (token_endpoint, authorization_endpoint):
            _assert_public_https(ep)
        return Discovery(
            issuer=meta.get("issuer", issuer),
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            registration_endpoint=meta.get("registration_endpoint"),
            revocation_endpoint=meta.get("revocation_endpoint"),
        )
    raise McpOauthError(f"no usable authorization-server metadata at {issuer}")


# --------------------------------------------------------------------------- #
# Client registration (catalog-pinned client_id or RFC 7591 DCR)
# --------------------------------------------------------------------------- #
async def ensure_client(
    db: AsyncSession, server: OAuthMcpServer, *, redirect_uri: str, disco: Discovery | None = None
) -> McpOauthClient:
    """Return the persisted OAuth client for ``server``, registering once if needed.

    Catalog-pinned ``client_id`` skips DCR. Otherwise RFC 7591 dynamic registration runs
    once and the result is persisted (``INSERT … ON CONFLICT DO NOTHING`` + re-read so two
    concurrent connects cannot double-register). Discovered endpoints are persisted too —
    the refresh hot path must never depend on a live discovery. ``disco`` lets the caller
    pass an already-fetched discovery to avoid a redundant round-trip.
    """
    existing = await db.get(McpOauthClient, server.key)
    if existing is not None:
        return existing

    # Single-flight per key: serialise concurrent first-connects so only one runs DCR /
    # the insert. The re-check inside the lock catches a peer that registered while we waited.
    async with _dcr_lock(server.key):
        existing = await db.get(McpOauthClient, server.key)
        if existing is not None:
            return existing
        return await _register_and_persist_client(
            db, server, redirect_uri=redirect_uri, disco=disco
        )


async def _register_and_persist_client(
    db: AsyncSession, server: OAuthMcpServer, *, redirect_uri: str, disco: Discovery | None
) -> McpOauthClient:
    if disco is None:
        disco = await discover(server.url)
    client_id = server.client_id
    client_secret_ref = server.client_secret
    client_expires_at: datetime | None = None

    if client_id is None:
        if not disco.registration_endpoint:
            raise McpOauthError(
                f"{server.key}: no client_id in catalog and the AS offers no DCR endpoint"
            )
        reg = await _register_client(disco.registration_endpoint, server, redirect_uri)
        client_id = reg["client_id"]
        # Prefer public (PKCE) clients; if the AS still issues a secret, persist it as-is.
        # Org-level secret-at-rest custody (KMS/sealed) is Increment 2 — noted limitation.
        client_secret_ref = reg.get("client_secret")
        if reg.get("client_secret_expires_at"):
            client_expires_at = datetime.fromtimestamp(int(reg["client_secret_expires_at"]), UTC)

    row = McpOauthClient(
        mcp_key=server.key,
        issuer=disco.issuer,
        token_endpoint=disco.token_endpoint,
        revocation_endpoint=disco.revocation_endpoint,
        client_id=client_id,
        client_secret_ref=client_secret_ref,
        client_expires_at=client_expires_at,
    )
    # Concurrency-safe: another connect for the same key may have inserted first.
    stmt = (
        pg_insert(McpOauthClient)
        .values(
            mcp_key=row.mcp_key,
            issuer=row.issuer,
            token_endpoint=row.token_endpoint,
            revocation_endpoint=row.revocation_endpoint,
            client_id=row.client_id,
            client_secret_ref=row.client_secret_ref,
            client_expires_at=row.client_expires_at,
        )
        .on_conflict_do_nothing(index_elements=["mcp_key"])
    )
    await db.execute(stmt)
    await db.commit()
    persisted = await db.get(McpOauthClient, server.key)
    assert persisted is not None  # noqa: S101 — just inserted (or a peer did)
    return persisted


async def _register_client(
    registration_endpoint: str, server: OAuthMcpServer, redirect_uri: str
) -> dict[str, Any]:
    body = {
        "client_name": f"Inkwest MCP ({server.key})",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",  # public client + PKCE
    }
    if server.scopes:
        body["scope"] = " ".join(server.scopes)
    resp = await _http("POST", registration_endpoint, json=body)
    if resp.status_code not in (200, 201):
        raise McpOauthError(f"DCR failed: HTTP {resp.status_code}")
    result: dict[str, Any] = resp.json()
    return result


# --------------------------------------------------------------------------- #
# Authorization + token exchange
# --------------------------------------------------------------------------- #
async def build_authorize_url(
    db: AsyncSession, server: OAuthMcpServer, *, user_id: UUID, redirect_uri: str
) -> str:
    """Persist a single-use pending state and return the AS authorization URL (PKCE S256)."""
    disco = await discover(server.url)
    client = await ensure_client(db, server, redirect_uri=redirect_uri, disco=disco)

    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = generate_pkce()
    db.add(
        McpOauthPendingState(
            state=state,
            code_verifier=code_verifier,
            mcp_key=server.key,
            user_id=user_id,
        )
    )
    await db.commit()

    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "resource": server.url,  # RFC 8707
    }
    if server.scopes:
        params["scope"] = " ".join(server.scopes)
    return f"{disco.authorization_endpoint}?{httpx.QueryParams(params)}"


async def exchange_code(
    db: AsyncSession, *, state: str, code: str, redirect_uri: str, dek: bytes | None = None
) -> McpUserToken:
    """Consume the pending state, exchange the code, seal tokens under the user's DEK, persist.

    The state row is deleted in the same step (single-use). The DEK is taken from the in-RAM
    store (the browser supplied it on WS connect) using the ``user_id`` recovered from the
    consumed state; ``dek`` overrides that for tests. If the token exchange succeeds but the
    DB write fails, the auth code is already spent — the error surfaces and the user redoes
    the popup; we never retry the exchange.

    Deleting the state BEFORE the token call is deliberate: single-use-on-read is the CSRF /
    replay protection for the unauthenticated callback, and that property is worth more than
    salvaging a transient AS failure — the popup is already gone, so there is nothing to retry
    against anyway; the user reconnects from the panel.
    """
    pending = (
        await db.execute(
            delete(McpOauthPendingState)
            .where(McpOauthPendingState.state == state)
            .returning(
                McpOauthPendingState.code_verifier,
                McpOauthPendingState.mcp_key,
                McpOauthPendingState.user_id,
                McpOauthPendingState.created_at,
            )
        )
    ).first()
    await db.commit()
    if pending is None:
        raise McpOauthError("unknown or already-used state")

    if pending.created_at < datetime.now(UTC) - timedelta(seconds=MCP_OAUTH_STATE_TTL_SECONDS):
        raise McpOauthError("state expired")

    server = get_oauth_server(pending.mcp_key)
    if server is None:
        raise McpOauthError(f"catalog key {pending.mcp_key!r} no longer exists")
    client = await db.get(McpOauthClient, pending.mcp_key)
    if client is None:
        raise McpOauthError("client registration missing")

    if dek is None:
        dek = await get_dek_store().get(pending.user_id)
        if dek is None:
            raise McpOauthError("no encryption key available; reconnect the browser and retry")

    token = await _token_request(
        client.token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client.client_id,
            "code_verifier": pending.code_verifier,
        },
        client.client_secret_ref,
    )
    return await _store_token(db, pending.user_id, server, token, dek)


async def get_fresh_token(
    db: AsyncSession, *, user_id: UUID, mcp_key: str, dek: bytes | None = None, force: bool = False
) -> str:
    """Return a usable access token, refreshing if expired/forced. Raises on a dead grant.

    Minimal form for this increment (the proxy adds single-flight + persist-before-use in
    Increment 2). Decrypts the stored blob with the in-RAM DEK (taken from the store unless
    ``dek`` is supplied).
    """
    if dek is None:
        dek = await get_dek_store().get(user_id)
        if dek is None:
            raise GrantDeadError("no encryption key available; browser not connected")
    row = await db.get(McpUserToken, (user_id, mcp_key))
    if row is None or row.status != "active" or row.blob is None or row.blob_iv is None:
        raise GrantDeadError(f"no active grant for {mcp_key}")
    blob = json.loads(unseal(dek, row.blob, row.blob_iv))

    expiring = row.expires_at is not None and row.expires_at <= datetime.now(UTC) + timedelta(
        seconds=60
    )
    if not force and not expiring:
        return str(blob["access_token"])

    refresh_token = blob.get("refresh_token")
    if not refresh_token:
        return str(blob["access_token"])  # non-expiring / no refresh material: use as-is

    client = await db.get(McpOauthClient, mcp_key)
    server = get_oauth_server(mcp_key)
    if client is None or server is None:
        raise GrantDeadError("client/catalog entry missing")
    try:
        token = await _token_request(
            client.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client.client_id,
            },
            client.client_secret_ref,
        )
    except GrantDeadError:
        row.status = "expired"
        await db.commit()
        raise
    await _store_token(db, user_id, server, token, dek)
    return str(token["access_token"])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _token_request(
    token_endpoint: str, data: dict[str, Any], client_secret: str | None
) -> dict[str, Any]:
    """POST the token endpoint. 400/401 → GrantDeadError; other non-200 → McpOauthError.

    Transport faults (timeout/connect/read) surface from ``_http`` as McpOauthError — a
    transient AS blip is recoverable and must NOT be mistaken for a dead grant.
    """
    if client_secret:
        data = {**data, "client_secret": client_secret}
    resp = await _http("POST", token_endpoint, data=data, headers={"Accept": "application/json"})
    if resp.status_code == 200:
        result: dict[str, Any] = resp.json()
        return result
    # 400/401 from the token endpoint = the grant/code is rejected — terminal.
    if resp.status_code in (400, 401):
        raise GrantDeadError(f"token endpoint rejected the grant: HTTP {resp.status_code}")
    raise McpOauthError(f"token endpoint error: HTTP {resp.status_code}")


async def _store_token(
    db: AsyncSession, user_id: UUID, server: OAuthMcpServer, token: dict[str, Any], dek: bytes
) -> McpUserToken:
    access_token = token.get("access_token")
    if not access_token:
        raise McpOauthError("token response missing access_token")
    expires_at: datetime | None = None
    if token.get("expires_in"):
        expires_at = datetime.now(UTC) + timedelta(seconds=int(token["expires_in"]))
    payload = {
        "access_token": access_token,
        "refresh_token": token.get("refresh_token"),
        "token_type": token.get("token_type", "Bearer"),
    }
    ciphertext, iv = seal(dek, json.dumps(payload).encode())
    scopes = token.get("scope") or " ".join(server.scopes)

    row = await db.get(McpUserToken, (user_id, server.key))
    if row is None:
        row = McpUserToken(
            user_id=user_id,
            mcp_key=server.key,
            vault_ref=vault_ref(user_id, server.key),
        )
        db.add(row)
    row.blob = ciphertext
    row.blob_iv = iv
    row.expires_at = expires_at
    row.scopes = scopes
    row.status = "active"
    await db.commit()
    return row


async def revoke_grant(
    db: AsyncSession, *, user_id: UUID, mcp_key: str, dek: bytes | None = None
) -> None:
    """Mark a grant revoked, best-effort RFC 7009 revoke upstream, drop the blob.

    Revocation is best-effort: a provider hiccup must not block the local de-scoping. The DEK
    (taken from the in-RAM store unless supplied) is only needed to read the access token for
    the upstream revoke; without it the local row is still revoked and the short-lived token
    expires on its own.
    """
    if dek is None:
        dek = await get_dek_store().get(user_id)
    row = await db.get(McpUserToken, (user_id, mcp_key))
    if row is None:
        return
    client = await db.get(McpOauthClient, mcp_key)
    if (
        dek is not None
        and client is not None
        and client.revocation_endpoint
        and row.blob is not None
        and row.blob_iv is not None
    ):
        try:
            blob = json.loads(unseal(dek, row.blob, row.blob_iv))
            await _revoke_upstream(client, blob.get("access_token"))
        except Exception:  # noqa: BLE001 — revoke is best-effort
            logger.debug("mcp_revoke_upstream_failed", extra={"mcp_key": mcp_key})
    row.status = "revoked"
    row.blob = None
    row.blob_iv = None
    await db.commit()


async def _revoke_upstream(client: McpOauthClient, access_token: str | None) -> None:
    if not access_token or not client.revocation_endpoint:
        return
    data = {"token": access_token, "client_id": client.client_id}
    if client.client_secret_ref:
        data["client_secret"] = client.client_secret_ref
    await _http("POST", client.revocation_endpoint, data=data)
