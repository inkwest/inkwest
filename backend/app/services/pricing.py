"""
Model pricing calculations.

Tracks cost per token for different AI models and providers.
"""

from decimal import Decimal
from typing import NamedTuple

from app.core.config import get_settings


class ModelPricing(NamedTuple):
    """Pricing for a model (per million tokens)."""

    input_price: Decimal  # USD per 1M input tokens
    output_price: Decimal  # USD per 1M output tokens


# Pricing as of January 2026 (USD per million tokens)
MODEL_PRICING: dict[str, ModelPricing] = {
    # Anthropic Claude models
    "claude-opus-4-20250514": ModelPricing(
        input_price=Decimal("15.00"),
        output_price=Decimal("75.00"),
    ),
    "claude-sonnet-4-20250514": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
    ),
    "claude-3-5-sonnet-20241022": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
    ),
    "claude-3-opus-20240229": ModelPricing(
        input_price=Decimal("15.00"),
        output_price=Decimal("75.00"),
    ),
    "claude-3-sonnet-20240229": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
    ),
    "claude-3-haiku-20240307": ModelPricing(
        input_price=Decimal("0.25"),
        output_price=Decimal("1.25"),
    ),
    # Bedrock Claude models (same pricing)
    "anthropic.claude-3-5-sonnet-20241022-v2:0": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
    ),
    "anthropic.claude-3-opus-20240229-v1:0": ModelPricing(
        input_price=Decimal("15.00"),
        output_price=Decimal("75.00"),
    ),
    # Azure OpenAI / GPT models (estimated)
    "gpt-5-codex": ModelPricing(
        input_price=Decimal("5.00"),
        output_price=Decimal("15.00"),
    ),
    "gpt-4-turbo": ModelPricing(
        input_price=Decimal("10.00"),
        output_price=Decimal("30.00"),
    ),
    "gpt-4o": ModelPricing(
        input_price=Decimal("2.50"),
        output_price=Decimal("10.00"),
    ),
    "gpt-4o-mini": ModelPricing(
        input_price=Decimal("0.15"),
        output_price=Decimal("0.60"),
    ),
    # OpenRouter models (provider/model format)
    # Anthropic via OpenRouter
    "anthropic/claude-opus-4": ModelPricing(
        input_price=Decimal("15.00"),
        output_price=Decimal("75.00"),
    ),
    "anthropic/claude-sonnet-4": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
    ),
    "anthropic/claude-3.5-sonnet": ModelPricing(
        input_price=Decimal("3.00"),
        output_price=Decimal("15.00"),
    ),
    "anthropic/claude-3-opus": ModelPricing(
        input_price=Decimal("15.00"),
        output_price=Decimal("75.00"),
    ),
    "anthropic/claude-3-haiku": ModelPricing(
        input_price=Decimal("0.25"),
        output_price=Decimal("1.25"),
    ),
    # OpenAI via OpenRouter
    "openai/gpt-4o": ModelPricing(
        input_price=Decimal("2.50"),
        output_price=Decimal("10.00"),
    ),
    "openai/gpt-4o-mini": ModelPricing(
        input_price=Decimal("0.15"),
        output_price=Decimal("0.60"),
    ),
    "openai/gpt-4-turbo": ModelPricing(
        input_price=Decimal("10.00"),
        output_price=Decimal("30.00"),
    ),
    # Google via OpenRouter
    "google/gemini-pro-1.5": ModelPricing(
        input_price=Decimal("1.25"),
        output_price=Decimal("5.00"),
    ),
    "google/gemini-flash-1.5": ModelPricing(
        input_price=Decimal("0.075"),
        output_price=Decimal("0.30"),
    ),
    # Meta via OpenRouter
    "meta-llama/llama-3.1-405b-instruct": ModelPricing(
        input_price=Decimal("2.70"),
        output_price=Decimal("2.70"),
    ),
    "meta-llama/llama-3.1-70b-instruct": ModelPricing(
        input_price=Decimal("0.52"),
        output_price=Decimal("0.75"),
    ),
    # Mistral via OpenRouter
    "mistralai/mistral-large": ModelPricing(
        input_price=Decimal("2.00"),
        output_price=Decimal("6.00"),
    ),
}

# Default pricing for unknown models (conservative estimate)
DEFAULT_PRICING = ModelPricing(
    input_price=Decimal("5.00"),
    output_price=Decimal("15.00"),
)


def get_model_pricing(model: str) -> ModelPricing:
    """
    Get pricing for a model.

    Args:
        model: Model name or ID

    Returns:
        ModelPricing with input and output prices
    """
    # Try exact match first
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]

    # Try partial match (for versioned models)
    model_lower = model.lower()
    for known_model, pricing in MODEL_PRICING.items():
        if known_model.lower() in model_lower or model_lower in known_model.lower():
            return pricing

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


def get_current_model_name() -> str:
    """Get the currently configured model name."""
    settings = get_settings()

    if settings.ai_provider == "anthropic":
        return settings.anthropic_model
    elif settings.ai_provider == "bedrock":
        return settings.bedrock_model_id
    elif settings.ai_provider == "azure":
        return settings.azure_openai_deployment or "unknown"
    elif settings.ai_provider == "openrouter":
        return settings.openrouter_model
    else:
        return "unknown"
