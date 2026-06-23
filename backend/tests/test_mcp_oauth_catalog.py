"""Tests for the OAuth2 MCP catalog parser (app.services.mcp_oauth_catalog).

The contract mirrors the static catalog: tolerant by design — one malformed entry is logged
and skipped, never fatal — so a broken catalog yields fewer servers, not a dead startup.
"""

import base64
import json

from app.services.mcp_oauth_catalog import parse_oauth_catalog


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_empty_catalog() -> None:
    assert parse_oauth_catalog("") == {}
    assert parse_oauth_catalog("   ") == {}


def test_invalid_json_is_ignored() -> None:
    assert parse_oauth_catalog("{not json") == {}


def test_non_object_is_ignored() -> None:
    assert parse_oauth_catalog("[1, 2, 3]") == {}


def test_good_entry_parsed() -> None:
    raw = json.dumps({"notion": _b64({"type": "http", "url": "https://m.example.com/mcp"})})
    catalog = parse_oauth_catalog(raw)
    assert set(catalog) == {"notion"}
    assert catalog["notion"].url == "https://m.example.com/mcp"


def test_one_bad_entry_does_not_sink_the_rest() -> None:
    raw = json.dumps(
        {
            "good": _b64({"type": "http", "url": "https://m.example.com/mcp", "scopes": ["a"]}),
            "not_b64": "@@@not-base64@@@",
            "not_a_dict": base64.b64encode(b'"a string"').decode(),
            "wrong_type": _b64({"type": "stdio", "url": "https://m.example.com/mcp"}),
            "not_https": _b64({"type": "http", "url": "http://m.example.com/mcp"}),
            "non_string_value": 123,  # type: ignore[dict-item]
        }
    )
    catalog = parse_oauth_catalog(raw)
    assert set(catalog) == {"good"}
    assert catalog["good"].scopes == ["a"]
