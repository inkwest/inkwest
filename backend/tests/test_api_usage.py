"""Tests for self-service usage routes (/api/me/usage/*).

These are scoped to the current user and must be reachable by non-admins.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_my_usage_summary_allows_non_admin(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_result = MagicMock()
    mock_result.one.return_value = (5000, 3000, 0.12, 10)
    mock_db.execute = AsyncMock(return_value=mock_result)

    resp = await authenticated_client.get("/api/me/usage/summary")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_input_tokens"] == 5000
    assert data["total_output_tokens"] == 3000
    assert data["total_tokens"] == 8000
    assert data["request_count"] == 10
    assert data["period"] == "monthly"


@pytest.mark.asyncio
async def test_my_usage_summary_custom_range(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_result = MagicMock()
    mock_result.one.return_value = (0, 0, 0, 0)
    mock_db.execute = AsyncMock(return_value=mock_result)

    resp = await authenticated_client.get(
        "/api/me/usage/summary?start_date=2026-01-01&end_date=2026-01-31"
    )

    assert resp.status_code == 200
    assert resp.json()["period"] == "custom"


@pytest.mark.asyncio
async def test_my_usage_by_model_empty(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

    resp = await authenticated_client.get("/api/me/usage/by-model")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_my_usage_by_model_exposes_engine(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    # Contract: the per-model breakdown reports the engine that ran each turn under the
    # `engine` key (DB column is `provider`), matching the admin report and the ModelUsage type.
    row = SimpleNamespace(
        model="claude-opus-4-8",
        engine="claude-cli",
        input_tokens=100,
        output_tokens=40,
        cost_usd=0.5,
        request_count=3,
    )
    mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: [row]))

    resp = await authenticated_client.get("/api/me/usage/by-model")
    assert resp.status_code == 200
    data = resp.json()
    assert data == [
        {
            "model": "claude-opus-4-8",
            "engine": "claude-cli",
            "input_tokens": 100,
            "output_tokens": 40,
            "total_tokens": 140,
            "cost_usd": 0.5,
            "request_count": 3,
        }
    ]
    assert "provider" not in data[0]


@pytest.mark.asyncio
async def test_my_usage_sessions_empty(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

    resp = await authenticated_client.get("/api/me/usage/sessions")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_my_usage_sessions_rejects_oversized_limit(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/me/usage/sessions?limit=1001")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_my_usage_timeseries_empty(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

    resp = await authenticated_client.get("/api/me/usage/timeseries?granularity=day")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_my_usage_timeseries_rejects_bad_granularity(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/me/usage/timeseries?granularity=year")
    assert resp.status_code == 422
