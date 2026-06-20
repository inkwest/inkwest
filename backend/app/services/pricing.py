"""
Model pricing calculations.

Tracks cost per token for different AI models and providers.
"""

import re
from decimal import Decimal
from typing import NamedTuple


class ModelPricing(NamedTuple):
    """Pricing for a model (per million tokens)."""

    input_price: Decimal  # USD per 1M input tokens
    output_price: Decimal  # USD per 1M output tokens
    # Older generation: kept for cost reporting on past usage, flagged in the
    # admin UI so admins know not to enable it for users.
    legacy: bool = False


# Pricing as of June 2026 (USD per million tokens). Dict order = display order
# in the model selectors (newest first).
MODEL_PRICING: dict[str, ModelPricing] = {
    # Current Anthropic Claude models
    "claude-fable-5": ModelPricing(
        input_price=Decimal("10.00"),
        output_price=Decimal("50.00"),
    ),
    "claude-opus-4-8": ModelPricing(
        input_price=Decimal("5.00"),
        output_price=Decimal("25.00"),
    ),
    "claude-opus-4-7": ModelPricing(
        input_price=Decimal("5.00"),
        output_price=Decimal("25.00"),
    ),
    "claude-opus-4-6": ModelPricing(
        input_price=Decimal("5.00"),
        output_price=Decimal("25.00"),
    ),
    "claude-sonnet-4-6": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
    ),
    "claude-opus-4-5": ModelPricing(
        input_price=Decimal("5.00"),
        output_price=Decimal("25.00"),
    ),
    "claude-sonnet-4-5": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
    ),
    "claude-haiku-4-5": ModelPricing(
        input_price=Decimal("1.00"),
        output_price=Decimal("5.00"),
    ),
    # Older Anthropic Claude models (kept for cost reporting on past usage;
    # uncheck them in the admin Model screen to hide from users)
    "claude-opus-4-20250514": ModelPricing(
        input_price=Decimal("15.00"),
        output_price=Decimal("75.00"),
        legacy=True,
    ),
    "claude-sonnet-4-20250514": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
        legacy=True,
    ),
    "claude-3-5-sonnet-20241022": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
        legacy=True,
    ),
    "claude-3-opus-20240229": ModelPricing(
        input_price=Decimal("15.00"),
        output_price=Decimal("75.00"),
        legacy=True,
    ),
    "claude-3-sonnet-20240229": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
        legacy=True,
    ),
    "claude-3-haiku-20240307": ModelPricing(
        input_price=Decimal("0.25"),
        output_price=Decimal("1.25"),
        legacy=True,
    ),
    # Bedrock Claude models (same pricing)
    "anthropic.claude-3-5-sonnet-20241022-v2:0": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
        legacy=True,
    ),
    "anthropic.claude-3-opus-20240229-v1:0": ModelPricing(
        input_price=Decimal("15.00"),
        output_price=Decimal("75.00"),
        legacy=True,
    ),
}

# Default pricing for unknown models (conservative estimate)
DEFAULT_PRICING = ModelPricing(
    input_price=Decimal("5.00"),
    output_price=Decimal("15.00"),
)


def model_label(model_id: str) -> str:
    """Human-friendly display name for a model ID.

    Derived, not hardcoded, so any catalog entry gets a sensible label:
    "claude-3-5-sonnet-20241022" -> "Sonnet 3.5",
    "claude-opus-4-20250514" -> "Opus 4.0",
    "eu.anthropic.claude-opus-4-8" -> "Opus 4.8",
    "anthropic.claude-3-5-sonnet-20241022-v2:0" -> "Sonnet 3.5".
    """
    name = re.sub(r"^(?:us|eu|apac|us-gov|global)\.", "", model_id)  # Bedrock routing profiles
    name = name.removeprefix("anthropic.").removeprefix("claude-")
    name = re.sub(r"-\d{8}.*$", "", name)  # drop date snapshot and Bedrock suffix
    name = re.sub(r"-v\d+(:\d+)?$", "", name)  # undated Bedrock version suffix (-v1:0)
    tokens = name.split("-")
    family = " ".join(t.capitalize() for t in tokens if not t.isdigit())
    numbers = [t for t in tokens if t.isdigit()]
    if len(numbers) == 1:
        numbers.append("0")  # major-only IDs: show an explicit minor ("Opus 4.0")
    version = ".".join(numbers)
    return f"{family} {version}".strip() or model_id


def is_legacy_model(model_id: str) -> bool:
    """Whether a model belongs to an older generation (per the catalog).

    Unknown IDs (e.g. an env-configured default outside the table) are not
    flagged — exact match only, no fuzzy lookup.
    """
    pricing = MODEL_PRICING.get(model_id)
    return pricing.legacy if pricing else False


