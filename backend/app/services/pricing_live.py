"""
Live Bedrock model pricing from the AWS **public** bulk Price List.

AWS publishes per-service offer files at a public, unauthenticated URL, so the
backend (which has free egress, unlike the sandbox) can fetch the AmazonBedrock
region offer and derive accurate per-token prices at catalog-refresh time — no
IAM creds (the discovery path only holds a Bedrock *bearer* token), and no
hand-maintained price table to rot.

The offer's products carry a loose model token in ``attributes.usagetype``
(e.g. ``EU-NovaLite-input-tokens``, ``EU-mistral.magistral-small-2509-input-tokens``);
:func:`app.services.pricing.price_match_keys` bridges those to real Bedrock model
IDs. Best-effort throughout: any failure leaves pricing on the static table.
"""

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.services.pricing import ModelPricing, price_match_keys

logger = logging.getLogger(__name__)

# Public bulk Price List (region offer file). No auth. ~1MB.
_PRICE_LIST_URL = (
    "https://pricing.us-east-1.amazonaws.com"
    "/offers/v1.0/aws/AmazonBedrock/current/{region}/index.json"
)

# usagetype = "<REGION>-<model token>-<input|output>-tokens[-flex/-batch/...]".
_REGION_PREFIX_RE = re.compile(r"^[A-Za-z]{2,4}-")
_TOKEN_SUFFIX_RE = re.compile(r"-(?:input|output)-tokens.*$", re.IGNORECASE)
# Price unit → multiplier to USD per 1M tokens.
_PER_MILLION: dict[str, Decimal] = {
    "1k tokens": Decimal(1000),
    "1m tokens": Decimal(1),
    "tokens": Decimal(1_000_000),
}


def _usagetype_token(usagetype: str) -> str:
    """The model token embedded in a usagetype (region prefix + token suffix stripped)."""
    return _TOKEN_SUFFIX_RE.sub("", _REGION_PREFIX_RE.sub("", usagetype))


def _on_demand_usd_unit(terms: dict[str, Any], sku: str) -> tuple[str | None, str | None]:
    """The (pricePerUnit.USD, unit) of a sku's first OnDemand price dimension."""
    term = next(iter((terms.get(sku) or {}).values()), None)
    if not isinstance(term, dict):
        return None, None
    dim = next(iter((term.get("priceDimensions") or {}).values()), None)
    if not isinstance(dim, dict):
        return None, None
    return (dim.get("pricePerUnit") or {}).get("USD"), dim.get("unit")


def _per_million(usd: str | None, unit: str | None) -> Decimal | None:
    mult = _PER_MILLION.get((unit or "").strip().lower())
    if mult is None or usd is None:
        return None
    try:
        return Decimal(str(usd)) * mult
    except (InvalidOperation, TypeError):
        return None


def parse_price_list(doc: dict[str, Any]) -> dict[str, ModelPricing]:
    """Build {match-key → ModelPricing} from a Bedrock offer document.

    Considers only **standard on-demand** input/output token products (skips
    batch/flex/priority/cache tiers and image/embedding modalities), pairs each
    model token's input+output prices, and indexes by every ``price_match_keys``
    so model-ID lookups resolve regardless of naming style.
    """
    products = doc.get("products") or {}
    on_demand = (doc.get("terms") or {}).get("OnDemand") or {}

    pairs: dict[str, dict[str, Decimal]] = {}
    for sku, prod in products.items():
        attrs = (prod or {}).get("attributes") or {}
        itype = attrs.get("inferenceType")
        if itype not in ("Input tokens", "Output tokens"):  # exact = standard on-demand
            continue
        # Require an explicit On-demand feature: a few products repeat the plain
        # inferenceType under Batch or a null feature — conservatively skip those rather
        # than risk pricing a chat turn at a batch rate. A model whose only price rows are
        # null-feature stays unpriced (admin override) instead of mispriced.
        if "on-demand" not in (attrs.get("feature") or "").lower():
            continue
        per_m = _per_million(*_on_demand_usd_unit(on_demand, sku))
        if per_m is None:
            continue
        token = _usagetype_token(attrs.get("usagetype") or "")
        if not token:
            continue
        pairs.setdefault(token, {})["in" if itype == "Input tokens" else "out"] = per_m

    by_key: dict[str, ModelPricing] = {}
    for token, p in pairs.items():
        if "in" not in p or "out" not in p:  # need both halves to price a model
            continue
        pricing = ModelPricing(input_price=p["in"], output_price=p["out"])
        for key in price_match_keys(token):
            by_key.setdefault(key, pricing)  # first token wins a shared key
    return by_key


async def fetch_bedrock_pricing(
    region: str | None, *, timeout: float = 15.0
) -> dict[str, ModelPricing]:
    """Fetch + parse the region's public Bedrock Price List. Best-effort → {} on failure."""
    if not region:
        return {}
    url = _PRICE_LIST_URL.format(region=region)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            doc = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Bedrock price-list fetch failed (%s): %s", region, e)
        return {}
    by_key = parse_price_list(doc)
    logger.info("Bedrock price list resolved %d match keys (%s)", len(by_key), region)
    return by_key
