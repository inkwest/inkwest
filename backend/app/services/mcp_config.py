"""
Merge a conversation's selected MCP servers into its ``.claude.json``.

Claude Code reads its MCP servers from the ``mcpServers`` key of the config file
in ``CLAUDE_CONFIG_DIR``. In sandbox mode that file is per-conversation
(``<session_dir>/claude/.claude.json``), so each conversation can enable a
different set of MCP servers (see app.services.mcp_catalog for the catalog and
docs/MCP.md for the design).

The merge is **catalog-scoped**: it adds/updates the entries the user enabled and
removes catalog-managed entries that are no longer enabled (deselection), while
leaving any non-catalog ``mcpServers`` entry untouched.

Note: per-conversation selection requires sandbox mode. Without it, all
conversations share a single ``~/.claude.json``, so there is nothing to isolate;
the apply step is a no-op there.
"""

import contextlib
import json
import logging
import os
import tempfile
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.mcp_user_token import McpUserToken
from app.services.mcp_catalog import build_mcp_servers, get_mcp_catalog
from app.services.mcp_oauth_catalog import get_oauth_catalog
from app.services.mcp_proxy_token import mint_stable

logger = logging.getLogger(__name__)


def merge_mcp_selection(
    config: dict[str, Any],
    enabled_servers: dict[str, dict[str, Any]],
    catalog_keys: set[str],
) -> dict[str, Any]:
    """
    Return a copy of ``config`` with its ``mcpServers`` reconciled to the selection.

    - enabled catalog entries are added/updated,
    - catalog-managed entries no longer enabled are removed (deselection),
    - any ``mcpServers`` entry NOT owned by the catalog is preserved as-is.

    Args:
        config: The current parsed ``.claude.json`` (``{}`` if the file is new).
        enabled_servers: ``{key: server_def}`` for the keys the user enabled.
        catalog_keys: All keys the catalog knows about — used to scope removals so
            we only prune entries we own, never a user/baseline server.
    """
    servers = dict(config.get("mcpServers") or {})

    # Prune catalog-managed servers that are no longer enabled.
    for key in catalog_keys:
        if key not in enabled_servers:
            servers.pop(key, None)

    # Add / update the enabled ones.
    servers.update(enabled_servers)

    merged = dict(config)
    if servers:
        merged["mcpServers"] = servers
    else:
        merged.pop("mcpServers", None)
    return merged


def claude_json_path(session_id: str) -> str | None:
    """
    Host path to the conversation's ``.claude.json``, or None if not applicable.

    Returns None in non-sandbox mode (the config is a single shared file, so
    per-conversation selection cannot be isolated).
    """
    if not get_settings().sandbox_enabled:
        return None
    # Import here to avoid a hard import-time dependency on the sandbox module.
    from app.services import sandbox

    return os.path.join(sandbox.session_dir(session_id), "claude", ".claude.json")


