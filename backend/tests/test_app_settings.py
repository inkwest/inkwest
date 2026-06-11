"""Tests for the DB-persisted runtime settings (startup load + parsing)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import app_settings
from app.services.app_settings import _parse_enabled_models, load_model_settings


@pytest.fixture(autouse=True)
def reset_state():
    yield
    app_settings._model_override = None
    app_settings._enabled_models = None


class TestParseEnabledModels:
    def test_valid_list(self):
        assert _parse_enabled_models('["a", "b"]') == ["a", "b"]

    def test_none_means_unset(self):
        assert _parse_enabled_models(None) is None

    def test_malformed_json_ignored(self):
        assert _parse_enabled_models("{not json") is None

    def test_non_list_ignored(self):
        assert _parse_enabled_models('{"a": 1}') is None

    def test_non_string_items_ignored(self):
        assert _parse_enabled_models('["a", 1]') is None


def _db_returning(rows_by_key: dict) -> AsyncMock:
    """Mock db whose execute returns the AppSetting row for the queried key."""
    db = AsyncMock()
    calls = iter(rows_by_key.values())

    async def execute(_query):
        result = MagicMock()
        result.scalar_one_or_none.return_value = next(calls)
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


@pytest.mark.asyncio
async def test_load_model_settings_populates_memory():
    model_row = MagicMock(value="claude-opus-4-8")
    enabled_row = MagicMock(value='["claude-opus-4-8", "claude-haiku-4-5"]')
    db = _db_returning({"anthropic_model": model_row, "enabled_models": enabled_row})

    await load_model_settings(db)

    assert app_settings.get_model_override() == "claude-opus-4-8"
    assert app_settings.get_enabled_models() == ["claude-opus-4-8", "claude-haiku-4-5"]


@pytest.mark.asyncio
async def test_load_model_settings_survives_corrupted_row():
    """A corrupted enabled_models row must not break startup — it parses to unset."""
    model_row = MagicMock(value="claude-opus-4-8")
    corrupted = MagicMock(value="{corrupted!!")
    db = _db_returning({"anthropic_model": model_row, "enabled_models": corrupted})

    await load_model_settings(db)

    assert app_settings.get_model_override() == "claude-opus-4-8"
    assert app_settings.get_enabled_models() is None


@pytest.mark.asyncio
async def test_load_model_settings_empty_db():
    db = _db_returning({"anthropic_model": None, "enabled_models": None})

    await load_model_settings(db)

    assert app_settings.get_model_override() is None
    assert app_settings.get_enabled_models() is None
