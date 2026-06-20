"""Tests for the broker's MCP config materialization for the inkwest-agent (P1.7).

The broker writes the conversation's enabled MCP servers into a sandbox file (so the
DB-free agent can read them) and removes it on turn end (it may embed creds).
"""

import json
import os
from unittest.mock import MagicMock, patch

from app.services import agent_mcp_config as amc

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _settings(tmp_path, *, enabled=True):
    return MagicMock(sandbox_enabled=enabled, sandbox_home="/home/agent")


def _patches(tmp_path, *, enabled=True):
    return (
        patch.object(amc, "get_settings", return_value=_settings(tmp_path, enabled=enabled)),
        patch("app.services.sandbox.session_dir", return_value=str(tmp_path)),
    )


def test_write_noop_off_sandbox(tmp_path):
    s, d = _patches(tmp_path, enabled=False)
    with s, d:
        assert amc.write_agent_mcp_config(SESSION_ID, ["coralogix-prod"]) is None


def test_write_no_enabled_servers_returns_none(tmp_path):
    s, d = _patches(tmp_path)
    with s, d, patch.object(amc, "build_mcp_servers", return_value={}):
        assert amc.write_agent_mcp_config(SESSION_ID, []) is None


def test_write_materializes_config_and_returns_container_path(tmp_path):
    (tmp_path / "claude").mkdir()
    server_def = {"type": "http", "url": "https://api.eu2.coralogix.com/mgmt/api/v1/mcp"}
    s, d = _patches(tmp_path)
    with s, d, patch.object(amc, "build_mcp_servers", return_value={"cx": server_def}):
        container_path = amc.write_agent_mcp_config(SESSION_ID, ["cx"])

    assert container_path == "/home/agent/.claude/inkwest-agent-mcp.json"
    host_path = tmp_path / "claude" / "inkwest-agent-mcp.json"
    assert host_path.exists()
    written = json.loads(host_path.read_text())
    assert written == {"mcpServers": {"cx": server_def}}
    # 0644: a different uid (the toolbox) reads it through the mount.
    assert oct(host_path.stat().st_mode & 0o777) == "0o644"


def test_remove_deletes_config(tmp_path):
    (tmp_path / "claude").mkdir()
    host_path = tmp_path / "claude" / "inkwest-agent-mcp.json"
    host_path.write_text("{}")
    s, d = _patches(tmp_path)
    with s, d:
        amc.remove_agent_mcp_config(SESSION_ID)
    assert not host_path.exists()


def test_remove_is_safe_when_absent(tmp_path):
    (tmp_path / "claude").mkdir()
    s, d = _patches(tmp_path)
    with s, d:
        amc.remove_agent_mcp_config(SESSION_ID)  # must not raise


def test_write_then_disable_prunes_stale_config(tmp_path):
    (tmp_path / "claude").mkdir()
    host_path = tmp_path / "claude" / "inkwest-agent-mcp.json"
    s, d = _patches(tmp_path)
    with s, d, patch.object(amc, "build_mcp_servers", return_value={"cx": {"url": "u"}}):
        amc.write_agent_mcp_config(SESSION_ID, ["cx"])
    assert host_path.exists()
    # Next turn: nothing enabled → the stale file is pruned, not left behind.
    s2, d2 = _patches(tmp_path)
    with s2, d2, patch.object(amc, "build_mcp_servers", return_value={}):
        assert amc.write_agent_mcp_config(SESSION_ID, []) is None
    assert not host_path.exists()


def test_container_path_uses_sandbox_home(tmp_path):
    s, d = _patches(tmp_path)
    with s, d:
        assert amc.agent_mcp_config_container_path() == "/home/agent/.claude/inkwest-agent-mcp.json"


def test_host_path_none_off_sandbox(tmp_path):
    s, d = _patches(tmp_path, enabled=False)
    with s, d:
        assert amc.agent_mcp_config_host_path(SESSION_ID) is None


def test_write_degrades_when_dir_unwritable(tmp_path):
    # A write failure (e.g. permission) must degrade to None, not crash the turn.
    s, d = _patches(tmp_path)
    with (
        s,
        d,
        patch.object(amc, "build_mcp_servers", return_value={"cx": {"url": "u"}}),
        patch.object(amc, "_write_atomic", side_effect=OSError("disk full")),
        patch("os.makedirs"),
    ):
        assert amc.write_agent_mcp_config(SESSION_ID, ["cx"]) is None


def test_no_leftover_tmp_file_on_write(tmp_path):
    (tmp_path / "claude").mkdir()
    s, d = _patches(tmp_path)
    with s, d, patch.object(amc, "build_mcp_servers", return_value={"cx": {"url": "u"}}):
        amc.write_agent_mcp_config(SESSION_ID, ["cx"])
    leftovers = [f for f in os.listdir(tmp_path / "claude") if f.endswith(".tmp")]
    assert leftovers == []
