"""
OAuth2 MCP catalog — remote MCP servers users may connect via OAuth 2.1.

The source of truth is the ``mcp_oauth_catalog`` table (admin-managed); the legacy
``MCP_CATALOG_OAUTH2`` env var is **seeded into the table once** on first startup
(when the table is empty) and ignored thereafter. The env value keeps the same outer
shape as ``MCP_CATALOG`` — a JSON object ``{"<key>": "<base64(json)>"}`` whose decoded
definitions are the oauth kind::

    {"type": "http", "url": "https://mcp.example.com/mcp", "scopes": ["mcp:read"],
     "client_id": "optional", "client_secret": "optional"}

Kept deliberately separate from :mod:`app.services.mcp_catalog`: the static catalog
(Coralogix, kubectl, …) is injected verbatim into ``.claude.json`` and never touches
the OAuth broker, while these entries drive the discover → DCR → PKCE → token flow.
The static catalog stays on env for now — only the OAuth2 catalog moved to the DB.

Caching mirrors :mod:`app.services.model_registry`: the **enabled** rows are loaded
into an in-memory snapshot at startup and refreshed on every admin write, keeping the
hot path (per-turn ``oauth_proxy_servers_for``) sync and DB-free. Disabled rows are
configured-but-not-selectable.

Security: definitions stay server-side. Only keys (and a per-user auth status) ever
reach the browser; ``client_secret`` is never serialized — not to chat clients, not to
the admin panel (which sees a ``has_secret`` boolean instead).
"""

import asyncio
import base64
import binascii
import json
import logging
import re
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.secretbox import (
    SecretBoxError,
    is_sealed,
    parse_keyring,
    seal,
    sealed_version,
    unseal,
)
from app.models.mcp_oauth_catalog import McpOauthCatalog
from app.services import pubsub


class DuplicateKeyError(Exception):
    """Raised when creating a catalog entry whose key already exists (→ HTTP 409)."""


# A catalog key is interpolated into a URL path (/api/mcp-proxy/<key>/) and used as a
# .claude.json mcpServers key, so restrict it to URL- and JSON-safe characters.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

logger = logging.getLogger(__name__)


class OAuthMcpServer:
    """A validated OAuth2 MCP catalog entry. Lightweight, secret-free to expose by key."""

    __slots__ = ("key", "url", "scopes", "client_id", "client_secret", "display_name", "enabled")

    def __init__(self, key: str, definition: dict[str, Any]) -> None:
        self.key = key
        self.url: str = definition["url"]
        # Scopes are optional; some authorization servers grant a default set.
        scopes = definition.get("scopes") or []
        self.scopes: list[str] = [str(s) for s in scopes]
        # client_id absent => Dynamic Client Registration (RFC 7591) at connect time.
        self.client_id: str | None = definition.get("client_id")
        self.client_secret: str | None = definition.get("client_secret")
        # display_name/enabled are DB-row metadata; absent in the seed definition
        # (env entries are always enabled, labelled by key).
        self.display_name: str | None = definition.get("display_name")
        self.enabled: bool = bool(definition.get("enabled", True))


def _is_valid_def(key: str, definition: dict[str, Any]) -> bool:
    """Validate an oauth entry: streamable-HTTP transport with an https URL."""
    if definition.get("type") != "http":
        logger.error("MCP_CATALOG_OAUTH2 entry %r must have type 'http', skipping", key)
        return False
    url = definition.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        logger.error("MCP_CATALOG_OAUTH2 entry %r must have an https url, skipping", key)
        return False
    return True


