"""Tests for the model registry: selection, pricing overrides, merge, CRUD."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import model_catalog, model_registry
from app.services.model_registry import (
    ModelEntry,
    calculate_cost,
    configured_default_id,
    default_model,
    merged_entries,
    selectable_models,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Isolate the in-memory registry + catalog cache between tests."""
    model_registry._rows = {}
    yield
    model_registry._rows = {}
    model_catalog._live_models = None


def _row(model_id: str, **overrides):
    base = {
        "model_id": model_id,
        "provider": "anthropic",
        "display_name": None,
        "endpoint": None,
        "arn": None,
        "options": {},
        "input_price": None,
        "output_price": None,
        "enabled": False,
        "is_default": False,
        "source": "discovered",
        "legacy": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selectable_unconfigured_is_all_available():
    model_catalog._live_models = ["claude-fable-5", "claude-opus-4-8"]
    assert selectable_models() == ["claude-fable-5", "claude-opus-4-8"]


def test_selectable_returns_only_enabled_when_configured():
    model_catalog._live_models = ["claude-fable-5", "claude-opus-4-8", "claude-haiku-4-5"]
    model_registry._rows = {"claude-opus-4-8": _row("claude-opus-4-8", enabled=True)}
    assert selectable_models() == ["claude-opus-4-8"]


def test_selectable_drops_stale_enabled_not_in_catalog():
    """A provider switch leaves an enabled row whose ID is gone from the catalog;
    it must not appear (merge only enables discovered/custom entries)."""
    model_catalog._live_models = ["anthropic.claude-x"]
    model_registry._rows = {"claude-opus-4-8": _row("claude-opus-4-8", enabled=True)}
    # No discovered model is enabled -> falls back to all available.
    assert selectable_models() == ["anthropic.claude-x"]


def test_default_model_honors_enabled_default():
    model_catalog._live_models = ["claude-fable-5", "claude-opus-4-8"]
    model_registry._rows = {
        "claude-opus-4-8": _row("claude-opus-4-8", enabled=True, is_default=True),
        "claude-fable-5": _row("claude-fable-5", enabled=True),
    }
    assert default_model() == "claude-opus-4-8"
    assert configured_default_id() == "claude-opus-4-8"


def test_default_model_unset_is_newest_available():
    model_catalog._live_models = None  # static fallback, newest first
    with patch.object(
        model_registry, "available_models", return_value=["claude-fable-5", "claude-opus-4-8"]
    ):
        assert default_model() == "claude-fable-5"
    assert configured_default_id() is None


def test_default_model_falls_back_to_newest_enabled():
    """A retired default falls back to the newest ENABLED model, not the newest
    catalog model (which the admin may have disabled)."""
    model_catalog._live_models = ["claude-fable-5", "claude-opus-4-8", "claude-haiku-4-5"]
    model_registry._rows = {"claude-haiku-4-5": _row("claude-haiku-4-5", enabled=True)}
    assert default_model() == "claude-haiku-4-5"


# ---------------------------------------------------------------------------
# Provider derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "model_id", "expected"),
    [
        ("anthropic", "claude-opus-4-8", "anthropic"),
        ("claude-cli", "claude-opus-4-8", "claude-cli"),
        ("bedrock", "eu.anthropic.claude-opus-4-8", "bedrock"),
        ("bedrock", "meta.llama3-1-70b-instruct-v1:0", "bedrock-open"),
    ],
)
def test_derive_provider(provider, model_id, expected):
    with patch.object(
        model_registry, "get_settings", return_value=SimpleNamespace(ai_provider=provider)
    ):
        assert model_registry._derive_provider(model_id) == expected


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_calculate_cost_uses_override():
    model_registry._rows = {
        "custom-x": _row("custom-x", input_price=Decimal("2.00"), output_price=Decimal("8.00"))
    }
    # 1M in + 1M out = 2.00 + 8.00
    assert calculate_cost("custom-x", 1_000_000, 1_000_000) == Decimal("10.00")


def test_calculate_cost_falls_back_to_static_table():
    # claude-opus-4-8 is in MODEL_PRICING at 5/25 per Mtok.
    assert calculate_cost("claude-opus-4-8", 1_000_000, 1_000_000) == Decimal("30.00")


def test_unpriced_model_flagged_in_merged_entry():
    model_catalog._live_models = ["totally-unknown-model"]
    entry = next(e for e in merged_entries() if e.model_id == "totally-unknown-model")
    assert entry.priced is False
    assert entry.input_price is None


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def test_merged_entries_includes_custom_not_in_catalog():
    model_catalog._live_models = ["claude-opus-4-8"]
    model_registry._rows = {
        "my-llama": _row(
            "my-llama",
            provider="bedrock-open",
            display_name="My Llama",
            source="custom",
            enabled=True,
        )
    }
    entries = merged_entries()
    ids = [e.model_id for e in entries]
    assert ids == ["claude-opus-4-8", "my-llama"]
    custom = next(e for e in entries if e.model_id == "my-llama")
    assert custom.label == "My Llama"
    assert custom.provider == "bedrock-open"
    assert custom.source == "custom"
    assert custom.priced is False  # no override, not in table -> red in UI


