"""Tests for the app config API route."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_app_config(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get("/api/config")

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"]
    assert data["model"]
    available_ids = [m["id"] for m in data["available_models"]]
    assert data["model"] in available_ids
    # Each entry carries a human-friendly label
    assert all(
        m["label"] and not m["label"].startswith("claude-") for m in data["available_models"]
    )


@pytest.mark.asyncio
async def test_app_config_reflects_model_settings(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import app_settings

    monkeypatch.setattr(app_settings, "_model_override", "claude-opus-4-20250514")
    monkeypatch.setattr(
        app_settings,
        "_enabled_models",
        ["claude-opus-4-20250514", "claude-sonnet-4-20250514"],
    )

    resp = await authenticated_client.get("/api/config")

    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "claude-opus-4-20250514"
    assert data["available_models"] == [
        {"id": "claude-opus-4-20250514", "label": "Opus 4.0", "legacy": True},
        {"id": "claude-sonnet-4-20250514", "label": "Sonnet 4.0", "legacy": True},
    ]