def parse_oauth_catalog(raw: str) -> dict[str, OAuthMcpServer]:
    """
    Parse ``MCP_CATALOG_OAUTH2`` into ``{key: OAuthMcpServer}``.

    Tolerant by design (same contract as :func:`app.services.mcp_catalog.parse_catalog`):
    a malformed catalog or a single bad entry is logged and skipped, never fatal — a
    broken catalog yields fewer selectable servers, it does not take the app down.
    """
    if not raw.strip():
        return {}

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("MCP_CATALOG_OAUTH2 is not valid JSON, ignoring it: %s", e)
        return {}

    if not isinstance(entries, dict):
        logger.error(
            "MCP_CATALOG_OAUTH2 must be a JSON object {key: base64(def)}, got %s", type(entries)
        )
        return {}

    catalog: dict[str, OAuthMcpServer] = {}
    for key, encoded in entries.items():
        if not isinstance(encoded, str):
            logger.error("MCP_CATALOG_OAUTH2 entry %r must be a base64 string, skipping", key)
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True)
            definition = json.loads(decoded)
        except (binascii.Error, ValueError) as e:
            logger.error(
                "MCP_CATALOG_OAUTH2 entry %r is not valid base64(JSON), skipping: %s", key, e
            )
            continue
        if not isinstance(definition, dict) or not _is_valid_def(key, definition):
            continue
        catalog[key] = OAuthMcpServer(key, definition)

    return catalog


# In-memory snapshot of the ENABLED catalog rows, loaded at startup and refreshed on
# every admin write so the per-turn hot path stays sync and DB-free (like model_registry).
_servers: dict[str, OAuthMcpServer] = {}
# Serializes writes so a concurrent admin save can't leave the snapshot holding a
# different writer's values than the DB (the reload is post-commit).
_save_lock = asyncio.Lock()


def _load_keyring() -> dict[int, bytes]:
    """Parse the configured keyring, degrading to empty (sealing disabled) on a bad value.

    A malformed ``SECRETBOX_KEYS`` must not crash the app — it disables sealing (and any
    sealed row then fails to unseal and is dropped from the snapshot, logged), rather than
    taking the whole deployment down.
    """
    try:
        return parse_keyring(get_settings().secretbox_keys)
    except SecretBoxError:
        # Don't log the parse error verbatim — it could echo part of the key value.
        logger.error("invalid SECRETBOX_KEYS, secret encryption disabled")
        return {}


def secret_storage_enabled() -> bool:
    """True when a usable encryption keyring is configured (so secrets CAN be sealed).

    Reflects the *parsed* keyring, not just a non-empty env var — a malformed value
    degrades to disabled, and this must report that so the UI doesn't offer Confidential
    (which would then fail to store anything).
    """
    return bool(_load_keyring())


def _secret_readable(stored: str | None, keyring: dict[int, bytes]) -> bool:
    """True if there's no secret, or it's sealed with a key version we hold.

    A non-sealed (clear-text) value is treated as NOT readable: operator secrets must be
    sealed at rest, so a clear-text leftover is invalid and the server is not editable
    (delete/recreate). Cheap: checks key-version membership, never decrypts.
    """
    if stored is None:
        return True
    if not is_sealed(stored):
        return False
    v = sealed_version(stored)
    return v is not None and v in keyring


def _server_from_row(row: McpOauthCatalog, keyring: dict[int, bytes]) -> OAuthMcpServer:
    secret = row.client_secret
    if secret is not None:
        # Secrets are sealed-or-nothing; a clear-text value is invalid, not "legacy".
        if not is_sealed(secret):
            raise SecretBoxError(f"server {row.key!r} has an unsealed (clear-text) secret")
        secret = unseal(secret, keyring)  # may raise SecretBoxError — caught by the caller
    return OAuthMcpServer(
        row.key,
        {
            "url": row.url,
            "scopes": row.scopes,
            "client_id": row.client_id,
            "client_secret": secret,
            "display_name": row.display_name,
            "enabled": row.enabled,
        },
    )


async def load_oauth_catalog(db: AsyncSession) -> None:
    """Load the enabled catalog into the in-memory snapshot; seed from env on first run.

    Called at startup. When the table is empty and ``MCP_CATALOG_OAUTH2`` is set, the env
    catalog is seeded into the table once (so existing deployments keep their servers),
    then the table is authoritative.

    Resilient: a row whose secret can't be unsealed (retired key version, missing/bad
    keyring) is logged and dropped from the snapshot rather than aborting the load — a
    rotation mistake or config error must not crash startup or 500 every admin write.
    """
    global _servers
    rows = (await db.execute(select(McpOauthCatalog))).scalars().all()
    if not rows:
        seeded = await _seed_from_env(db)
        if seeded:
            rows = (await db.execute(select(McpOauthCatalog))).scalars().all()
    keyring = _load_keyring()
    servers: dict[str, OAuthMcpServer] = {}
    for row in rows:
        if not row.enabled:
            continue
        try:
            servers[row.key] = _server_from_row(row, keyring)
        except SecretBoxError as e:
            logger.error(
                "dropping MCP server %r from catalog: cannot unseal its secret (%s)", row.key, e
            )
    _servers = servers
    if _servers:
        logger.info("Loaded %d enabled OAuth2 MCP catalog server(s)", len(_servers))