_ROUTE_PREFIX_RE = re.compile(r"^(?:us|eu|apac|us-gov|global)\.")


def _pricing_candidates(model: str) -> list[str]:
    """Progressively normalized forms of a (possibly Bedrock-routed) model ID.

    eu.anthropic.claude-haiku-4-5-20251001-v1:0 ->
    anthropic.claude-haiku-4-5-20251001-v1:0 -> anthropic.claude-haiku-4-5-20251001
    -> claude-haiku-4-5-20251001 -> claude-haiku-4-5
    """
    base = _ROUTE_PREFIX_RE.sub("", model)
    no_version = re.sub(r"-v\d+(:\d+)?$", "", base)
    bare = no_version.removeprefix("anthropic.")
    return [model, base, no_version, bare, re.sub(r"-\d{8}$", "", bare)]


_ALNUM_RE = re.compile(r"[^a-z0-9]")


def price_match_keys(token: str) -> set[str]:
    """Normalized keys for matching a model token across naming styles.

    Bridges the AWS Price List's loose model tokens (from a product's ``usagetype``,
    e.g. ``Magistral-Small-2509``, ``NovaLite``, ``nvidia.nemotron-super-3-120b``) to
    real Bedrock model IDs (``mistral.magistral-small-2509``, ``amazon.nova-lite-v1:0``).
    Strips the regional routing prefix + version suffix, lowercases to alphanumerics, and
    also yields the form **without** the leading vendor namespace — so the price side and
    the model-ID side meet on a common key regardless of which carries the vendor prefix.
    """
    token = _ROUTE_PREFIX_RE.sub("", token)
    token = re.sub(r"-v\d+(:\d+)?$", "", token)  # -v1:0
    token = re.sub(r":\d+$", "", token)  # bare :0 version
    keys = {_ALNUM_RE.sub("", token.lower())}
    if "." in token:
        keys.add(_ALNUM_RE.sub("", token.split(".", 1)[1].lower()))
    return {k for k in keys if k}


# Live prices resolved from the AWS public bulk Price List at catalog-refresh time
# (see app.services.pricing_live), keyed by every ``price_match_keys`` of the price-list
# model token. Consulted before the static table so accurate per-vendor prices need no
# hardcoding (and don't rot). Empty until the first successful refresh.
_LIVE_PRICING: dict[str, ModelPricing] = {}


def set_live_pricing(by_key: dict[str, ModelPricing]) -> None:
    """Replace the live price map (called after a price-list refresh; ``{}`` clears it)."""
    global _LIVE_PRICING
    _LIVE_PRICING = by_key


def _live_pricing(model: str) -> ModelPricing | None:
    if not _LIVE_PRICING:
        return None
    for key in price_match_keys(model):
        hit = _LIVE_PRICING.get(key)
        if hit is not None:
            return hit
    return None


def get_model_pricing(model: str) -> ModelPricing:
    """
    Get pricing for a model.

    Resolution order: live Price-List prices (accurate, refreshed) → the static table
    (exact ID then progressively normalized forms — Bedrock routing prefix, version
    suffix, date snapshot stripped; never substring) → the conservative default.

    Args:
        model: Model name or ID

    Returns:
        ModelPricing with input and output prices
    """
    live = _live_pricing(model)
    if live is not None:
        return live
    for candidate in _pricing_candidates(model):
        if candidate in MODEL_PRICING:
            return MODEL_PRICING[candidate]
    return DEFAULT_PRICING


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    """
    Calculate cost for token usage.

    Args:
        model: Model name or ID
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens

    Returns:
        Cost in USD
    """
    pricing = get_model_pricing(model)

    # Convert to millions and calculate
    input_cost = (Decimal(input_tokens) / Decimal(1_000_000)) * pricing.input_price
    output_cost = (Decimal(output_tokens) / Decimal(1_000_000)) * pricing.output_price

    return input_cost + output_cost


def estimate_cost(
    model: str,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> dict[str, object]:
    """
    Estimate cost for a request.

    Args:
        model: Model name
        estimated_input_tokens: Estimated input tokens
        estimated_output_tokens: Estimated output tokens

    Returns:
        Dict with cost breakdown
    """
    pricing = get_model_pricing(model)
    total_cost = calculate_cost(model, estimated_input_tokens, estimated_output_tokens)

    return {
        "model": model,
        "input_tokens": estimated_input_tokens,
        "output_tokens": estimated_output_tokens,
        "input_price_per_million": float(pricing.input_price),
        "output_price_per_million": float(pricing.output_price),
        "estimated_cost_usd": float(total_cost),
        "estimated_cost_cents": int(total_cost * 100),
    }
