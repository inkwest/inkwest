"""Unit tests for backend-side MCP tool enumeration (app.services.mcp_tools)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.services import mcp_tools
from app.services.mcp_oauth import McpOauthError


def _tool(name: str, desc: str | None = "", schema: object = None) -> SimpleNamespace:
    """A stand-in for the mcp SDK's Tool (name / description / inputSchema)."""
    return SimpleNamespace(name=name, description=desc, inputSchema=schema)


def test_normalize_maps_fields_and_defaults() -> None:
    out = mcp_tools._normalize(_tool("query", "Run a query", {"type": "object"}))
    assert out == {
        "name": "query",
        "description": "Run a query",
        "input_schema": {"type": "object"},
        "annotations": None,  # no annotations on the stub → None
    }


def test_normalize_coerces_missing_description_and_schema() -> None:
    # description None -> "", non-dict schema -> {} (never leak a None the UI must special-case).
    out = mcp_tools._normalize(_tool("t", None, None))
    assert out == {"name": "t", "description": "", "input_schema": {}, "annotations": None}


def test_normalize_captures_annotations() -> None:
    from types import SimpleNamespace

    tool = SimpleNamespace(
        name="get_x",
        description="",
        inputSchema={},
        annotations=SimpleNamespace(
            title="Get X",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=None,
            openWorldHint=None,
        ),
    )
    out = mcp_tools._normalize(tool)
    assert out["annotations"] == {
        "title": "Get X",
        "read_only": True,
        "destructive": False,
        "idempotent": None,
        "open_world": None,
    }


@pytest.mark.asyncio
async def test_resolve_upstream_oauth_injects_bearer() -> None:
    db = AsyncMock()
    with (
        patch(
            "app.services.mcp_tools.get_oauth_server",
            return_value=SimpleNamespace(url="https://as.example.com/mcp"),
        ),
        patch("app.services.mcp_tools.mcp_oauth.get_fresh_token", AsyncMock(return_value="tok")),
    ):
        url, headers = await mcp_tools._resolve_upstream(db, "notion", uuid4())
    assert url == "https://as.example.com/mcp"
    assert headers == {"Authorization": "Bearer tok"}


@pytest.mark.asyncio
async def test_resolve_upstream_static_uses_catalog_headers() -> None:
    db = AsyncMock()
    with (
        patch("app.services.mcp_tools.get_oauth_server", return_value=None),
        patch(
            "app.services.mcp_tools.get_static_http_server",
            return_value={"url": "https://mcp.example.com/", "headers": {"X-API-Key": "k"}},
        ),
    ):
        url, headers = await mcp_tools._resolve_upstream(db, "coralogix", uuid4())
    assert url == "https://mcp.example.com/"
    assert headers == {"X-API-Key": "k"}


@pytest.mark.asyncio
async def test_resolve_upstream_unknown_key_raises() -> None:
    db = AsyncMock()
    with (
        patch("app.services.mcp_tools.get_oauth_server", return_value=None),
        patch("app.services.mcp_tools.get_static_http_server", return_value=None),
    ):
        with pytest.raises(mcp_tools.McpEnumError):
            await mcp_tools._resolve_upstream(db, "nope", uuid4())


@pytest.mark.asyncio
async def test_resolve_upstream_dead_grant_maps_to_enum_error() -> None:
    db = AsyncMock()
    with (
        patch(
            "app.services.mcp_tools.get_oauth_server",
            return_value=SimpleNamespace(url="https://as.example.com/mcp"),
        ),
        patch(
            "app.services.mcp_tools.mcp_oauth.get_fresh_token",
            AsyncMock(side_effect=McpOauthError("dead")),
        ),
    ):
        with pytest.raises(mcp_tools.McpEnumError):
            await mcp_tools._resolve_upstream(db, "notion", uuid4())


@pytest.mark.asyncio
async def test_enumerate_tools_success() -> None:
    db = AsyncMock()
    tools = [{"name": "a", "description": "", "input_schema": {}}]
    with (
        patch(
            "app.services.mcp_tools._resolve_upstream",
            AsyncMock(return_value=("https://mcp.example.com/", {})),
        ),
        patch("app.services.mcp_tools.assert_public_https", return_value=None),
        patch("app.services.mcp_tools._list_tools", AsyncMock(return_value=tools)),
    ):
        out = await mcp_tools.enumerate_tools(db, key="coralogix", user_id=uuid4())
    assert out == tools


@pytest.mark.asyncio
async def test_enumerate_tools_url_rejected() -> None:
    db = AsyncMock()
    with (
        patch(
            "app.services.mcp_tools._resolve_upstream",
            AsyncMock(return_value=("http://169.254.169.254/", {})),
        ),
        patch(
            "app.services.mcp_tools.assert_public_https",
            side_effect=McpOauthError("private"),
        ),
    ):
        with pytest.raises(mcp_tools.McpEnumError):
            await mcp_tools.enumerate_tools(db, key="evil", user_id=uuid4())


@pytest.mark.asyncio
async def test_enumerate_tools_transport_error_wrapped() -> None:
    db = AsyncMock()
    with (
        patch(
            "app.services.mcp_tools._resolve_upstream",
            AsyncMock(return_value=("https://mcp.example.com/", {})),
        ),
        patch("app.services.mcp_tools.assert_public_https", return_value=None),
        patch(
            "app.services.mcp_tools._list_tools",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        ),
    ):
        with pytest.raises(mcp_tools.McpEnumError):
            await mcp_tools.enumerate_tools(db, key="coralogix", user_id=uuid4())


@pytest.mark.asyncio
async def test_refresh_tools_enumerates_then_upserts_and_commits() -> None:
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    tools = [{"name": "a", "description": "", "input_schema": {}}]
    with patch("app.services.mcp_tools.enumerate_tools", AsyncMock(return_value=tools)):
        out = await mcp_tools.refresh_tools(db, key="coralogix", user_id=uuid4())
    assert out == tools
    db.execute.assert_awaited_once()  # the upsert
    db.commit.assert_awaited_once()
