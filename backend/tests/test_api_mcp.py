"""Tests for the MCP catalog + per-session selection API routes."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_catalog_returns_keys(authenticated_client: AsyncClient) -> None:
    with patch("app.api.routes.mcp.catalog_keys", return_value=["coralogix", "gitlab"]):
        resp = await authenticated_client.get("/api/mcp/catalog")
    assert resp.status_code == 200
    assert resp.json() == {"keys": ["coralogix", "gitlab"]}


@pytest.mark.asyncio
async def test_get_session_mcp_returns_enabled(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    mock_session_obj.enabled_mcp_servers = ["coralogix"]
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/mcp")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": ["coralogix"]}


@pytest.mark.asyncio
async def test_set_session_mcp_persists_valid_keys(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)

    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=["coralogix", "gitlab"]),
        patch("app.services.mcp_config.apply_session_mcp_selection") as apply,
    ):
        resp = await authenticated_client.put(
            f"/api/sessions/{mock_session_obj.id}/mcp",
            json={"enabled": ["coralogix", "coralogix"]},  # dupes collapse
        )

    assert resp.status_code == 200
    assert resp.json() == {"enabled": ["coralogix"]}
    assert mock_session_obj.enabled_mcp_servers == ["coralogix"]
    apply.assert_called_once_with(str(mock_session_obj.id), ["coralogix"])


@pytest.mark.asyncio
async def test_set_session_mcp_rejects_unknown_key(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_session_obj: MagicMock
) -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)

    with (
        patch("app.services.mcp_catalog.catalog_keys", return_value=["coralogix"]),
        patch("app.services.mcp_config.apply_session_mcp_selection") as apply,
    ):
        resp = await authenticated_client.put(
            f"/api/sessions/{mock_session_obj.id}/mcp",
            json={"enabled": ["coralogix", "evil"]},
        )

    assert resp.status_code == 400
    assert "evil" in resp.json()["detail"]
    apply.assert_not_called()
