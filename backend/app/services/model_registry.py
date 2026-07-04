"""
Model registry: admin-managed selection + metadata over the discovered catalog.

Owns everything the admin configures about models and everything the rest of the
app reads about them:

- which models users may pick (``selectable_models``) and the default
  (``default_model``) — resolved against the *current* catalog so a stale
  persisted choice (e.g. after a backend/catalog change) can never be served;
- the merged view the admin/console UIs render (``merged_entries``): discovered
  models plus operator-added custom rows, each carrying label, provider,
  pricing, and lifecycle flags;
- pricing resolution with per-model overrides (``calculate_cost``; the merged
  ``ModelEntry`` carries the effective ``priced`` flag and prices for the UI).

Persistent state lives in the ``model_registry`` table. Rows exist only for
models that carry admin intent (enabled/default/override) or are operator-added;
discovered models with no row are synthesized on read as disabled, unpriced
unless the static table knows them. The in-memory snapshot is loaded at startup
and refreshed on every write, keeping the hot path (read per chat turn) sync and
DB-free — like app.services.app_settings.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_registry import ModelRegistry
from app.services.model_catalog import (
    available_models,
    bedrock_vendor,
    is_anthropic_model,
    model_modalities,
    profile_arn,
    profile_ids_for_arn,
)
from app.services.pricing import (
    DEFAULT_PRICING,
    ModelPricing,
    cost_for_pricing,
    get_model_pricing,
    is_legacy_model,
    model_label,
)
from app.services.pricing import (
    _pricing_candidates as _id_candidates,
)
from app.services.pricing import (
    calculate_cost as _table_cost,
)

logger = logging.getLogger(__name__)

# Columns an admin may patch on a single model via update_model().
EDITABLE_FIELDS = frozenset(
    {
        "display_name",
        "endpoint",
        "arn",
        "options",
        "input_price",
        "output_price",
        "legacy",
        "enabled",
    }
)

_rows: dict[str, dict[str, Any]] = {}
# Serializes writes so concurrent admin saves can't leave the in-memory copy
# holding a different writer's values than the database (reload is post-commit).
_save_lock = asyncio.Lock()


@dataclass(frozen=True)
class ModelEntry:
    """A model as the admin/console UIs see it: identity + config + pricing."""

    model_id: str
    provider: str
    label: str
    enabled: bool
    is_default: bool
    source: str
    legacy: bool
    # False = no real pricing (override unset and not in the static table); cost
    # tracking falls back to a conservative default, surfaced in the admin UI.
    priced: bool
    endpoint: str | None
    arn: str | None
    options: dict[str, Any] = field(default_factory=dict)
    input_price: Decimal | None = None
    output_price: Decimal | None = None
    # Modality capabilities captured from the live Bedrock catalog (P1.8). Empty =
    # unknown (off-Bedrock, custom row, or no live data) ⇒ treated as text-only. Drives
    # the picker's IMAGE-upload gate + the admin modality icons. Bedrock terms: input
    # TEXT/IMAGE/VIDEO/SPEECH, output TEXT/EMBEDDING/IMAGE/VIDEO.
    input_modalities: list[str] = field(default_factory=list)
    output_modalities: list[str] = field(default_factory=list)


def _derive_provider(model_id: str) -> str:
    """Provider tag for a discovered model ID (no explicit row).

    The tag is the per-vendor Bedrock namespace (``anthropic``, ``meta``,
    ``mistral``...) whenever the ID carries a recognized vendor namespace — so the
    admin UI groups by vendor and per-turn routing reads the right provider. With
    the inkwest-agent engine on, the whole-Bedrock catalog surfaces vendor models;
    each keeps its own vendor tag so a Claude model routes via the Messages/CLI path
    and an open-weight one via Converse. A bare ``claude-*`` (Anthropic API / gateway,
    no Bedrock namespace) is tagged ``anthropic``; any other un-namespaced ID keeps
    the generic ``bedrock`` tag (``model_catalog.bedrock_vendor``'s own fallback).
    """
    vendor = bedrock_vendor(model_id)
    if vendor != "bedrock":  # a recognized Bedrock vendor namespace (incl. anthropic)
        return vendor
    if is_anthropic_model(model_id):  # bare claude-* without a Bedrock namespace
        return "anthropic"
    # Un-namespaced, non-Claude ID → the generic "bedrock" tag (bedrock_vendor's own
    # fallback). NB: "bedrock" is in _MESSAGES_PROVIDERS, so such a model is treated as
    # Messages-capable and can run on claude-cli — correct for the only realistic source
    # of a bare non-Claude ID (an Anthropic-compatible gateway, which speaks Messages).
    return vendor


def _row_pricing(row: dict[str, Any] | None) -> tuple[ModelPricing | None, bool]:
    """A row's override pricing if set, else (None, False)."""
    if row and row.get("input_price") is not None and row.get("output_price") is not None:
        pricing = ModelPricing(row["input_price"], row["output_price"], legacy=bool(row["legacy"]))
        return pricing, True
    return None, False


def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> Decimal:
    """Cost in USD honoring any per-model pricing override (prompt-cache aware).

    Overrides carry input/output prices only; cache prices derive from the
    override's input_price via the default multipliers (cost_for_pricing).
    """
    override, _ = _row_pricing(_rows.get(model))
    if override is None:
        return _table_cost(
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            cache_write_1h_tokens=cache_write_1h_tokens,
        )
    return cost_for_pricing(
        override,
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
    )


def resolve_invoke_target(target: str) -> str:
    """Registry model_id behind a proxied invoke target (bare id, inference-profile id, ARN).

    The LLM broker meters per call, but the URL carries whatever the engine invoked — Claude
    Code auto-upgrades a bare foundation id to its regional profile id (``eu.anthropic.…``),
    and profile-only models invoke via their ARN. Usage rows must aggregate under the
    registry's friendly model_id regardless. Unresolvable targets return verbatim — the
    pricing lookup applies its own normalization, so cost still resolves for known families.
    """
    entries = merged_entries()
    ids = {e.model_id for e in entries}
    if target in ids:
        return target
    for e in entries:
        if e.arn and (target == e.arn or target in profile_ids_for_arn(e.arn)):
            return e.model_id
    # Progressive normalization (route prefix / version / date stripped) → known id.
    for candidate in _id_candidates(target):
        if candidate in ids:
            return candidate
    return target


def _entry(model_id: str, row: dict[str, Any] | None) -> ModelEntry:
    override, has_override = _row_pricing(row)
    pricing = override or get_model_pricing(model_id)
    priced = has_override or pricing is not DEFAULT_PRICING
    # Modalities come from the live catalog only (no DB column): a custom row with no live
    # match yields empty lists = text-only/unknown, the back-compat default.
    input_modalities, output_modalities = model_modalities(model_id)
    # Invoke target: an explicit row arn (custom/admin intent) always wins; otherwise a
    # discovered profile-only model gets its inference-profile ARN from the
    # live catalog so the orchestrator/agent invokes the profile, not the bare id.
    arn = (row.get("arn") if row else None) or profile_arn(model_id)
    return ModelEntry(
        model_id=model_id,
        # Re-derive the vendor tag for discovered rows: their stored provider is just a
        # derivation cached at enable-time (set_model_settings upserts it but never
        # refreshes it), so a row enabled before a derivation change keeps a stale tag —
        # which mis-grouped/mis-routed open-weight models lumped under "claude-cli". Only
        # a custom row's provider is admin intent and kept verbatim.
        provider=(
            row["provider"]
            if (row and row.get("source") == "custom")
            else _derive_provider(model_id)
        ),
        label=(row.get("display_name") if row else None) or model_label(model_id),
        enabled=bool(row["enabled"]) if row else False,
        is_default=bool(row["is_default"]) if row else False,
        source=row["source"] if row else "discovered",
        legacy=(bool(row["legacy"]) if row else False) or is_legacy_model(model_id),
        priced=priced,
        endpoint=row.get("endpoint") if row else None,
        arn=arn,
        options=(row.get("options") or {}) if row else {},
        input_price=pricing.input_price if priced else None,
        output_price=pricing.output_price if priced else None,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
    )


def merged_entries() -> list[ModelEntry]:
    """Discovered catalog ∪ custom rows, newest-first then custom in add order.

    Discovered IDs come ordered newest-first from the catalog; custom rows the
    catalog does not surface are appended in insertion (created_at) order.
    """
    discovered = available_models()
    seen = set(discovered)
    entries = [_entry(mid, _rows.get(mid)) for mid in discovered]
    entries += [
        _entry(mid, row)
        for mid, row in _rows.items()
        if mid not in seen and row["source"] == "custom"
    ]
    return entries


def selectable_models() -> list[str]:
    """Model IDs users may pick per session: the admin-enabled subset.

    No enabled rows means the registry is unconfigured — offer everything
    available (fresh-deploy default-open), mirroring the original "unset = all"
    semantics. Otherwise the enabled set, already intersected with the live
    catalog by construction (synthesized entries are never enabled).
    """
    entries = merged_entries()
    enabled = [e.model_id for e in entries if e.enabled]
    return enabled or [e.model_id for e in entries]


def get_entry(model_id: str) -> ModelEntry | None:
    """The merged entry for a model ID, or None if it is not in the catalog."""
    for entry in merged_entries():
        if entry.model_id == model_id:
            return entry
    return None


def configured_default_id() -> str | None:
    """The admin-set default model ID if one is configured and still available."""
    for entry in merged_entries():
        if entry.is_default:
            return entry.model_id
    return None


def default_model() -> str:
    """The model applied when a session has no explicit choice.

    The admin default wins while it is still enabled and available; otherwise the
    newest enabled model, else the newest available — the catalog is ordered
    newest-first, so a fresh deployment defaults to the latest model.
    """
    entries = merged_entries()
    candidates = [e for e in entries if e.enabled] or entries
    for entry in candidates:
        if entry.is_default:
            return entry.model_id
    return candidates[0].model_id


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _snapshot(model: ModelRegistry) -> dict[str, Any]:
    return {
        "model_id": model.model_id,
        "provider": model.provider,
        "display_name": model.display_name,
        "endpoint": model.endpoint,
        "arn": model.arn,
        "options": model.options or {},
        "input_price": model.input_price,
        "output_price": model.output_price,
        "enabled": model.enabled,
        "is_default": model.is_default,
        "source": model.source,
        "legacy": model.legacy,
    }


async def load_registry(db: AsyncSession) -> None:
    """Load all registry rows into memory (startup), oldest-first for stable order."""
    global _rows
    result = await db.execute(select(ModelRegistry).order_by(ModelRegistry.created_at))
    _rows = {m.model_id: _snapshot(m) for m in result.scalars()}
    if _rows:
        logger.info(f"Loaded {len(_rows)} model registry row(s)")


def _blank_row(model_id: str) -> dict[str, Any]:
    """A default in-memory discovered row for a model that has no DB row yet."""
    return {
        "model_id": model_id,
        "provider": _derive_provider(model_id),
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


def _validate_price_pair(input_price: Decimal | None, output_price: Decimal | None) -> None:
    if (input_price is None) != (output_price is None):
        raise ValueError("input_price and output_price must be set together")


# Writes persist to the database and then mirror the change into the in-memory
# snapshot post-commit (rather than re-SELECTing): the snapshot is the single
# authoritative copy on the hot path, and mirroring keeps writes DB-read-free —
# the same approach as app.services.app_settings.


async def set_model_settings(
    db: AsyncSession,
    default: str,
    enabled: list[str],
    availability: dict[str, list[str]] | None = None,
) -> None:
    """Bulk save: enable exactly ``enabled`` and make ``default`` the default.

    Upserts a row per enabled model (preserving existing source/overrides),
    disables every other row, and moves the single default flag.

    ``availability`` (optional) sets each model's per-engine offering — the engine
    ids it is exposed on, stored in ``options["engines"]`` (see
    ``engines.capable_engines``). ``None`` leaves ``options`` untouched (the
    legacy enable-only path); an absent key likewise leaves a model's engines
    unset (= offered on all its candidate engines).
    """
    if default not in enabled:
        raise ValueError("the default model must be one of the enabled models")
    async with _save_lock:
        for model_id in enabled:
            stmt = (
                pg_insert(ModelRegistry)
                .values(
                    model_id=model_id,
                    provider=_derive_provider(model_id),
                    source="discovered",
                    enabled=True,
                )
                # Existing rows keep their source/metadata/overrides; only the
                # enabled flag is (re)asserted.
                .on_conflict_do_update(
                    index_elements=[ModelRegistry.model_id],
                    set_={"enabled": True},
                )
            )
            await db.execute(stmt)
        # Disable anything not in the new set.
        await db.execute(
            update(ModelRegistry)
            .where(ModelRegistry.enabled, ModelRegistry.model_id.not_in(enabled))
            .values(enabled=False)
        )
        # Move the default flag (clear all, then set one — keeps the partial
        # unique index satisfied at each statement boundary).
        await db.execute(
            update(ModelRegistry).where(ModelRegistry.is_default).values(is_default=False)
        )
        await db.execute(
            update(ModelRegistry).where(ModelRegistry.model_id == default).values(is_default=True)
        )
        # Per-engine availability: merge ``engines`` into each model's options JSONB
        # (preserving any other option keys, e.g. max_tokens). Done before commit so
        # it lands atomically with the enable/default flips.
        if availability is not None:
            for model_id, engines in availability.items():
                existing = dict(_rows.get(model_id, _blank_row(model_id)).get("options") or {})
                existing["engines"] = list(engines)
                await db.execute(
                    update(ModelRegistry)
                    .where(ModelRegistry.model_id == model_id)
                    .values(options=existing)
                )
        await db.commit()
        enabled_set = set(enabled)
        # First pass only *creates* rows for newly-enabled discovered models (the
        # enabled flag is authoritatively (re)set in the second pass over all rows).
        for model_id in enabled:
            _rows.setdefault(model_id, _blank_row(model_id))
        for model_id, row in _rows.items():
            row["enabled"] = model_id in enabled_set
            row["is_default"] = model_id == default
        if availability is not None:
            for model_id, engines in availability.items():
                row = _rows.setdefault(model_id, _blank_row(model_id))
                row["options"] = {**(row.get("options") or {}), "engines": list(engines)}
    logger.info(f"Model settings updated: default={default}, enabled={enabled}")


async def add_custom_model(
    db: AsyncSession,
    *,
    model_id: str,
    provider: str,
    display_name: str | None = None,
    endpoint: str | None = None,
    arn: str | None = None,
    options: dict[str, Any] | None = None,
    input_price: Decimal | None = None,
    output_price: Decimal | None = None,
    enabled: bool = False,
    legacy: bool = False,
) -> None:
    """Add an operator-defined model the discovery filter does not surface."""
    _validate_price_pair(input_price, output_price)
    async with _save_lock:
        # Re-check uniqueness under the lock so two concurrent adds can't both
        # pass and have the second hit a raw PK violation (500).
        if model_id in _rows or model_id in available_models():
            raise ValueError(f"model {model_id} already exists")
        db.add(
            ModelRegistry(
                model_id=model_id,
                provider=provider,
                display_name=display_name,
                endpoint=endpoint,
                arn=arn,
                options=options or {},
                input_price=input_price,
                output_price=output_price,
                enabled=enabled,
                source="custom",
                legacy=legacy,
            )
        )
        await db.commit()
        # Key set lives in _blank_row(); spread it so a new column can't desync.
        _rows[model_id] = {
            **_blank_row(model_id),
            "provider": provider,
            "display_name": display_name,
            "endpoint": endpoint,
            "arn": arn,
            "options": options or {},
            "input_price": input_price,
            "output_price": output_price,
            "enabled": enabled,
            "source": "custom",
            "legacy": legacy,
        }
    logger.info(f"Custom model added: {model_id} ({provider})")


async def update_model(db: AsyncSession, model_id: str, **fields: Any) -> None:
    """Patch a model's metadata, creating a row for a discovered model if needed."""
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"non-editable fields: {', '.join(sorted(unknown))}")
    if "input_price" in fields or "output_price" in fields:
        existing = _rows.get(model_id, {})
        _validate_price_pair(
            fields.get("input_price", existing.get("input_price")),
            fields.get("output_price", existing.get("output_price")),
        )
    if model_id not in _rows and model_id not in available_models():
        raise ValueError(f"unknown model: {model_id}")
    async with _save_lock:
        stmt = (
            pg_insert(ModelRegistry)
            .values(
                model_id=model_id,
                provider=_derive_provider(model_id),
                source="discovered",
                **fields,
            )
            .on_conflict_do_update(index_elements=[ModelRegistry.model_id], set_=fields)
        )
        await db.execute(stmt)
        await db.commit()
        _rows.setdefault(model_id, _blank_row(model_id)).update(fields)
    logger.info(f"Model updated: {model_id} ({', '.join(fields)})")


async def delete_model(db: AsyncSession, model_id: str) -> None:
    """Remove a custom model. Discovered models cannot be deleted, only disabled."""
    row = _rows.get(model_id)
    if row is None:
        raise ValueError(f"unknown model: {model_id}")
    if row["source"] != "custom":
        raise ValueError("discovered models cannot be deleted; disable them instead")
    async with _save_lock:
        await db.execute(delete(ModelRegistry).where(ModelRegistry.model_id == model_id))
        await db.commit()
        _rows.pop(model_id, None)
    logger.info(f"Custom model deleted: {model_id}")
