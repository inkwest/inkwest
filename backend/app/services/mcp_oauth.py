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

``get_fresh_token`` is the proxy hot path: refresh single-flight per (user, key), a
post-lock recency re-check, persist-before-use of rotated refresh tokens, and a negative
cache so an AS outage can't become a refresh storm.
"""

import asyncio
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit
from uuid import UUID

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.constants import (
    MCP_OAUTH_HTTP_TIMEOUT,
    MCP_OAUTH_MAX_RESPONSE_BYTES,
    MCP_OAUTH_STATE_TTL_SECONDS,
)
from app.core.crypto import VaultDecryptError, seal, unseal, vault_ref
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


# Refresh hot-path coordination (OAuth 2.1 ASes rotate refresh tokens on use, so two
# concurrent refreshes would brick each other). Single-flight per (user, key); a short
# negative cache so an AS outage doesn't turn CLI tool-call retries into a refresh storm.
_refresh_locks: dict[tuple[str, str], asyncio.Lock] = {}
_refresh_failed_at: dict[tuple[str, str], float] = {}  # monotonic ts of last AS failure
_REFRESH_SKEW_S = 60  # refresh when within this of expiry
_RECENT_REFRESH_S = 30  # a token refreshed this recently is reused (post-lock re-check)
_NEG_CACHE_S = 30  # fail fast if a refresh failed this recently


def _refresh_lock(user_id: UUID, mcp_key: str) -> asyncio.Lock:
    return _refresh_locks.setdefault((str(user_id), mcp_key), asyncio.Lock())


def sweep_refresh_state() -> int:
    """Drop negative-cache entries older than the back-off window; return the count removed.

    Bounds ``_refresh_failed_at`` on a long-lived single worker (entries are otherwise only
    cleared on the success/revoke paths). Called from the connector-cleanup loop. The
    single-flight lock dicts are deliberately NOT swept: a lock may be held or about to be
    acquired via an existing reference, and replacing its dict entry would let a concurrent
    caller mint a second lock for the same key — breaking single-flight.
    """
    cutoff = time.monotonic() - _NEG_CACHE_S
    stale = [k for k, ts in _refresh_failed_at.items() if ts < cutoff]
    for k in stale:
        _refresh_failed_at.pop(k, None)
    return len(stale)


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
def _allow_hosts() -> frozenset[str]:
    """Exact hostnames waived from the public-IP requirement (MCP_OAUTH_ALLOW_HOSTS)."""
    raw = get_settings().mcp_oauth_allow_hosts
    return frozenset(h.strip() for h in raw.split(",") if h.strip())


# Audit the SSRF waiver once per host, not per request — an allowlisted host is hit many
# times per flow (discover, PRM, issuer, each endpoint, plus per-request IP-pinning).
_warned_allow_hosts: set[str] = set()


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
    # Explicit operator allowlist (MCP_OAUTH_ALLOW_HOSTS): named hosts may resolve to a
    # non-public address — for internal/VPN-hosted MCP servers behind private DNS. https +
    # IP-pinning still apply; this only waives the public-IP requirement, and only for these
    # exact hosts. Keyed on hostname, never on the IP, so it can't be widened by DNS.
    host_allowed = host in _allow_hosts()
    for ip in ips:
        # Allow-list, not deny-list: only globally-routable addresses pass. A deny-list of
        # private/loopback/link-local/etc. silently admits special-purpose ranges it forgot —
        # e.g. RFC 6598 CGNAT 100.64.0.0/10, routable to internal infra in cloud/k8s/NAT.
        # ``is_global`` rejects every non-public range without enumerating them.
        if not ipaddress.ip_address(ip).is_global:
            if not host_allowed:
                raise McpOauthError(f"refusing URL resolving to non-public address {ip}")
            if host not in _warned_allow_hosts:
                _warned_allow_hosts.add(host)
                logger.warning("mcp_oauth_allowlisted_private_host", extra={"host": host, "ip": ip})
    return ips


def assert_public_https(url: str) -> None:
    """Raise unless ``url`` is https and resolves only to public addresses."""
    _validated_ips(url)


def pin_url(url: str) -> tuple[str, str, dict[str, str]]:
    """Validate ``url`` and return ``(pinned_url, host_header, extensions)`` for an SSRF-safe
    fetch: connect to the validated IP while preserving the Host header + TLS SNI (so cert
    verification is unaffected). Closes the DNS-rebinding TOCTOU. Raises McpOauthError if the
    URL is not public https. Shared by ``_http`` and the streaming MCP proxy.
    """
    parts = urlsplit(url)
    ip = _validated_ips(url)[0]
    authority = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        authority = f"{authority}:{parts.port}"
    pinned = urlunsplit((parts.scheme, authority, parts.path, parts.query, parts.fragment))
    extensions = {"sni_hostname": parts.hostname} if parts.hostname else {}
    return pinned, parts.netloc, extensions


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
    pinned, host_header, extensions = pin_url(url)
    headers = {**kwargs.pop("headers", {}), "Host": host_header}
    try:
        async with _client() as client:
            req = client.build_request(
                method, pinned, headers=headers, extensions=extensions, **kwargs
            )
            resp = await client.send(req, stream=True)
            try:
                # Stream-cap the body: a compromised AS/MCP endpoint can return endpoints we
                # then fetch, so bound bytes (not just time) to keep one bad host from OOMing
                # the single worker. Rebuild a buffered Response so callers keep .json()/.headers.
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MCP_OAUTH_MAX_RESPONSE_BYTES:
                        raise McpOauthError(
                            f"{method} {url} response exceeds {MCP_OAUTH_MAX_RESPONSE_BYTES} bytes"
                        )
                    chunks.append(chunk)
            finally:
                await resp.aclose()
            return httpx.Response(
                status_code=resp.status_code,
                headers=resp.headers,
                content=b"".join(chunks),
                request=resp.request,
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
    assert_public_https(server_url)
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

    assert_public_https(resource_metadata_url)
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
    assert_public_https(issuer)
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
            assert_public_https(ep)
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

    The DEK is taken from the in-RAM store (the browser supplied it on WS connect) using the
    ``user_id`` recovered from the state row; ``dek`` overrides that for tests.

    Ordering is load-bearing. The state is *peeked* (read, not consumed) first so the DEK can
    be verified present BEFORE anything irreversible happens: consuming the single-use state
    or spending the auth code with no DEK to seal the result would burn both and force the
    user to redo the whole popup with no way to recover. Only once we hold a DEK do we consume
    the state (atomic DELETE … RETURNING — still single-use, the CSRF/replay protection for the
    unauthenticated callback) and then call the token endpoint. If the token exchange succeeds
    but the DB write fails, the code is already spent — the error surfaces and the user redoes
    the popup; we never retry the exchange.
    """
    peek = (
        await db.execute(
            select(
                McpOauthPendingState.user_id,
                McpOauthPendingState.created_at,
            ).where(McpOauthPendingState.state == state)
        )
    ).first()
    if peek is None:
        raise McpOauthError("unknown or already-used state")
    if peek.created_at < datetime.now(UTC) - timedelta(seconds=MCP_OAUTH_STATE_TTL_SECONDS):
        raise McpOauthError("state expired")

    if dek is None:
        dek = await get_dek_store().get(peek.user_id)
        if dek is None:
            # State + code untouched — the user can retry once the browser (and thus the DEK)
            # is connected, instead of being stranded with a spent code.
            raise McpOauthError("no encryption key available; reconnect the browser and retry")

    # Consume the state now (single-use, atomic). A concurrent callback racing the same state
    # loses here (RETURNING yields no row) and is rejected.
    pending = (
        await db.execute(
            delete(McpOauthPendingState)
            .where(McpOauthPendingState.state == state)
            .returning(
                McpOauthPendingState.code_verifier,
                McpOauthPendingState.mcp_key,
                McpOauthPendingState.user_id,
            )
        )
    ).first()
    await db.commit()
    if pending is None:
        raise McpOauthError("unknown or already-used state")

    server = get_oauth_server(pending.mcp_key)
    if server is None:
        raise McpOauthError(f"catalog key {pending.mcp_key!r} no longer exists")
    client = await db.get(McpOauthClient, pending.mcp_key)
    if client is None:
        raise McpOauthError("client registration missing")

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


