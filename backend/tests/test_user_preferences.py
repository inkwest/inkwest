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
    ConfigImportError,
    _disabled_admin,
    get_effective_skills,
    plan_import,
)

_SKILL_A = "---\nname: a\n---\nbody a\n"
_SKILL_B = "---\nname: b\n---\nbody b\n"

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


# --------------------------------------------------------------------------- #
# plan_import (pure validation / merge / quota)                               #
# --------------------------------------------------------------------------- #


def _plan(**over: object):
    kwargs: dict[str, object] = dict(
        schema_version=1,
        document={},
        raw_skills=[(_SKILL_A, True), (_SKILL_B, True)],
        replace_all=False,
        existing_active={},
        admin_names=set(),
    )
    kwargs.update(over)
    return plan_import(**kwargs)  # type: ignore[arg-type]


def test_plan_import_basic_upsert() -> None:
    plan = _plan(document={"ui": {"x": 1}})
    assert [p.name for p in plan.skills] == ["a", "b"]
    assert all(p.is_active and not p.is_override for p in plan.skills)
    assert plan.document == {"ui": {"x": 1}}
    assert plan.replace_all is False


def test_plan_import_recomputes_is_override_against_admin() -> None:
    plan = _plan(admin_names={"a"})
    overrides = {p.name: p.is_override for p in plan.skills}
    assert overrides == {"a": True, "b": False}


def test_plan_import_preserves_is_active_flag() -> None:
    plan = _plan(raw_skills=[(_SKILL_A, True), (_SKILL_B, False)])
    actives = {p.name: p.is_active for p in plan.skills}
    assert actives == {"a": True, "b": False}


def test_plan_import_round_trip_is_idempotent() -> None:
    # EXTRA CARE: an export's contents re-plan to the exact same normalized contents.
    plan = _plan()
    raw2 = [(p.content, p.is_active) for p in plan.skills]
    plan2 = _plan(raw_skills=raw2)
    assert [(p.name, p.content, p.is_active) for p in plan2.skills] == [
        (p.name, p.content, p.is_active) for p in plan.skills
    ]


def test_plan_import_rejects_unknown_schema_version() -> None:
    with pytest.raises(ConfigImportError) as ei:
        _plan(schema_version=999)
    assert ei.value.status_code == 422


def test_plan_import_rejects_malformed_skill() -> None:
    with pytest.raises(ConfigImportError) as ei:
        _plan(raw_skills=[("no frontmatter here", True)])
    assert ei.value.status_code == 422


def test_plan_import_rejects_duplicate_slug() -> None:
    with pytest.raises(ConfigImportError) as ei:
        _plan(raw_skills=[(_SKILL_A, True), (_SKILL_A, True)])
    assert ei.value.status_code == 422
    assert "duplicate" in ei.value.detail.lower()


def test_plan_import_rejects_oversized_skill() -> None:
    big = "---\nname: big\n---\n" + "x" * (64 * 1024 + 10)
    with pytest.raises(ConfigImportError) as ei:
        _plan(raw_skills=[(big, True)])
    assert ei.value.status_code == 413


def test_plan_import_rejects_oversized_document() -> None:
    with pytest.raises(ConfigImportError) as ei:
        _plan(document={"blob": "x" * (256 * 1024 + 10)}, raw_skills=[])
    assert ei.value.status_code == 413


def test_plan_import_quota_replace_all() -> None:
    raw = [(f"---\nname: s{i}\n---\nb\n", True) for i in range(51)]
    with pytest.raises(ConfigImportError) as ei:
        _plan(raw_skills=raw, replace_all=True)
    assert ei.value.status_code == 409


def test_plan_import_quota_upsert_counts_existing() -> None:
    existing = {f"e{i}": True for i in range(50)}  # already at the cap
    with pytest.raises(ConfigImportError) as ei:
        _plan(raw_skills=[(_SKILL_A, True)], existing_active=existing)
    assert ei.value.status_code == 409


def test_plan_import_upsert_overwrite_does_not_double_count() -> None:
    # Re-importing the same active name over an existing active one stays within quota.
    existing = {f"e{i}": True for i in range(49)} | {"a": True}  # 50 active incl "a"
    plan = _plan(raw_skills=[(_SKILL_A, True)], existing_active=existing)
    assert [p.name for p in plan.skills] == ["a"]


def test_plan_import_inactive_skills_do_not_count_against_quota() -> None:
    raw = [(f"---\nname: s{i}\n---\nb\n", False) for i in range(100)]
    plan = _plan(raw_skills=raw, replace_all=True)  # 100 skills but all inactive → OK
    assert len(plan.skills) == 100
