"""
Tests for the pricing service.
"""

from decimal import Decimal

import pytest

from app.services.pricing import (
    ModelPricing,
    calculate_cost,
    cost_for_pricing,
    estimate_cost,
    get_model_pricing,
)


class TestGetModelPricing:
    """Tests for get_model_pricing function."""

    def test_exact_match_anthropic(self):
        """Should return exact pricing for known Anthropic models."""
        pricing = get_model_pricing("claude-sonnet-4-20250514")
        assert pricing.input_price == Decimal("3.00")
        assert pricing.output_price == Decimal("15.00")

    def test_exact_match_opus(self):
        """Should return exact pricing for Opus model."""
        pricing = get_model_pricing("claude-opus-4-20250514")
        assert pricing.input_price == Decimal("15.00")
        assert pricing.output_price == Decimal("75.00")

    def test_partial_match_bedrock(self):
        """Should match Bedrock model IDs."""
        pricing = get_model_pricing("anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert pricing.input_price == Decimal("3.00")

    def test_unknown_model_returns_default(self):
        """Should return default pricing for unknown models."""
        pricing = get_model_pricing("unknown-model-xyz")
        assert pricing.input_price == Decimal("5.00")
        assert pricing.output_price == Decimal("15.00")

    def test_open_weight_without_live_prices_is_default(self):
        """Open-weight models aren't in the static table → DEFAULT (priced=False) until the
        live Price List resolves them."""
        from app.services import pricing as pricing_mod

        pricing_mod.set_live_pricing({})  # ensure no live map
        p = get_model_pricing("mistral.magistral-small-2509")
        assert p.input_price == Decimal("5.00")  # DEFAULT_PRICING

    def test_live_pricing_resolves_across_naming_styles(self):
        """Live prices (keyed by price_match_keys) resolve a routed/versioned model ID."""
        from app.services import pricing as pricing_mod
        from app.services.pricing import ModelPricing, price_match_keys

        # Build a live map the way pricing_live does: index by every key of the token.
        live: dict = {}
        for key in price_match_keys("Magistral-Small-2509"):
            live[key] = ModelPricing(Decimal("0.50"), Decimal("1.50"))
        for key in price_match_keys("NovaLite"):
            live[key] = ModelPricing(Decimal("0.06"), Decimal("0.24"))
        pricing_mod.set_live_pricing(live)
        try:
            assert get_model_pricing("mistral.magistral-small-2509").input_price == Decimal("0.50")
            assert get_model_pricing("eu.amazon.nova-lite-v1:0").output_price == Decimal("0.24")
            # live wins over the static table for Claude too, if present
            assert get_model_pricing("unknown.vendor-x").input_price == Decimal("5.00")  # default
        finally:
            pricing_mod.set_live_pricing({})


class TestCalculateCost:
    """Tests for calculate_cost function."""

    def test_zero_tokens(self):
        """Should return zero cost for zero tokens."""
        cost = calculate_cost("claude-sonnet-4-20250514", 0, 0)
        assert cost == Decimal("0")

    def test_input_tokens_only(self):
        """Should calculate cost for input tokens only."""
        # 1M input tokens at $3/M = $3
        cost = calculate_cost("claude-sonnet-4-20250514", 1_000_000, 0)
        assert cost == Decimal("3.00")

    def test_output_tokens_only(self):
        """Should calculate cost for output tokens only."""
        # 1M output tokens at $15/M = $15
        cost = calculate_cost("claude-sonnet-4-20250514", 0, 1_000_000)
        assert cost == Decimal("15.00")

    def test_mixed_tokens(self):
        """Should calculate cost for mixed input and output."""
        # 500K input at $3/M = $1.50
        # 100K output at $15/M = $1.50
        # Total = $3.00
        cost = calculate_cost("claude-sonnet-4-20250514", 500_000, 100_000)
        assert cost == Decimal("3.00")

    def test_small_token_counts(self):
        """Should handle small token counts with precision."""
        # 1K input at $3/M = $0.003
        # 1K output at $15/M = $0.015
        # Total = $0.018
        cost = calculate_cost("claude-sonnet-4-20250514", 1000, 1000)
        assert cost == Decimal("0.018")

    def test_opus_pricing(self):
        """Should use Opus pricing (more expensive)."""
        # 1M input at $15/M = $15
        cost = calculate_cost("claude-opus-4-20250514", 1_000_000, 0)
        assert cost == Decimal("15.00")


class TestCacheAwareCost:
    """Prompt-cache-aware cost: reads 0.1x, 5m writes 1.25x, 1h writes 2x input rate."""

    # claude-sonnet-4-20250514: input $3/M, output $15/M.

    def test_cache_read_billed_at_tenth_of_input(self):
        # 1M cache-read at 0.1 x $3/M = $0.30
        cost = calculate_cost("claude-sonnet-4-20250514", 0, 0, cache_read_tokens=1_000_000)
        assert cost == Decimal("0.30")

    def test_cache_write_billed_at_1_25x_input(self):
        # 1M cache-write (5m TTL) at 1.25 x $3/M = $3.75
        cost = calculate_cost("claude-sonnet-4-20250514", 0, 0, cache_write_tokens=1_000_000)
        assert cost == Decimal("3.75")

    def test_cache_write_1h_billed_at_2x_input(self):
        # All writes at 1h TTL: 1M at 2 x $3/M = $6
        cost = calculate_cost(
            "claude-sonnet-4-20250514",
            0,
            0,
            cache_write_tokens=1_000_000,
            cache_write_1h_tokens=1_000_000,
        )
        assert cost == Decimal("6.00")

    def test_cache_write_ttl_split(self):
        # 600k at 5m (1.25 x 3 = 3.75/M -> 2.25) + 400k at 1h (2 x 3 = 6/M -> 2.40)
        cost = calculate_cost(
            "claude-sonnet-4-20250514",
            0,
            0,
            cache_write_tokens=1_000_000,
            cache_write_1h_tokens=400_000,
        )
        assert cost == Decimal("4.65")

    def test_full_four_way_split(self):
        # Real-shaped turn: fresh 7, out 16, write 12608 — the captured converse turn.
        cost = calculate_cost(
            "claude-sonnet-4-20250514",
            7,
            16,
            cache_write_tokens=12_608,
        )
        expected = (
            Decimal(7) * Decimal("3") / Decimal(1_000_000)
            + Decimal(16) * Decimal("15") / Decimal(1_000_000)
            + Decimal(12_608) * Decimal("3.75") / Decimal(1_000_000)
        )
        assert cost == expected

    def test_1h_portion_clamped_to_total_writes(self):
        # A reported 1h portion larger than the total never over-bills.
        cost = calculate_cost(
            "claude-sonnet-4-20250514",
            0,
            0,
            cache_write_tokens=100_000,
            cache_write_1h_tokens=500_000,
        )
        assert cost == Decimal("0.60")  # all 100k at 2x rate ($6/M)

    def test_defaults_keep_legacy_behavior(self):
        # No cache args = the pre-cache cost, byte-identical.
        assert calculate_cost("claude-sonnet-4-20250514", 500_000, 100_000) == Decimal("3.00")

    def test_explicit_cache_prices_win_over_multipliers(self):
        pricing = ModelPricing(
            input_price=Decimal("3.00"),
            output_price=Decimal("15.00"),
            cache_read_price=Decimal("0.50"),
            cache_write_price=Decimal("4.00"),
        )
        cost = cost_for_pricing(
            pricing, 0, 0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000
        )
        assert cost == Decimal("4.50")

    def test_explicit_write_price_scales_1h_by_premium_ratio(self):
        # Explicit 5m write $4/M -> 1h at 4 x (2/1.25) = $6.40/M.
        pricing = ModelPricing(
            input_price=Decimal("3.00"),
            output_price=Decimal("15.00"),
            cache_write_price=Decimal("4.00"),
        )
        cost = cost_for_pricing(
            pricing,
            0,
            0,
            cache_write_tokens=1_000_000,
            cache_write_1h_tokens=1_000_000,
        )
        assert cost == Decimal("6.40")


class TestEstimateCost:
    """Tests for estimate_cost function."""

    def test_estimate_returns_dict(self):
        """Should return a dict with cost breakdown."""
        estimate = estimate_cost("claude-sonnet-4-20250514", 10000, 5000)

        assert "model" in estimate
        assert "input_tokens" in estimate
        assert "output_tokens" in estimate
        assert "input_price_per_million" in estimate
        assert "output_price_per_million" in estimate
        assert "estimated_cost_usd" in estimate
        assert "estimated_cost_cents" in estimate

    def test_estimate_values(self):
        """Should return correct estimate values."""
        estimate = estimate_cost("claude-sonnet-4-20250514", 100_000, 50_000)

        assert estimate["model"] == "claude-sonnet-4-20250514"
        assert estimate["input_tokens"] == 100_000
        assert estimate["output_tokens"] == 50_000
        assert estimate["input_price_per_million"] == 3.00
        assert estimate["output_price_per_million"] == 15.00
        # 100K input = $0.30, 50K output = $0.75, total = $1.05
        assert estimate["estimated_cost_usd"] == pytest.approx(1.05, rel=0.001)
        assert estimate["estimated_cost_cents"] == 105
