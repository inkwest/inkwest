"""
Effective-skills resolution + preferences-document parsing.

Hermetic: the DB is an AsyncMock whose ``execute`` is fed pre-baked results in call
order (admin skills, prefs row, personal skills) — mirroring the repo's mocked-DB style.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.user_preferences import (
    _disabled_admin,
    get_effective_skills,
)

_USER = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


def _skill(name: str, *, content: str | None = None, is_active: bool = True) -> SimpleNamespace:
    """A duck-typed admin/personal skill (only name/content/description/is_active used)."""
    return SimpleNamespace(
        name=name,
        content=content or f"---\nname: {name}\n---\nbody {name}\n",
        description=f"desc {name}",
        is_active=is_active,
    )


def _scalars_result(rows: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _prefs_result(document: dict | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = (
        SimpleNamespace(document=document) if document is not None else None
    )
    return result


def _db(*results: object) -> AsyncMock:
    db = AsyncMock(spec=AsyncSession)
    db.execute = AsyncMock(side_effect=list(results))
    return db


# --------------------------------------------------------------------------- #
# _disabled_admin: defensive parsing of user-supplied JSON                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "document,expected",
    [
        ({}, []),
        ({"skills": {"disabled_admin": ["a", "b"]}}, ["a", "b"]),
        ({"skills": {}}, []),
        ({"skills": {"disabled_admin": []}}, []),
        # Malformed shapes degrade to "nothing disabled" rather than raising.
        ({"skills": "nope"}, []),
        ({"skills": {"disabled_admin": "a"}}, []),
        ({"skills": {"disabled_admin": ["a", 1, None, "b"]}}, ["a", "b"]),
        ({"skills": None}, []),
    ],
)
def test_disabled_admin_parsing(document: dict, expected: list[str]) -> None:
    assert _disabled_admin(document) == expected


# --------------------------------------------------------------------------- #
# get_effective_skills                                                         #
# --------------------------------------------------------------------------- #


async def test_none_user_returns_admin_active_sorted() -> None:
    db = _db(_scalars_result([_skill("beta"), _skill("alpha")]))
    result = await get_effective_skills(db, None)
    assert [s.name for s in result] == ["alpha", "beta"]
    db.execute.assert_awaited_once()  # no prefs/personal queries for an ownerless turn


async def test_personal_shadows_admin_same_name() -> None:
    admin = _scalars_result([_skill("shared", content="ADMIN")])
    prefs = _prefs_result({})
    personal = _scalars_result([_skill("shared", content="PERSONAL")])
    result = await get_effective_skills(_db(admin, prefs, personal), _USER)
    assert len(result) == 1
    assert result[0].content == "PERSONAL"  # personal always wins


async def test_disabled_admin_skill_dropped() -> None:
    admin = _scalars_result([_skill("keep"), _skill("drop")])
    prefs = _prefs_result({"skills": {"disabled_admin": ["drop"]}})
    personal = _scalars_result([])
    result = await get_effective_skills(_db(admin, prefs, personal), _USER)
    assert [s.name for s in result] == ["keep"]


async def test_personal_survives_even_if_admin_name_disabled() -> None:
    # User disabled the admin "x" AND authored a personal "x": the personal one stands.
    admin = _scalars_result([_skill("x", content="ADMIN")])
    prefs = _prefs_result({"skills": {"disabled_admin": ["x"]}})
    personal = _scalars_result([_skill("x", content="PERSONAL")])
    result = await get_effective_skills(_db(admin, prefs, personal), _USER)
    assert [s.name for s in result] == ["x"]
    assert result[0].content == "PERSONAL"


async def test_merge_ordered_by_name() -> None:
    admin = _scalars_result([_skill("m-admin"), _skill("z-admin")])
    prefs = _prefs_result({})
    personal = _scalars_result([_skill("a-personal")])
    result = await get_effective_skills(_db(admin, prefs, personal), _USER)
    assert [s.name for s in result] == ["a-personal", "m-admin", "z-admin"]


async def test_no_prefs_row_keeps_all_admin() -> None:
    admin = _scalars_result([_skill("a"), _skill("b")])
    prefs = _prefs_result(None)  # user never saved preferences
    personal = _scalars_result([])
    result = await get_effective_skills(_db(admin, prefs, personal), _USER)
    assert [s.name for s in result] == ["a", "b"]


async def test_malformed_document_does_not_disable_anything() -> None:
    admin = _scalars_result([_skill("a"), _skill("b")])
    prefs = _prefs_result({"skills": {"disabled_admin": "not-a-list"}})
    personal = _scalars_result([])
    result = await get_effective_skills(_db(admin, prefs, personal), _USER)
    assert [s.name for s in result] == ["a", "b"]
