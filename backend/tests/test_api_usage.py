"""Tests for self-service usage routes (/api/me/usage/*).

These are scoped to the current user and must be reachable by non-admins.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_my_usage_summary_allows_non_admin(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_result = MagicMock()
    # Distinct sentinels: cache_read must NOT equal total_tokens (5000+3000) or a
    # column mix-up would pass unnoticed (same discipline as the admin twin).
    mock_result.one.return_value = SimpleNamespace(
        input_tokens=5000,
        output_tokens=3000,
        cache_read_tokens=7777,
        cache_write_tokens=2000,
        cost_usd=0.12,
        request_count=10,
    )
    mock_db.execute = AsyncMock(return_value=mock_result)

    resp = await authenticated_client.get("/api/me/usage/summary")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_input_tokens"] == 5000
    assert data["total_output_tokens"] == 3000
    assert data["total_tokens"] == 8000
    assert data["total_cache_read_tokens"] == 7777
    assert data["total_cache_write_tokens"] == 2000
    assert data["request_count"] == 10
    assert data["period"] == "monthly"


@pytest.mark.asyncio
async def test_my_usage_summary_custom_range(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_result = MagicMock()
    mock_result.one.return_value = SimpleNamespace(
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=0,
        request_count=0,
    )
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
        cache_read_tokens=800,
        cache_write_tokens=200,
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
            "cache_read_tokens": 800,
            "cache_write_tokens": 200,
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


@pytest.mark.asyncio
async def test_my_usage_turns_rollup(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    """Per-turn rollup: N broker calls of one turn aggregate under its turn_id."""
    turn = uuid.uuid4()
    sess = uuid.uuid4()
    row = SimpleNamespace(
        turn_key=turn,
        session_id=sess,
        model="anthropic.claude-opus-4-8",
        engine="claude-cli",
        source="broker",
        input_tokens=30,
        output_tokens=1251,
        cache_read_tokens=27082,
        cache_write_tokens=30039,
        cost_usd=0.42,
        call_count=3,
        last_call=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
    )
    mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: [row]))

    resp = await authenticated_client.get(f"/api/me/usage/turns?session_id={sess}")
    assert resp.status_code == 200
    data = resp.json()
    assert data == [
        {
            "turn_id": str(turn),
            "session_id": str(sess),
            "model": "anthropic.claude-opus-4-8",
            "engine": "claude-cli",
            "source": "broker",
            "input_tokens": 30,
            "output_tokens": 1251,
            "cache_read_tokens": 27082,
            "cache_write_tokens": 30039,
            "cost_usd": 0.42,
            "call_count": 3,
            "last_call": "2026-07-04T12:00:00+00:00",
        }
    ]
