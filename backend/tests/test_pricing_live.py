"""Tests for live Bedrock pricing parsing (app.services.pricing_live)."""

from decimal import Decimal

from app.services.pricing import price_match_keys
from app.services.pricing_live import _usagetype_token, parse_price_list


def _term(usd: str, unit: str = "1K tokens") -> dict:
    # Shape of terms.OnDemand[sku]: {<sku.termCode>: {priceDimensions: {<rate>: {...}}}}.
    return {
        "SKU.JRTCKXETXF": {
            "priceDimensions": {
                "SKU.JRTCKXETXF.6YS6EN2CT7": {"unit": unit, "pricePerUnit": {"USD": usd}}
            }
        }
    }


def _product(sku: str, usagetype: str, itype: str, feature: str = "On-demand Inference") -> dict:
    return {
        "sku": sku,
        "attributes": {"usagetype": usagetype, "inferenceType": itype, "feature": feature},
    }


def test_usagetype_token_strips_region_and_suffix():
    assert _usagetype_token("EU-NovaLite-input-tokens") == "NovaLite"
    assert _usagetype_token("US-mistral.magistral-small-2509-output-tokens") == (
        "mistral.magistral-small-2509"
    )
    assert _usagetype_token("EU-zai.glm-4.7-flash-input-tokens") == "zai.glm-4.7-flash"
    # flex/batch tier suffix also dropped
    assert _usagetype_token("EU-Qwen3-32B-output-tokens-flex") == "Qwen3-32B"


def test_parse_price_list_pairs_input_output_per_million():
    doc = {
        "products": {
            "S1": _product("S1", "EU-NovaLite-input-tokens", "Input tokens"),
            "S2": _product("S2", "EU-NovaLite-output-tokens", "Output tokens"),
            "S3": _product("S3", "EU-mistral.magistral-small-2509-input-tokens", "Input tokens"),
            "S4": _product("S4", "EU-mistral.magistral-small-2509-output-tokens", "Output tokens"),
            # batch tier — must be ignored (inferenceType not exactly Input/Output tokens)
            "S5": _product("S5", "EU-NovaLite-input-tokens-batch", "input tokens batch"),
            # only an input half — no pair → dropped
            "S6": _product("S6", "EU-Lonely-input-tokens", "Input tokens"),
        },
        "terms": {
            "OnDemand": {
                "S1": _term("0.00006"),  # 0.06 / 1M
                "S2": _term("0.00024"),  # 0.24 / 1M
                "S3": _term("0.0005"),  # 0.50 / 1M
                "S4": _term("0.0015"),  # 1.50 / 1M
                "S5": _term("0.00001"),
                "S6": _term("0.0009"),
            }
        },
    }
    by_key = parse_price_list(doc)

    # Nova Lite resolves for the real model ID via the shared match keys.
    nova = by_key[next(iter(price_match_keys("eu.amazon.nova-lite-v1:0") & by_key.keys()))]
    assert nova.input_price == Decimal("0.06")
    assert nova.output_price == Decimal("0.24")

    mag_keys = price_match_keys("mistral.magistral-small-2509") & by_key.keys()
    assert mag_keys, "magistral should resolve"
    mag = by_key[next(iter(mag_keys))]
    assert mag.input_price == Decimal("0.50")
    assert mag.output_price == Decimal("1.50")

    # Lonely (input only) and the batch tier produced no entry.
    assert not (price_match_keys("Lonely") & by_key.keys())


def test_parse_price_list_skips_unknown_unit():
    doc = {
        "products": {
            "S1": _product("S1", "EU-Foo-input-tokens", "Input tokens"),
            "S2": _product("S2", "EU-Foo-output-tokens", "Output tokens"),
        },
        "terms": {"OnDemand": {"S1": _term("1", unit="hours"), "S2": _term("1", unit="hours")}},
    }
    assert parse_price_list(doc) == {}
