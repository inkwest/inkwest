"""Tests for the MCP -> .claude.json merge (app.services.mcp_config)."""

import json

from app.services.mcp_config import (
    apply_session_mcp_selection,
    merge_mcp_selection,
)

CORALOGIX = {"command": "npx", "args": ["coralogix-mcp"]}
GITLAB = {"command": "npx", "args": ["gitlab-mcp"]}


class TestMergeMcpSelection:
    def test_adds_enabled_into_empty_config(self):
        out = merge_mcp_selection({}, {"coralogix": CORALOGIX}, {"coralogix", "gitlab"})
        assert out == {"mcpServers": {"coralogix": CORALOGIX}}

    def test_prunes_deselected_catalog_entry(self):
        config = {"mcpServers": {"coralogix": CORALOGIX, "gitlab": GITLAB}}
        # Only gitlab stays enabled -> coralogix (catalog-managed) is removed.
        out = merge_mcp_selection(config, {"gitlab": GITLAB}, {"coralogix", "gitlab"})
        assert out["mcpServers"] == {"gitlab": GITLAB}

    def test_preserves_non_catalog_entries(self):
        config = {"mcpServers": {"custom": {"command": "x"}}}
        out = merge_mcp_selection(config, {"coralogix": CORALOGIX}, {"coralogix"})
        # 'custom' isn't a catalog key, so it survives untouched alongside the new one.
        assert out["mcpServers"] == {"custom": {"command": "x"}, "coralogix": CORALOGIX}

    def test_keeps_other_top_level_keys(self):
        config = {"theme": "dark", "mcpServers": {}}
        out = merge_mcp_selection(config, {"coralogix": CORALOGIX}, {"coralogix"})
        assert out["theme"] == "dark"

    def test_empty_selection_removes_mcpservers_key(self):
        config = {"mcpServers": {"coralogix": CORALOGIX}}
        out = merge_mcp_selection(config, {}, {"coralogix"})
        assert "mcpServers" not in out


class TestApplySessionMcpSelection:
    def test_writes_merged_file_in_sandbox_mode(self, tmp_path, monkeypatch):
        from app.services import mcp_config

        path = tmp_path / "claude" / ".claude.json"
        monkeypatch.setattr(mcp_config, "claude_json_path", lambda _sid: str(path))
        monkeypatch.setattr(mcp_config, "get_mcp_catalog", lambda: {"coralogix": CORALOGIX})
        monkeypatch.setattr(
            mcp_config, "build_mcp_servers", lambda keys: {"coralogix": CORALOGIX} if keys else {}
        )

        apply_session_mcp_selection("sess-1", ["coralogix"])

        written = json.loads(path.read_text())
        assert written["mcpServers"] == {"coralogix": CORALOGIX}

    def test_noop_when_path_is_none(self, monkeypatch):
        from app.services import mcp_config

        monkeypatch.setattr(mcp_config, "claude_json_path", lambda _sid: None)
        # Should not raise even though no file exists / no catalog set.
        apply_session_mcp_selection("sess-1", ["coralogix"])
