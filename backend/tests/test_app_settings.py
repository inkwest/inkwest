"""Tests for the generic DB-persisted runtime settings (KV store)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import app_settings
from app.services.app_settings import (
    get_bool_setting,
    get_int_setting,
    get_setting,
    load_settings,
    set_setting,
)


@pytest.fixture(autouse=True)
def reset_state():
    yield
    app_settings._settings = {}


def _rows(**pairs: str) -> AsyncMock:
    """Mock db whose execute returns key/value rows for load_settings()."""
    db = AsyncMock()
    rows = [MagicMock(key=k, value=v) for k, v in pairs.items()]

    async def execute(_query):
        result = MagicMock()
        result.__iter__ = lambda self: iter(rows)
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


@pytest.mark.asyncio
async def test_load_settings_populates_memory():
    db = _rows(bedrock_include_open="true", other="x")
    await load_settings(db)
    assert get_setting("bedrock_include_open") == "true"
    assert get_setting("other") == "x"


@pytest.mark.asyncio
async def test_load_settings_empty_db():
    db = _rows()
    await load_settings(db)
    assert get_setting("anything") is None


def test_get_setting_unset_is_none():
    assert get_setting("missing") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("nope", False),
    ],
)
def test_get_bool_setting(raw, expected):
    app_settings._settings = {"flag": raw}
    assert get_bool_setting("flag") is expected


def test_get_bool_setting_default_when_unset():
    assert get_bool_setting("missing", default=True) is True
    assert get_bool_setting("missing") is False


@pytest.mark.parametrize(
    "raw,expected",
    [("0", 0), ("5", 5), ("42", 42), ("-3", -3)],
)
def test_get_int_setting(raw, expected):
    app_settings._settings = {"cap": raw}
    assert get_int_setting("cap", 99) == expected


@pytest.mark.parametrize("raw", ["", "  ", "abc", "1.5"])
def test_get_int_setting_falls_back_on_bad_value(raw):
    app_settings._settings = {"cap": raw}
    assert get_int_setting("cap", 7) == 7


def test_get_int_setting_default_when_unset():
    assert get_int_setting("missing", 5) == 5


@pytest.mark.asyncio
async def test_set_setting_upserts_and_updates_memory():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.commit = AsyncMock()

    await set_setting(db, "bedrock_include_open", "true")

    db.execute.assert_awaited_once()  # single ON CONFLICT upsert
    db.commit.assert_awaited_once()
    assert get_setting("bedrock_include_open") == "true"
