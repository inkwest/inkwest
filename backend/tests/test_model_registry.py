"""Tests for the model registry: selection, pricing overrides, merge, CRUD."""

from decimal import Decimal
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
    model_catalog._modalities = {}
    model_catalog._profile_arns = {}


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
    ("model_id", "expected"),
    [
        # Bare claude-* (no Bedrock namespace) → anthropic, so it routes to the CLI
        # (Messages protocol) and groups under Anthropic.
        ("claude-opus-4-8", "anthropic"),
        # Per-vendor tag derived from the (region-stripped) namespace.
        ("eu.anthropic.claude-opus-4-8", "anthropic"),
        ("anthropic.claude-3-5-sonnet-20241022-v2:0", "anthropic"),
        ("meta.llama3-1-70b-instruct-v1:0", "meta"),
        ("us.meta.llama3-1-70b-instruct-v1:0", "meta"),
        ("mistral.mistral-large-2407-v1:0", "mistral"),
        ("amazon.nova-pro-v1:0", "amazon"),
        ("cohere.command-r-v1:0", "cohere"),
        ("ai21.jamba-1-5-large-v1:0", "ai21"),
        ("deepseek.r1-v1:0", "deepseek"),
        ("stability.stable-image-core-v1:0", "stability"),
        # Vendor tag is derived verbatim from the namespace — NOT an allowlist — so
        # vendors Bedrock keeps adding group coherently instead of all under "bedrock".
        ("google.gemma-3-27b-it", "google"),
        ("nvidia.nemotron-nano-30b-3.0", "nvidia"),
        ("qwen.qwen3-coder-30b-a3b-v1:0", "qwen"),
        ("openai.gpt-oss-120b:0", "openai"),
        ("twelvelabs.marengo-embed-3-0-v1:0", "twelvelabs"),
        ("us.minimax.minimax-m2-5", "minimax"),
        # Bare, un-namespaced non-Claude ID (no dot) → generic "bedrock" tag
        # (model_catalog.bedrock_vendor's own fallback; no deployment provider anymore).
        ("some-bare-model", "bedrock"),
    ],
)
def test_derive_provider(model_id, expected):
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


def test_calculate_cost_override_derives_cache_prices_from_override_input():
    """Cache multipliers apply to the OVERRIDE's input price, not the table's."""
    model_registry._rows = {
        "custom-x": _row("custom-x", input_price=Decimal("2.00"), output_price=Decimal("8.00"))
    }
    # 1M cache-read at 0.1 x $2/M + 1M cache-write at 1.25 x $2/M = 0.20 + 2.50
    cost = calculate_cost(
        "custom-x", 0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000
    )
    assert cost == Decimal("2.70")


def test_calculate_cost_override_cache_write_1h_ttl():
    model_registry._rows = {
        "custom-x": _row("custom-x", input_price=Decimal("2.00"), output_price=Decimal("8.00"))
    }
    # 1h TTL writes bill 2x the override input price.
    cost = calculate_cost(
        "custom-x", 0, 0, cache_write_tokens=1_000_000, cache_write_1h_tokens=1_000_000
    )
    assert cost == Decimal("4.00")


def test_calculate_cost_no_override_passes_cache_to_table():
    # claude-opus-4-8: input $5/M -> 1M cache-read = $0.50.
    assert calculate_cost("claude-opus-4-8", 0, 0, cache_read_tokens=1_000_000) == Decimal("0.50")


def test_unpriced_model_flagged_in_merged_entry():
    model_catalog._live_models = ["totally-unknown-model"]
    entry = next(e for e in merged_entries() if e.model_id == "totally-unknown-model")
    assert entry.priced is False
    assert entry.input_price is None


def test_merged_entry_carries_modalities_from_catalog():
    """P1.8: _entry populates input/output modalities from the catalog capability map."""
    model_catalog._live_models = ["anthropic.claude-opus-4-v1:0"]
    model_catalog._modalities = {
        "anthropic.claude-opus-4-v1:0": {"input": ["TEXT", "IMAGE"], "output": ["TEXT"]}
    }
    entry = next(e for e in merged_entries() if e.model_id == "anthropic.claude-opus-4-v1:0")
    assert entry.input_modalities == ["TEXT", "IMAGE"]
    assert entry.output_modalities == ["TEXT"]


def test_merged_entry_modalities_empty_when_unknown():
    """A model with no live modality data → empty lists (back-compat / text-only)."""
    model_catalog._live_models = ["totally-unknown-model"]
    model_catalog._modalities = {}
    entry = next(e for e in merged_entries() if e.model_id == "totally-unknown-model")
    assert entry.input_modalities == []
    assert entry.output_modalities == []


