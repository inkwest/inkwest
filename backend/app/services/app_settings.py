"""
Runtime-tunable application settings persisted in the database.

A small key/value store for the few settings admins may change without a
redeploy. Values are loaded into module memory at startup and refreshed on every
write so the hot path (read per chat turn) stays sync and DB-free. With multiple
backend replicas the others pick up a change on restart only.

The model catalog/default/enabled configuration moved to its own table — see
app.services.model_registry. This module now holds only scalar feature flags
(e.g. whether to surface open-weight Bedrock models in the catalog).
"""

import asyncio
import logging

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_setting import AppSetting

logger = logging.getLogger(__name__)

# Whether the Bedrock catalog includes non-Anthropic (open-weight) models.
BEDROCK_INCLUDE_OPEN_KEY = "bedrock_include_open"

_settings: dict[str, str] = {}
# Serializes writes so concurrent admin saves can't leave the in-memory copy
# holding a different writer's value than the database (assignment is post-commit).
_save_lock = asyncio.Lock()

_TRUE = {"1", "true", "yes", "on"}


def get_setting(key: str) -> str | None:
    """Get a setting value from memory, or None when unset."""
    return _settings.get(key)


def get_bool_setting(key: str, default: bool = False) -> bool:
    """Get a boolean setting; unset/empty falls back to ``default``."""
    raw = _settings.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE


async def load_settings(db: AsyncSession) -> None:
    """Load all persisted settings into memory (startup)."""
    global _settings
    result = await db.execute(select(AppSetting.key, AppSetting.value))
    _settings = {row.key: row.value for row in result}
    if _settings:
        logger.info(f"Loaded {len(_settings)} app setting(s)")


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    """Persist a setting and refresh the in-memory copy."""
    async with _save_lock:
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
        await db.commit()
        _settings[key] = value
    logger.info(f"App setting updated: {key}={value}")