def _expiring(row: McpUserToken) -> bool:
    return row.expires_at is not None and row.expires_at <= datetime.now(UTC) + timedelta(
        seconds=_REFRESH_SKEW_S
    )


async def _load_active(
    db: AsyncSession, user_id: UUID, mcp_key: str, dek: bytes
) -> tuple[McpUserToken, dict[str, Any]]:
    row = await db.get(McpUserToken, (user_id, mcp_key))
    if row is None or row.status != "active" or row.blob is None or row.blob_iv is None:
        raise GrantDeadError(f"no active grant for {mcp_key}")
    try:
        blob = json.loads(unseal(dek, row.blob, row.blob_iv))
    except (VaultDecryptError, ValueError) as e:
        # Undecryptable (stale/mismatched DEK) or non-JSON plaintext (corrupted blob): the
        # grant is unusable. Surface it as DEAD (→ 401 reconnect) rather than letting it
        # bubble to the proxy's generic handler as 503 ("try again"), which would loop the
        # user forever with no path to recovery. JSONDecodeError is a ValueError subclass.
        raise GrantDeadError(f"grant blob undecryptable for {mcp_key}") from e
    # We only ever seal a dict; a non-dict here means a corrupted blob that still
    # authenticated (not cryptographically reachable, but cheap to fail closed) — guard it so
    # downstream blob["access_token"] can't raise an uncaught TypeError past this DEAD mapping.
    if not isinstance(blob, dict):
        raise GrantDeadError(f"grant blob is not an object for {mcp_key}")
    return row, blob


