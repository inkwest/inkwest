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
    from app.services import model_catalog, model_registry

    def _row(model_id: str, **over):
        base = {
            "model_id": model_id,
            "provider": "anthropic",
            "display_name": None,
            "endpoint": None,
            "arn": None,
            "options": {},
            "input_price": None,
            "output_price": None,
            "enabled": True,
            "is_default": False,
            "source": "discovered",
            "legacy": False,
        }
        base.update(over)
        return base

    monkeypatch.setattr(
        model_catalog, "_live_models", ["claude-opus-4-20250514", "claude-sonnet-4-20250514"]
    )
    monkeypatch.setattr(
        model_registry,
        "_rows",
        {
            "claude-opus-4-20250514": _row("claude-opus-4-20250514", is_default=True),
            "claude-sonnet-4-20250514": _row("claude-sonnet-4-20250514"),
        },
    )

    resp = await authenticated_client.get("/api/config")

    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "claude-opus-4-20250514"
    # The test deployment enables both engines, so a Claude model is runnable on each
    # (claude-cli native + inkwest-agent via Converse) — the picker can pair Engine·Model.
    assert data["engines"] == ["claude-cli", "inkwest-agent"]
    # Default (Opus, a Claude model) resolves to the native CLI when both engines are on.
    assert data["default_engine"] == "claude-cli"
    both = ["claude-cli", "inkwest-agent"]
    assert data["available_models"] == [
        {"id": "claude-opus-4-20250514", "label": "Opus 4.0", "legacy": True, "engines": both},
        {"id": "claude-sonnet-4-20250514", "label": "Sonnet 4.0", "legacy": True, "engines": both},
    ]
