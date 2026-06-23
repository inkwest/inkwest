"""
WebSocket DEK handshake (browser-fractioned key custody, audit-doc Phase C).

The per-user data-encryption key (DEK) that seals MCP token blobs is reconstructed in
server RAM only while the user is connected. The browser holds a non-extractable wrapping
key in IndexedDB (per origin+profile, shared across tabs) and this handshake moves the DEK
between the two over the (TLS) chat WebSocket:

    browser → ``mcp_dek_open``
    server  → ``mcp_dek_challenge`` (wrapped DEK)   if a key row exists
              ↳ browser unwraps → ``mcp_dek_supply`` (raw DEK) → server holds it in RAM
            or ``mcp_dek_bootstrap`` (fresh DEK)     if no key row yet (or on reset)
              ↳ browser wraps → ``mcp_dek_wrapped`` → server persists the wrapped form
    browser → ``mcp_dek_reset``                      when it cannot unwrap (lost/rotated key)

The raw DEK crosses only in RAM and is never persisted server-side; the wrapped DEK on disk
is useless without the browser's key. A connection refcount evicts the DEK when the user's
last socket closes, so a dump taken with nobody connected yields no plaintext.

Security model of ``mcp_dek_supply``: the server CANNOT verify the supplied raw DEK against
the stored wrapped DEK — that is the whole point of fractioning (only the browser holds the
unwrapping key). The blast radius is therefore bounded by construction: the DEK is keyed by
the WS-authenticated ``user_id``, so a client can only ever install a DEK for *its own*
grants. A bad/garbage DEK can corrupt only that user's own token decryption (self-inflicted),
never another user's. We do NOT trust a supplied DEK as authoritative; convergence across a
user's tabs is handled by making bootstrap reuse the live RAM DEK (below).

Same single-replica / session-affinity constraint as the credential and warm-grant stores
(the OAuth callback reads this RAM DEK by ``user_id`` and must hit the same process).

Multi-device caveat: ``mcp_user_keys`` holds ONE wrapped DEK per user, wrapped by that
browser/profile's non-extractable key. A second device (different wrapping key) that connects
gets the first device's wrapped form, fails to unwrap, and sends ``mcp_dek_reset`` → a fresh
DEK is minted and the row overwritten (last-writer-wins), so grants sealed under the previous
DEK become undecryptable and need a reconnect. Acceptable for the single-active-device target;
true multi-device custody would require a wrapped-DEK row per (user, wrapping-key).
"""

import asyncio
import base64
import binascii
import logging
from typing import Any
from uuid import UUID

from app.core.config import get_settings
from app.core.crypto import DEK_BYTES, generate_dek
from app.core.database import async_session_maker
from app.models.mcp_user_key import McpUserKey
from app.services.mcp_dek_store import get_dek_store
from app.services.mcp_oauth_catalog import oauth_catalog_keys

logger = logging.getLogger(__name__)

# Per-user count of live WebSockets; the DEK is evicted when it reaches zero so the
# "decryptable only while connected" property holds across tabs without N evictions.
# Guarded by ``_lock`` together with every store mutation so a disconnect-eviction and a
# concurrent connect/bootstrap on another tab cannot interleave (refcount race / DEK leak).
_conn_counts: dict[str, int] = {}
_lock = asyncio.Lock()

# Bounds on the client-supplied wrapped-DEK frame (base64-encoded). A 32-byte DEK wrapped by
# AES-GCM is ~60 bytes raw (~80 base64); these caps leave generous headroom for other wrap
# algorithms while stopping an authenticated client from pushing a multi-MB blob through the
# decode + DB insert (single-worker OOM vector). Checked on the encoded string BEFORE decode.
_MAX_WRAPPED_B64 = 4096
_MAX_IV_B64 = 64


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


async def note_connect(user_id: UUID) -> None:
    async with _lock:
        _conn_counts[str(user_id)] = _conn_counts.get(str(user_id), 0) + 1


async def note_disconnect(user_id: UUID) -> None:
    """Decrement the user's live-socket count; evict the DEK when it reaches zero."""
    async with _lock:
        key = str(user_id)
        # max(0, …) so an unbalanced disconnect can never drive the count negative and
        # wedge eviction; reaching zero (or below) evicts and clears the entry.
        remaining = max(0, _conn_counts.get(key, 0) - 1)
        if remaining == 0:
            _conn_counts.pop(key, None)
            await get_dek_store().delete(user_id)
        else:
            _conn_counts[key] = remaining