async def _seed_from_env(db: AsyncSession) -> int:
    """One-time seed of the table from the legacy ``MCP_CATALOG_OAUTH2`` env value.

    Secrets are sealed at seed time. With no keyring a secret CANNOT be stored (operator
    secrets are sealed-or-nothing) — the row is seeded without it and an error is logged so
    the operator sets a key and re-adds the secret. Returns the number of rows inserted.
    No-op when the env value is empty/invalid.
    """
    parsed = parse_oauth_catalog(get_settings().mcp_catalog_oauth2)
    if not parsed:
        return 0
    keyring = _load_keyring()
    for key, server in parsed.items():
        secret = server.client_secret
        if secret:
            if keyring:
                secret = seal(secret, keyring)
            else:
                logger.error(
                    "seeded MCP server %r has a client_secret but SECRETBOX_KEYS is unset; "
                    "seeding it without the secret — set a key and re-add it",
                    key,
                )
                secret = None
        db.add(
            McpOauthCatalog(
                key=key,
                url=server.url,
                scopes=server.scopes,
                client_id=server.client_id,
                client_secret=secret,
                display_name=server.display_name,
                enabled=True,
            )
        )
    await db.commit()
    logger.info("Seeded %d OAuth2 MCP catalog server(s) from MCP_CATALOG_OAUTH2", len(parsed))
    return len(parsed)


def get_oauth_catalog() -> dict[str, OAuthMcpServer]:
    """Return the enabled OAuth2 MCP catalog ``{key: OAuthMcpServer}`` (in-memory snapshot)."""
    return _servers


def oauth_catalog_keys() -> list[str]:
    """Return the enabled OAuth2 MCP keys (safe to expose to the browser)."""
    return sorted(_servers.keys())


def get_oauth_server(key: str) -> OAuthMcpServer | None:
    """Return a single enabled OAuth2 MCP catalog entry, or None if unknown/disabled."""
    return _servers.get(key)


# ---------------------------------------------------------------------------
# Admin management (DB writes + snapshot refresh). Never expose client_secret.
# ---------------------------------------------------------------------------


def _validate_url(url: str) -> None:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("MCP server url must be an https:// URL")


def _validate_key(key: str) -> None:
    if not _KEY_RE.match(key):
        raise ValueError(
            "MCP server key must be 1-128 chars of letters, digits, '.', '-' or '_' "
            "and start with a letter or digit"
        )


