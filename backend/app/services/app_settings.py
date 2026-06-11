"""
Runtime-tunable application settings persisted in the database.

Holds the model configuration chosen from the admin console — the database is
the single source of truth (no env-configured model):
- ``anthropic_model``: the default model; unset means "newest catalog model"
  (resolved by app.services.model_catalog).
- ``enabled_models``: JSON array of model IDs users may pick per session;
  unset means "all available models" (resolved by callers, keeping this
  module free of a circular import on the pricing catalog).

Values are loaded into module memory at startup and refreshed on every admin
update so the hot path (called per chat turn) stays sync and DB-free. With
multiple backend replicas, the others pick up a change on restart only.
"""

import asyncio
import json
import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_setting import AppSetting

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL_KEY = "anthropic_model"
ENABLED_MODELS_KEY = "enabled_models"

_model_override: str | None = None
_enabled_models: list[str] | None = None
# Serializes saves so concurrent PUTs can't leave the in-memory copy holding a
# different writer's values than the database (assignment happens post-commit).
_save_lock = asyncio.Lock()


def get_model_override() -> str | None:
    """Get the admin-selected default model, or None when unset."""
    return _model_override


def get_enabled_models() -> list[str] | None:
    """Get the admin-enabled model IDs, or None when unset (= all available)."""
    return _enabled_models


async def _get_setting(db: AsyncSession, key: str) -> AppSetting | None:
    result = await db.execute(select(AppSetting).where(AppSetting.key == key))
    return result.scalar_one_or_none()


async def _upsert_setting(db: AsyncSession, key: str, value: str) -> None:
    # Atomic ON CONFLICT upsert: a select-then-insert would race under
    # concurrent admin saves (unique violation -> 500).
    stmt = (
        pg_insert(AppSetting)
        .values(key=key, value=value)
        .on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": value, "updated_at": func.now()},
        )
    )
    await db.execute(stmt)


def _parse_enabled_models(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if isinstance(value, list) and all(isinstance(m, str) for m in value):
            return value
    except json.JSONDecodeError:
        pass
    logger.warning(f"Ignoring malformed {ENABLED_MODELS_KEY} setting: {raw!r}")
    return None


async def load_model_settings(db: AsyncSession) -> None:
    """Load the persisted model settings into memory (startup)."""
    global _model_override, _enabled_models
    setting = await _get_setting(db, ANTHROPIC_MODEL_KEY)
    _model_override = setting.value if setting else None
    enabled = await _get_setting(db, ENABLED_MODELS_KEY)
    _enabled_models = _parse_enabled_models(enabled.value if enabled else None)
    if _model_override or _enabled_models:
        logger.info(f"Loaded model settings: default={_model_override}, enabled={_enabled_models}")


async def set_model_settings(db: AsyncSession, default_model: str, enabled: list[str]) -> None:
    """Persist the admin model configuration and refresh the in-memory copy."""
    global _model_override, _enabled_models
    async with _save_lock:
        await _upsert_setting(db, ANTHROPIC_MODEL_KEY, default_model)
        await _upsert_setting(db, ENABLED_MODELS_KEY, json.dumps(enabled))
        await db.commit()
        _model_override = default_model
        _enabled_models = enabled
    logger.info(f"Model settings updated: default={default_model}, enabled={enabled}")