def _read_config(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        # A corrupt config must not wedge the conversation; start from a clean
        # object rather than crash (the CLI would rewrite it anyway).
        logger.warning("Could not read %s, starting fresh: %s", path, e)
        return {}


def _write_config_atomic(path: str, config: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # Write to a temp file in the same dir, then rename — readers never see a
    # half-written config. World-writable so the toolbox (a different uid) can
    # later let `claude` rewrite it; per-conversation dir, so not a leak.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".claude.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
        os.chmod(tmp, 0o666)  # noqa: S103 — per-conversation dir; toolbox uid must rewrite
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def _oauth_proxy_servers(
    db: AsyncSession,
    *,
    user_id: UUID,
    session_id: str,
    enabled_oauth_keys: list[str],
    existing: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Build ``.claude.json`` entries for enabled oauth keys; return (servers, dead_keys).

    An enabled oauth server is injected only when its per-user grant is ``active``; the entry
    points at the in-app proxy and carries a mint-stable internal JWT (never the real token).
    Keys whose grant is missing/expired/revoked are returned as ``dead`` so the caller can tell
    the user to reconnect. With no ``mcp_proxy_base_url`` configured, oauth injection is skipped
    silently (the feature isn't wired) — not reported as a dead grant.
    """
    base = get_settings().mcp_proxy_base_url
    if not base:
        logger.debug("oauth MCP injection skipped: mcp_proxy_base_url unset")
        return {}, []

    rows = (
        await db.execute(
            select(McpUserToken.mcp_key, McpUserToken.status).where(
                McpUserToken.user_id == user_id,
                McpUserToken.mcp_key.in_(enabled_oauth_keys),
            )
        )
    ).all()
    status_by_key = {key: status for key, status in rows}

    servers: dict[str, dict[str, Any]] = {}
    dead: list[str] = []
    base = base.rstrip("/")
    for key in enabled_oauth_keys:
        if status_by_key.get(key) != "active":
            dead.append(key)
            continue
        prior = (existing.get(key) or {}).get("headers", {}).get("Authorization", "")
        prior_jwt = prior[7:] if prior.lower().startswith("bearer ") else None
        token = mint_stable(prior_jwt, user_id, UUID(session_id), key)
        servers[key] = {
            "type": "http",
            "url": f"{base}/api/mcp-proxy/{key}/",
            "headers": {"Authorization": f"Bearer {token}"},
        }
    return servers, dead


async def oauth_proxy_servers_for(
    session_id: str,
    enabled_keys: list[str],
    *,
    user_id: UUID | None,
    db: AsyncSession,
    existing: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Proxy-URL ``mcpServers`` entries for the enabled OAuth2 keys + the dead keys.

    Shared by the claude-cli (``.claude.json``) and inkwest-agent engines so OAuth2 MCP
    brokering is identical across both. The agent materializer reads only the static
    catalog (``build_mcp_servers``), so without this the Converse engine would silently
    drop every OAuth2 server. Selects the oauth subset of ``enabled_keys``, then defers
    to :func:`_oauth_proxy_servers` (grant check + mint-stable JWT). No oauth keys, or no
    ``user_id``, yields ``({}, [])``.
    """
    oauth_catalog = get_oauth_catalog()
    enabled_oauth = [k for k in enabled_keys if k in oauth_catalog]
    if not enabled_oauth or user_id is None:
        return {}, []
    return await _oauth_proxy_servers(
        db,
        user_id=user_id,
        session_id=session_id,
        enabled_oauth_keys=enabled_oauth,
        existing=existing or {},
    )


async def apply_session_mcp_selection(
    session_id: str, enabled_keys: list[str], *, user_id: UUID | None, db: AsyncSession
) -> list[str]:
    """
    Reconcile the conversation's ``.claude.json`` ``mcpServers`` to ``enabled_keys``.

    Static catalog entries inject verbatim; enabled oauth entries inject a proxy URL + a
    mint-stable internal JWT when the user's grant is ``active`` (see
    :func:`_oauth_proxy_servers`). Returns the list of enabled oauth keys whose grant is NOT
    active (so the caller can surface an ``mcp_auth_required`` event). Idempotent — a stable
    JWT keeps an unchanged selection a no-op write. No-op in non-sandbox mode.
    """
    path = claude_json_path(session_id)
    if path is None:
        logger.debug("MCP selection skipped for %s: sandbox disabled", session_id)
        return []

    static_catalog = get_mcp_catalog()
    oauth_catalog = get_oauth_catalog()
    enabled_static = [k for k in enabled_keys if k in static_catalog]

    config = _read_config(path)
    existing_servers = config.get("mcpServers") or {}
    oauth_servers, dead = await oauth_proxy_servers_for(
        session_id, enabled_keys, user_id=user_id, db=db, existing=existing_servers
    )

    enabled_servers = {**build_mcp_servers(enabled_static), **oauth_servers}
    catalog_keys = set(static_catalog) | set(oauth_catalog)

    merged = merge_mcp_selection(config, enabled_servers, catalog_keys)
    if merged != config:
        _write_config_atomic(path, merged)
        logger.info(
            "Applied MCP selection for session %s: %d server(s) enabled",
            session_id,
            len(enabled_servers),
        )
    return dead
