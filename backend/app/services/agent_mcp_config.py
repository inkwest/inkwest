"""
Materialize a conversation's enabled MCP servers for the sandboxed inkwest-agent (P1.7).

The claude-cli engine reads MCP servers from its ``.claude.json`` (see
:mod:`app.services.mcp_config`); the custom Converse agent has no such native config,
so the broker writes a sibling JSON file the agent's lazy MCP client reads — same
``{"mcpServers": {key: server_def}}`` shape so the catalog definitions are reused
verbatim (command/args/env or url/headers/type), including any creds/headers a
definition embeds.

Cred posture (one-way, mirrors the connector-cred projection): the file is written
into the conversation's durable claude dir just before the turn and **removed on turn
end** — the agent reads it, never calls back to the portal for MCP creds. It lives in
the per-conversation dir (the isolation boundary, never shared across sessions); the
mode is 0644 because the toolbox runs as a different uid than the backend and must
read it through the mount (same reasoning as the SKILL.md / .claude.json materializers).

No-op outside sandbox mode (no per-conversation dir to write into).
"""

import contextlib
import json
import logging
import os
import tempfile

from app.core.config import get_settings
from app.services.mcp_catalog import build_mcp_servers

logger = logging.getLogger(__name__)

# Basename of the agent's MCP config in the conversation's claude dir. Distinct from
# .claude.json (the CLI's) so the two engines never clobber each other's config.
_MCP_CONFIG_NAME = "inkwest-agent-mcp.json"


def agent_mcp_config_host_path(session_id: str) -> str | None:
    """Host path of the conversation's agent MCP config, or None off-sandbox.

    Sibling of the CLI's ``.claude.json`` under the ``claude/`` mount that maps to
    ``<sandbox_home>/.claude`` in-container.
    """
    if not get_settings().sandbox_enabled:
        return None
    from app.services import sandbox

    return os.path.join(sandbox.session_dir(session_id), "claude", _MCP_CONFIG_NAME)


def agent_mcp_config_container_path() -> str:
    """In-container path the agent reads the MCP config from (the turn-spec carries it)."""
    return f"{get_settings().sandbox_home.rstrip('/')}/.claude/{_MCP_CONFIG_NAME}"


def write_agent_mcp_config(session_id: str, enabled_keys: list[str]) -> str | None:
    """Write the enabled MCP server defs for the turn; return the in-container path.

    Returns None when there is nothing to write (off-sandbox, or no enabled servers)
    so the broker can leave ``mcp_config_path`` unset and the agent skips MCP entirely.
    Best-effort: a write failure logs and degrades to no MCP rather than failing the turn.
    """
    host_path = agent_mcp_config_host_path(session_id)
    if host_path is None:
        return None
    servers = build_mcp_servers(enabled_keys)
    if not servers:
        # Remove any stale file from a prior turn that had servers enabled.
        remove_agent_mcp_config(session_id)
        return None

    payload = json.dumps({"mcpServers": servers}, indent=2)
    try:
        os.makedirs(os.path.dirname(host_path), mode=0o755, exist_ok=True)
        _write_atomic(host_path, payload)
    except OSError as e:
        logger.warning("agent_mcp_config_write_failed", extra={"error": str(e)})
        return None
    logger.info(
        "agent_mcp_config_written",
        extra={"session_id": session_id, "servers": len(servers)},
    )
    return agent_mcp_config_container_path()


def remove_agent_mcp_config(session_id: str) -> None:
    """Delete the conversation's agent MCP config (turn-end cleanup; never raises).

    The config may embed MCP creds/headers, so it does not outlive the turn — it is
    re-materialized from the DB at the next turn (mirrors the connector-cred lifecycle).
    """
    host_path = agent_mcp_config_host_path(session_id)
    if host_path is None:
        return
    with contextlib.suppress(OSError):
        os.unlink(host_path)


def _write_atomic(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{_MCP_CONFIG_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        # 0644: per-conversation dir is the isolation boundary; the toolbox (other uid)
        # must read this through the mount.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
