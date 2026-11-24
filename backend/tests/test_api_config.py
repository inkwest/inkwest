"""Tests for the app config API route."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_get_app_config(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get("/api/config")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["engines"], list) and data["engines"]
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
    # input_modalities is empty here: these are static-catalog Claude models with no live
    # Bedrock modality data (P1.8). Populated only when the live catalog supplies it.
    assert data["available_models"] == [
        {
            "id": "claude-opus-4-20250514",
            "label": "Opus 4.0",
            "legacy": True,
            "engines": both,
            "input_modalities": [],
            # Older Claude generations have no published context window, so the gauge
            # stays off for them rather than assuming one (see context_window.py).
            "context_window": None,
        },
        {
            "id": "claude-sonnet-4-20250514",
            "label": "Sonnet 4.0",
            "legacy": True,
            "engines": both,
            "input_modalities": [],
            # Older Claude generations have no published context window, so the gauge
            # stays off for them rather than assuming one (see context_window.py).
            "context_window": None,
        },
    ]


@pytest.mark.asyncio
async def test_app_config_ships_the_resolved_context_window(
    authenticated_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gauge and the compact offer are both driven by this field: a deployment where it
    is null everywhere silently loses the whole feature, with every other test still green."""
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

    monkeypatch.setattr(model_catalog, "_live_models", ["claude-sonnet-5", "custom-model"])
    monkeypatch.setattr(
        model_registry,
        "_rows",
        {
            "claude-sonnet-5": _row("claude-sonnet-5", is_default=True),
            # The registry override is the escape hatch for anything the table can't source.
            "custom-model": _row("custom-model", options={"context_window": 500_000}),
        },
    )

    data = (await authenticated_client.get("/api/config")).json()
    windows = {m["id"]: m["context_window"] for m in data["available_models"]}
    assert windows["claude-sonnet-5"] == 1_000_000  # from the sourced static table
    assert windows["custom-model"] == 500_000  # registry options win
