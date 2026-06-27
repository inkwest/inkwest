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


def test_is_http_server_classifies_transport():
    """HTTP/SSE servers carry a url; stdio servers carry command/args. Only http is brokerable."""
    from app.services.mcp_catalog import is_http_server

    assert is_http_server({"type": "http", "url": "https://x/mcp"}) is True
    assert is_http_server({"url": "https://x/mcp"}) is True  # type optional, url is the signal
    assert is_http_server({"command": "npx", "args": ["x"]}) is False  # stdio
    assert is_http_server({}) is False


def test_get_static_http_server(monkeypatch):
    from app.services import mcp_catalog

    http = {"type": "http", "url": "https://x/mcp", "headers": {"X-API-Key": "k"}}
    stdio = {"command": "npx", "args": ["y"]}
    monkeypatch.setattr(mcp_catalog, "get_mcp_catalog", lambda: {"h": http, "s": stdio})
    assert mcp_catalog.get_static_http_server("h") == http
    assert mcp_catalog.get_static_http_server("s") is None  # stdio: not brokerable
    assert mcp_catalog.get_static_http_server("missing") is None