def test_synthesized_entry_carries_profile_arn_as_invoke_target():
    """A discovered profile-only model (no DB row) gets its inference-profile
    ARN from the catalog as ``arn`` — the invoke target the orchestrator/agent reads."""
    model_catalog._live_models = ["eu.zai.glm5"]
    arn = "arn:aws:bedrock:eu-west-1:123:inference-profile/eu.zai.glm5"
    model_catalog._profile_arns = {"eu.zai.glm5": arn, "zai.glm5": arn}
    entry = next(e for e in merged_entries() if e.model_id == "eu.zai.glm5")
    assert entry.arn == arn


def test_explicit_row_arn_wins_over_profile_arn():
    """An admin/custom row arn is intent and beats the discovered profile ARN."""
    model_catalog._live_models = ["eu.zai.glm5"]
    model_catalog._profile_arns = {"eu.zai.glm5": "arn:profile"}
    model_registry._rows = {"eu.zai.glm5": _row("eu.zai.glm5", arn="arn:custom-override")}
    entry = next(e for e in merged_entries() if e.model_id == "eu.zai.glm5")
    assert entry.arn == "arn:custom-override"


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


def test_discovered_row_provider_is_rederived_not_stale():
    """A discovered row enabled before a derivation change keeps a stale stored provider;
    the merged entry must re-derive it (vendor), not trust the cached tag."""
    model_catalog._live_models = ["google.gemma-3-27b-it"]
    model_registry._rows = {
        # stale tag from when it was enabled under the old all-claude-cli derivation
        "google.gemma-3-27b-it": _row(
            "google.gemma-3-27b-it", provider="claude-cli", source="discovered", enabled=True
        )
    }
    entry = next(e for e in merged_entries() if e.model_id == "google.gemma-3-27b-it")
    assert entry.provider == "google"  # re-derived, not the stale "claude-cli"


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


# ---------------------------------------------------------------------------
# Invoke-target normalization (broker metering)
# ---------------------------------------------------------------------------


def test_resolve_invoke_target_exact_id():
    model_catalog._live_models = ["claude-opus-4-8"]
    assert model_registry.resolve_invoke_target("claude-opus-4-8") == "claude-opus-4-8"


def test_resolve_invoke_target_profile_id_and_arn():
    arn = "arn:aws:bedrock:eu-west-1:123:inference-profile/eu.anthropic.claude-opus-4-8"
    model_catalog._live_models = ["anthropic.claude-opus-4-8"]
    model_catalog._profile_arns = {
        "eu.anthropic.claude-opus-4-8": arn,
        "anthropic.claude-opus-4-8": arn,
    }
    model_registry._rows = {
        "anthropic.claude-opus-4-8": _row("anthropic.claude-opus-4-8", enabled=True, arn=arn)
    }
    # The engine may invoke via the profile id or the ARN — both map back to the entry.
    assert (
        model_registry.resolve_invoke_target("eu.anthropic.claude-opus-4-8")
        == "anthropic.claude-opus-4-8"
    )
    assert model_registry.resolve_invoke_target(arn) == "anthropic.claude-opus-4-8"


def test_resolve_invoke_target_route_prefix_normalization():
    """No arn/profile row, but the route-prefixed id normalizes to a known entry."""
    model_catalog._live_models = ["anthropic.claude-opus-4-8"]
    assert (
        model_registry.resolve_invoke_target("eu.anthropic.claude-opus-4-8")
        == "anthropic.claude-opus-4-8"
    )


def test_resolve_invoke_target_unknown_verbatim():
    model_catalog._live_models = ["claude-opus-4-8"]
    assert model_registry.resolve_invoke_target("weird-target") == "weird-target"


def test_resolve_invoke_target_covers_every_enabled_entry():
    """Handoff open-point 2: normalization must hit for every enabled registry entry —
    its id, its ARN, and every profile id of that ARN all resolve to the entry."""
    arn_a = "arn:aws:bedrock:eu-west-1:123:inference-profile/eu.anthropic.claude-opus-4-8"
    arn_b = "arn:aws:bedrock:eu-west-1:123:inference-profile/eu.meta.llama4-x"
    model_catalog._live_models = ["anthropic.claude-opus-4-8", "meta.llama4-x"]
    model_catalog._profile_arns = {
        "eu.anthropic.claude-opus-4-8": arn_a,
        "eu.meta.llama4-x": arn_b,
    }
    model_registry._rows = {
        "anthropic.claude-opus-4-8": _row("anthropic.claude-opus-4-8", enabled=True, arn=arn_a),
        "meta.llama4-x": _row("meta.llama4-x", enabled=True, arn=arn_b),
    }
    for entry in model_registry.merged_entries():
        if not entry.enabled:
            continue
        assert model_registry.resolve_invoke_target(entry.model_id) == entry.model_id
        if entry.arn:
            assert model_registry.resolve_invoke_target(entry.arn) == entry.model_id
            for pid in model_catalog.profile_ids_for_arn(entry.arn):
                assert model_registry.resolve_invoke_target(pid) == entry.model_id