async def sweep_orphaned_deks() -> int:
    """Evict DEKs for users with no live socket; return the count removed.

    Eviction is normally synchronous in :func:`note_disconnect`, but a socket that dies
    without a clean close (missed ``finally``) could otherwise leave a DEK resident with no
    refcount entry — silently weakening the "decryptable only while connected" property. This
    is the backstop, called from the connector-cleanup loop. SAFE: a user with a live socket
    has a ``_conn_counts`` entry and is never swept; only DEKs with no refcount at all go.
    """
    store = get_dek_store()
    async with _lock:
        orphaned = [uid for uid in await store.user_ids() if uid not in _conn_counts]
        for uid in orphaned:
            await store.delete(UUID(uid))
    return len(orphaned)


async def handle_dek_message(user_id: UUID, data: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one ``mcp_dek_*`` WS message; return the reply to send (or None to ignore).

    Never raises on malformed client input — a bad frame yields an ``mcp_dek_error`` reply
    rather than propagating and tearing down the whole chat socket.
    """
    msg_type = data.get("type")

    if msg_type == "mcp_dek_open":
        # Gate: do no work (no DB read, no key row, no RAM DEK) unless OAuth2 MCP is actually
        # usable. Without this, EVERY chat WS connect for EVERY user would touch the DB and
        # bootstrap a key even in deployments that never configured an oauth MCP server.
        if not oauth_catalog_keys() or not get_settings().sandbox_enabled:
            return {"type": "mcp_dek_absent"}
        async with async_session_maker() as db:
            row = await db.get(McpUserKey, user_id)
        if row is None:
            return await _bootstrap(user_id)
        return {
            "type": "mcp_dek_challenge",
            "wrapped_dek": _b64e(row.wrapped_dek),
            "wrap_iv": _b64e(row.wrap_iv) if row.wrap_iv else None,
            "wrap_alg": row.wrap_alg,
        }

    if msg_type == "mcp_dek_reset":
        # The browser holds a key row but can't unwrap (key cleared/rotated). Start over —
        # any prior token blobs become undecryptable and will need a reconnect (Increment 1
        # has no proxy consuming them yet, so this is a clean re-key, not data loss).
        return await _bootstrap(user_id)

    if msg_type == "mcp_dek_supply":
        try:
            dek = _b64d(data.get("dek", ""))
        except (binascii.Error, ValueError):
            return {"type": "mcp_dek_error", "reason": "bad_base64"}
        if len(dek) != DEK_BYTES:
            return {"type": "mcp_dek_error", "reason": "bad_dek_length"}
        await get_dek_store().put(user_id, dek)
        return {"type": "mcp_dek_ready"}

    if msg_type == "mcp_dek_wrapped":
        wrapped_b64 = data.get("wrapped_dek", "")
        iv_b64 = data.get("wrap_iv") or ""
        # Cap the encoded length BEFORE decoding so a multi-MB frame never reaches _b64d/the DB.
        if len(wrapped_b64) > _MAX_WRAPPED_B64 or len(iv_b64) > _MAX_IV_B64:
            return {"type": "mcp_dek_error", "reason": "too_large"}
        try:
            wrapped = _b64d(wrapped_b64)
            iv = _b64d(iv_b64) if iv_b64 else None
        except (binascii.Error, ValueError):
            return {"type": "mcp_dek_error", "reason": "bad_base64"}
        if not wrapped:
            return {"type": "mcp_dek_error", "reason": "empty_wrapped_dek"}
        alg = data.get("wrap_alg", "AES-GCM")
        async with async_session_maker() as db:
            row = await db.get(McpUserKey, user_id)
            if row is None:
                db.add(McpUserKey(user_id=user_id, wrapped_dek=wrapped, wrap_alg=alg, wrap_iv=iv))
            else:
                row.wrapped_dek, row.wrap_alg, row.wrap_iv = wrapped, alg, iv
            await db.commit()
        return {"type": "mcp_dek_ready"}

    return None


async def _bootstrap(user_id: UUID) -> dict[str, Any]:
    """Generate (or reuse) the user's RAM DEK and hand it to the browser to wrap + persist.

    Reuse is what makes concurrent bootstraps converge: if two tabs both ``mcp_dek_open``
    with no key row, the first generates + stores the DEK and the second finds it already in
    RAM and reuses it. Without this, the second bootstrap would mint a *different* DEK and its
    ``mcp_dek_wrapped`` would overwrite the first's persisted wrapped form — orphaning any
    grant sealed under the first DEK (permanently undecryptable). The ``_lock`` makes the
    get-or-generate atomic across tabs.
    """
    async with _lock:
        dek = await get_dek_store().get(user_id)
        if dek is None:
            dek = generate_dek()
            await get_dek_store().put(user_id, dek)
    return {"type": "mcp_dek_bootstrap", "dek": _b64e(dek)}