def test_merged_entry_label_override_and_priced_flag():
    model_catalog._live_models = ["claude-opus-4-8"]
    entries = merged_entries()
    opus = entries[0]
    assert isinstance(opus, ModelEntry)
    assert opus.priced is True
    assert opus.label == "Opus 4.8"
    assert opus.input_price == Decimal("5.00")


# ---------------------------------------------------------------------------
# CRUD (mocked DB)
# ---------------------------------------------------------------------------


def _mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_set_model_settings_mirrors_memory():
    model_catalog._live_models = ["claude-fable-5", "claude-opus-4-8", "claude-haiku-4-5"]
    db = _mock_db()
    await model_registry.set_model_settings(
        db, default="claude-opus-4-8", enabled=["claude-opus-4-8", "claude-haiku-4-5"]
    )
    db.commit.assert_awaited_once()
    assert selectable_models() == ["claude-opus-4-8", "claude-haiku-4-5"]
    assert default_model() == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_set_model_settings_rejects_default_not_enabled():
    db = _mock_db()
    with pytest.raises(ValueError, match="default model"):
        await model_registry.set_model_settings(db, default="a", enabled=["b"])
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_custom_model_then_reject_duplicate():
    db = _mock_db()
    await model_registry.add_custom_model(
        db,
        model_id="my-llama",
        provider="bedrock-open",
        input_price=Decimal("1.00"),
        output_price=Decimal("3.00"),
        enabled=True,
    )
    assert "my-llama" in model_registry._rows
    assert model_registry._rows["my-llama"]["input_price"] == Decimal("1.00")
    with pytest.raises(ValueError, match="already exists"):
        await model_registry.add_custom_model(db, model_id="my-llama", provider="bedrock-open")


@pytest.mark.asyncio
async def test_add_custom_model_rejects_lone_price():
    db = _mock_db()
    with pytest.raises(ValueError, match="set together"):
        await model_registry.add_custom_model(
            db, model_id="x", provider="bedrock-open", input_price=Decimal("1.00")
        )


@pytest.mark.asyncio
async def test_update_model_creates_row_for_discovered():
    model_catalog._live_models = ["claude-opus-4-8"]
    db = _mock_db()
    await model_registry.update_model(db, "claude-opus-4-8", display_name="Custom Name")
    assert model_registry._rows["claude-opus-4-8"]["display_name"] == "Custom Name"
    assert merged_entries()[0].label == "Custom Name"


@pytest.mark.asyncio
async def test_update_model_rejects_unknown_field():
    model_catalog._live_models = ["claude-opus-4-8"]
    db = _mock_db()
    with pytest.raises(ValueError, match="non-editable"):
        await model_registry.update_model(db, "claude-opus-4-8", provider="hacked")


@pytest.mark.asyncio
async def test_update_model_rejects_unknown_model():
    model_catalog._live_models = ["claude-opus-4-8"]
    db = _mock_db()
    with pytest.raises(ValueError, match="unknown model"):
        await model_registry.update_model(db, "ghost", enabled=True)


@pytest.mark.asyncio
async def test_set_model_settings_moves_default():
    model_catalog._live_models = ["claude-fable-5", "claude-opus-4-8"]
    model_registry._rows = {"claude-fable-5": _row("claude-fable-5", enabled=True, is_default=True)}
    db = _mock_db()
    await model_registry.set_model_settings(
        db, default="claude-opus-4-8", enabled=["claude-opus-4-8"]
    )
    assert configured_default_id() == "claude-opus-4-8"
    assert model_registry._rows["claude-fable-5"]["is_default"] is False
    assert model_registry._rows["claude-fable-5"]["enabled"] is False


@pytest.mark.asyncio
async def test_delete_custom_model():
    db = _mock_db()
    model_registry._rows = {"my-llama": _row("my-llama", source="custom")}
    await model_registry.delete_model(db, "my-llama")
    assert "my-llama" not in model_registry._rows


@pytest.mark.asyncio
async def test_delete_rejects_discovered_model():
    db = _mock_db()
    model_registry._rows = {"claude-opus-4-8": _row("claude-opus-4-8", source="discovered")}
    with pytest.raises(ValueError, match="cannot be deleted"):
        await model_registry.delete_model(db, "claude-opus-4-8")
    db.commit.assert_not_awaited()