async def list_oauth_entries(db: AsyncSession) -> list[dict[str, Any]]:
    """All catalog rows (incl. disabled) as secret-free dicts for the admin panel.

    ``client_secret`` is replaced by a ``has_secret`` boolean — the raw value never
    leaves the server.
    """
    rows = (await db.execute(select(McpOauthCatalog).order_by(McpOauthCatalog.key))).scalars().all()
    keyring = _load_keyring()
    return [
        {
            "key": r.key,
            "display_name": r.display_name,
            "url": r.url,
            "scopes": list(r.scopes or []),
            "client_id": r.client_id,
            "has_secret": r.client_secret is not None,
            # False when the secret is sealed but can't be decrypted with the current keyring
            # (no key, or the sealing version was retired). Such a server is already dropped
            # from the live snapshot and must not be edited — only deleted/recreated.
            "editable": _secret_readable(r.client_secret, keyring),
            "enabled": r.enabled,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


async def _broadcast_catalog_change() -> None:
    """Best-effort global broadcast that the OAuth-MCP catalog changed.

    The *set* of servers is global (unlike per-user auth_status), so this fans out to every
    connection (``user_id=None``) to drop the client-side catalog cache and reload. Emitted
    after the in-memory snapshot is refreshed so a client reloading immediately sees the new
    state. Swallowed on failure: the write is already durable, and clients also revalidate on
    mount/popup-open (stale-while-revalidate), so a dropped notify is a missed live push, not
    lost state — it must not turn a successful admin write into an error.
    """
    try:
        await pubsub.notify({"type": "mcp_catalog_changed"}, user_id=None)
    except Exception:  # noqa: BLE001 — broadcast is advisory; never fail the write on it
        logger.warning("mcp_catalog_broadcast_failed", exc_info=True)


async def upsert_oauth_server(
    db: AsyncSession,
    *,
    key: str,
    url: str,
    scopes: list[str] | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    display_name: str | None = None,
    enabled: bool = True,
    create_only: bool = False,
) -> None:
    """Create or update a catalog entry, then refresh the snapshot.

    ``client_secret=None`` on an update **preserves** the existing secret (so editing a
    server's url/scopes from the panel — which never receives the secret back — does not
    wipe it). Pass an empty string to clear it explicitly.

    ``create_only=True`` rejects an existing key with :class:`DuplicateKeyError` (so the
    create endpoint doesn't silently overwrite); otherwise the write is an upsert.
    """
    _validate_key(key)
    _validate_url(url)

    # client_secret semantics: None = keep the stored one (coalesce); "" = clear it (store
    # NULL); a real value = seal it before it touches the DB. Sealing needs a configured key.
    if client_secret is None:
        secret_value: str | None = None
        preserve_secret = True
    elif client_secret == "":
        secret_value = None
        preserve_secret = False
    else:
        keyring = parse_keyring(get_settings().secretbox_keys)
        if not keyring:
            raise ValueError(
                "cannot store a client secret: no encryption key configured (SECRETBOX_KEYS)"
            )
        secret_value = seal(client_secret, keyring)
        preserve_secret = False

    async with _save_lock:
        # Create must not clobber an existing key (the upsert path is for updates). The lock
        # makes the check-then-insert race-free within this process (single-replica deploy);
        # the PK constraint is the cross-process backstop, surfaced as DuplicateKeyError too.
        if create_only:
            exists = await db.scalar(select(McpOauthCatalog.key).where(McpOauthCatalog.key == key))
            if exists is not None:
                raise DuplicateKeyError(key)

        stmt = pg_insert(McpOauthCatalog).values(
            key=key,
            url=url,
            scopes=scopes or [],
            client_id=client_id,
            client_secret=secret_value,
            display_name=display_name,
            enabled=enabled,
        )
        if create_only:
            try:
                await db.execute(stmt)
            except IntegrityError as e:
                await db.rollback()
                raise DuplicateKeyError(key) from e
        else:
            secret_set = (
                func.coalesce(stmt.excluded.client_secret, McpOauthCatalog.client_secret)
                if preserve_secret  # writer didn't resend one → keep what's stored
                else stmt.excluded.client_secret  # seal a new one, or NULL to clear
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[McpOauthCatalog.key],
                set_={
                    "url": stmt.excluded.url,
                    "scopes": stmt.excluded.scopes,
                    "client_id": stmt.excluded.client_id,
                    "client_secret": secret_set,
                    "display_name": stmt.excluded.display_name,
                    "enabled": stmt.excluded.enabled,
                    "updated_at": func.now(),
                },
            )
            await db.execute(stmt)
        await db.commit()
        await load_oauth_catalog(db)

    await _broadcast_catalog_change()


async def delete_oauth_server(db: AsyncSession, key: str) -> None:
    """Delete a catalog entry, then refresh the snapshot. Raises if the key is unknown."""
    async with _save_lock:
        result = await db.execute(delete(McpOauthCatalog).where(McpOauthCatalog.key == key))
        # execute() is typed Result, but a DML statement yields a CursorResult with rowcount.
        if cast("CursorResult[Any]", result).rowcount == 0:
            raise ValueError(f"Unknown MCP server key: {key}")
        await db.commit()
        await load_oauth_catalog(db)

    # Only reached on a real delete; an unknown key raises above, before any broadcast.
    await _broadcast_catalog_change()