async def get_fresh_token(
    db: AsyncSession, *, user_id: UUID, mcp_key: str, dek: bytes | None = None, force: bool = False
) -> str:
    """Return a usable access token, refreshing if expired/forced. Raises on a dead grant.

    Refresh discipline (OAuth 2.1 ASes rotate refresh tokens on use): single-flight per
    (user, key); a post-lock re-check reuses a token a competitor just refreshed so N
    concurrent 401s don't produce N rotations; the rotated blob is persisted (``_store_token``
    commits) BEFORE the new access token is returned; a recent AS failure short-circuits via a
    negative cache so an outage can't become a retry storm. Decrypts with the in-RAM DEK.
    """
    if dek is None:
        dek = await get_dek_store().get(user_id)
        if dek is None:
            # The DEK lives in RAM only while the browser's WS is up; a dropped/reconnecting
            # socket evicts it. That is a *transient* broker condition (recoverable on
            # reconnect), NOT a dead grant — raise McpOauthError so the proxy maps it to 503
            # ("try again") rather than GrantDeadError → 401 ("reconnect OAuth"), which would
            # tell the user to redo a perfectly valid grant over a momentary network blip.
            raise McpOauthError("no encryption key available; browser not connected")

    row, blob = await _load_active(db, user_id, mcp_key, dek)
    if not force and not _expiring(row):
        return str(blob["access_token"])
    if not blob.get("refresh_token"):
        return str(blob["access_token"])  # non-expiring / no refresh material: use as-is

    cache_key = (str(user_id), mcp_key)
    last_fail = _refresh_failed_at.get(cache_key)
    if last_fail is not None and time.monotonic() - last_fail < _NEG_CACHE_S:
        # An AS refresh failed moments ago — fail fast instead of queueing on the lock and
        # amplifying an outage into a sequential retry storm.
        raise McpOauthError("MCP token refresh is backing off after a recent failure")

    async with _refresh_lock(user_id, mcp_key):
        # Post-lock re-check: a competitor on this lock may have just rotated the token.
        await db.refresh(row)
        row, blob = await _load_active(db, user_id, mcp_key, dek)
        recently_refreshed = row.updated_at is not None and (
            datetime.now(UTC) - row.updated_at < timedelta(seconds=_RECENT_REFRESH_S)
        )
        if recently_refreshed and not _expiring(row):
            return str(blob["access_token"])
        refresh_token = blob.get("refresh_token")
        if not refresh_token:
            return str(blob["access_token"])

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
            row.status = "expired"  # invalid_grant — the grant is dead, reconnect needed
            await db.commit()
            raise
        except McpOauthError:
            _refresh_failed_at[cache_key] = time.monotonic()  # transient — back off briefly
            raise
        _refresh_failed_at.pop(cache_key, None)
        # _store_token commits the rotated blob — persist-before-use: the new access token is
        # returned only after the new refresh token is durably stored.
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

    row = await db.get(McpUserToken, (user_id, server.key))
    # RFC 6749 §5.1: an omitted `scope` on a refresh means "unchanged". Carry the previously
    # granted scopes forward rather than overwriting them with the (possibly broader) catalog
    # default, so the stored/audited scopes keep reflecting what was actually granted.
    scopes = (
        token.get("scope") or (row.scopes if row is not None else None) or " ".join(server.scopes)
    )
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
    # Set explicitly (not only via the DB onupdate) so the single-flight post-lock recency
    # check sees a just-rotated token even before a DB round-trip.
    row.updated_at = datetime.now(UTC)
    await db.commit()
    return row


async def mark_expired(db: AsyncSession, *, user_id: UUID, mcp_key: str) -> None:
    """Mark a grant ``expired`` (e.g. upstream kept returning 401). Idempotent."""
    # Clear any refresh back-off so a fresh reconnect isn't stuck in the negative cache.
    _refresh_failed_at.pop((str(user_id), mcp_key), None)
    row = await db.get(McpUserToken, (user_id, mcp_key))
    if row is not None and row.status == "active":
        row.status = "expired"
        await db.commit()


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
    _refresh_failed_at.pop((str(user_id), mcp_key), None)  # don't strand a reconnect in back-off
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
