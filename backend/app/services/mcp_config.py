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

from app.core.config import get_settings
from app.services.mcp_catalog import build_mcp_servers, get_mcp_catalog

logger = logging.getLogger(__name__)


def merge_mcp_selection(
    config: dict,
    enabled_servers: dict[str, dict],
    catalog_keys: set[str],
) -> dict:
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


def _read_config(path: str) -> dict:
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


def _write_config_atomic(path: str, config: dict) -> None:
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


def apply_session_mcp_selection(session_id: str, enabled_keys: list[str]) -> None:
    """
    Reconcile the conversation's ``.claude.json`` ``mcpServers`` to ``enabled_keys``.

    Idempotent. No-op in non-sandbox mode. Takes effect on the next ``claude``
    process start (the CLI reads its config at launch), i.e. the next message.
    """
    path = claude_json_path(session_id)
    if path is None:
        logger.debug("MCP selection skipped for %s: sandbox disabled", session_id)
        return

    enabled_servers = build_mcp_servers(enabled_keys)
    catalog_keys = set(get_mcp_catalog().keys())

    config = _read_config(path)
    merged = merge_mcp_selection(config, enabled_servers, catalog_keys)
    if merged == config:
        return  # nothing changed; avoid a needless rewrite

    _write_config_atomic(path, merged)
    logger.info(
        "Applied MCP selection for session %s: %d server(s) enabled",
        session_id,
        len(enabled_servers),
    )
