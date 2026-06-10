"""Tests for the MCP catalog parser (app.services.mcp_catalog)."""

import base64
import json

from app.services.mcp_catalog import build_mcp_servers, parse_catalog


def _encode(server_def: dict) -> str:
    return base64.b64encode(json.dumps(server_def).encode()).decode()


def _catalog(entries: dict[str, dict]) -> str:
    return json.dumps({key: _encode(d) for key, d in entries.items()})


class TestParseCatalog:
    def test_empty_is_empty(self):
        assert parse_catalog("") == {}
        assert parse_catalog("   ") == {}

    def test_decodes_each_entry(self):
        coralogix = {"command": "npx", "args": ["coralogix-mcp"], "env": {"KEY": "secret"}}
        raw = _catalog({"coralogix": coralogix})
        assert parse_catalog(raw) == {"coralogix": coralogix}

    def test_invalid_top_level_json_yields_empty(self):
        assert parse_catalog("not json") == {}

    def test_non_object_top_level_yields_empty(self):
        assert parse_catalog(json.dumps(["a", "b"])) == {}

    def test_bad_base64_entry_is_skipped_others_kept(self):
        good = {"command": "x"}
        raw = json.dumps({"good": _encode(good), "bad": "!!!not-base64!!!"})
        assert parse_catalog(raw) == {"good": good}

    def test_entry_decoding_to_non_object_is_skipped(self):
        raw = json.dumps({"weird": base64.b64encode(b'"just a string"').decode()})
        assert parse_catalog(raw) == {}


class TestBuildMcpServers:
    def test_builds_only_enabled_known_keys(self, monkeypatch):
        a = {"command": "a"}
        b = {"command": "b"}
        from app.services import mcp_catalog

        monkeypatch.setattr(mcp_catalog, "get_mcp_catalog", lambda: {"a": a, "b": b})
        # Unknown key ignored; order follows the requested keys.
        assert build_mcp_servers(["b", "unknown"]) == {"b": b}
