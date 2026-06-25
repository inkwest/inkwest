"""Tests for the current-user self-service routes (/api/me): preferences + personal skills."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

_SKILL_MD = "---\nname: my-skill\ndescription: Mine\n---\nDo $ARGUMENTS.\n"


def _result(
    *, scalar_one_or_none: object = None, scalars_all: list | None = None, scalar_one: object = None
) -> MagicMock:
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar_one_or_none
    m.scalars.return_value.all.return_value = scalars_all or []
    if scalar_one is not None:
        m.scalar_one.return_value = scalar_one
    return m


def _user_skill(name: str = "my-skill", *, is_override: bool = False, content: str | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        name=name,
        content=content or f"---\nname: {name}\n---\nbody\n",
        description="desc",
        is_active=True,
        is_override=is_override,
        source_admin_skill_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _admin_skill(name: str = "my-skill"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        content=f"---\nname: {name}\ndescription: Admin\n---\nAdmin body.\n",
        description="Admin",
    )


def _refresh_stamp() -> AsyncMock:
    """Emulate the DB applying server defaults on refresh (id, is_active, timestamps)."""

    def _refresh(obj: object) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        if getattr(obj, "is_active", None) is None:
            obj.is_active = True  # type: ignore[attr-defined]
        obj.created_at = datetime.now(UTC)  # type: ignore[attr-defined]
        obj.updated_at = datetime.now(UTC)  # type: ignore[attr-defined]

    return AsyncMock(side_effect=_refresh)


# --------------------------------------------------------------------------- #
# Preferences                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_get_preferences_defaults_when_unset(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(side_effect=[_result(scalar_one_or_none=None), _result()])
    resp = await authenticated_client.get("/api/me/preferences")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"schema_version": 1, "document": {}, "skills": []}


@pytest.mark.asyncio
async def test_get_preferences_with_row_and_skills(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    prefs = SimpleNamespace(schema_version=1, document={"ui": {"wideComposer": True}})
    mock_db.execute = AsyncMock(
        side_effect=[_result(scalar_one_or_none=prefs), _result(scalars_all=[_user_skill()])]
    )
    resp = await authenticated_client.get("/api/me/preferences")
    assert resp.status_code == 200
    body = resp.json()
    assert body["document"] == {"ui": {"wideComposer": True}}
    assert body["skills"][0]["name"] == "my-skill"


@pytest.mark.asyncio
async def test_put_preferences_ok(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    saved = SimpleNamespace(schema_version=1, document={"ui": {"showTimestamps": True}})
    mock_db.execute = AsyncMock(side_effect=[_result(scalar_one=saved), _result()])
    mock_db.flush = AsyncMock()
    resp = await authenticated_client.put(
        "/api/me/preferences", json={"document": {"ui": {"showTimestamps": True}}}
    )
    assert resp.status_code == 200
    assert resp.json()["document"] == {"ui": {"showTimestamps": True}}


@pytest.mark.asyncio
async def test_put_preferences_too_large(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    huge = {"ui": {"blob": "x" * (256 * 1024 + 10)}}
    resp = await authenticated_client.put("/api/me/preferences", json={"document": huge})
    assert resp.status_code == 413
    mock_db.execute.assert_not_called()  # rejected before any DB work


# --------------------------------------------------------------------------- #
# Personal skills CRUD                                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_my_skills(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    mock_db.execute = AsyncMock(return_value=_result(scalars_all=[_user_skill()]))
    resp = await authenticated_client.get("/api/me/skills")
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "my-skill"


@pytest.mark.asyncio
async def test_create_skill_ok_not_override(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    # existing-name check (None), active-count (0), admin-shadow check (None) → not override.
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalar_one_or_none=None),
            _result(scalar_one=0),
            _result(scalar_one_or_none=None),
        ]
    )
    mock_db.flush = AsyncMock()
    mock_db.refresh = _refresh_stamp()
    resp = await authenticated_client.post("/api/me/skills", json={"content": _SKILL_MD})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "my-skill"
    assert body["is_override"] is False


@pytest.mark.asyncio
async def test_create_skill_marks_override_when_admin_name_exists(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalar_one_or_none=None),
            _result(scalar_one=0),
            _result(scalar_one_or_none=_admin_skill()),  # admin skill with same name exists
        ]
    )
    mock_db.flush = AsyncMock()
    mock_db.refresh = _refresh_stamp()
    resp = await authenticated_client.post("/api/me/skills", json={"content": _SKILL_MD})
    assert resp.status_code == 201
    assert resp.json()["is_override"] is True


@pytest.mark.asyncio
async def test_create_skill_invalid_frontmatter(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.post("/api/me/skills", json={"content": "no frontmatter"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_skill_duplicate(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(return_value=_result(scalar_one_or_none=_user_skill()))
    resp = await authenticated_client.post("/api/me/skills", json={"content": _SKILL_MD})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_skill_too_large(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    big = "---\nname: big\n---\n" + "x" * (64 * 1024 + 10)
    resp = await authenticated_client.post("/api/me/skills", json={"content": big})
    assert resp.status_code == 413
    mock_db.execute.assert_not_called()  # size checked before DB work


@pytest.mark.asyncio
async def test_create_skill_quota_exceeded(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    # existing-name None, then active-count at the cap (50) → 409.
    mock_db.execute = AsyncMock(
        side_effect=[_result(scalar_one_or_none=None), _result(scalar_one=50)]
    )
    resp = await authenticated_client.post("/api/me/skills", json={"content": _SKILL_MD})
    assert resp.status_code == 409
    assert "quota" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_skill_race_returns_409(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    # Pre-checks pass (None existing, count 0, no admin shadow) but a concurrent insert
    # already took the name → the UNIQUE constraint fires on flush; surface 409 not 500.
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalar_one_or_none=None),
            _result(scalar_one=0),
            _result(scalar_one_or_none=None),
        ]
    )
    mock_db.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, Exception("dup key")))
    resp = await authenticated_client.post("/api/me/skills", json={"content": _SKILL_MD})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_deactivate_skill_does_not_check_quota(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    skill = _user_skill(name="my-skill")  # currently active
    # Only the owned-fetch query runs; no count query (deactivating never hits the quota).
    mock_db.execute = AsyncMock(return_value=_result(scalar_one_or_none=skill))
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()
    resp = await authenticated_client.put(f"/api/me/skills/{skill.id}", json={"is_active": False})
    assert resp.status_code == 200
    assert mock_db.execute.await_count == 1


@pytest.mark.asyncio
async def test_update_skill_ok(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    skill = _user_skill(name="my-skill")
    # owned-fetch, then admin-shadow re-derivation (same slug → no rename conflict lookup).
    mock_db.execute = AsyncMock(
        side_effect=[_result(scalar_one_or_none=skill), _result(scalar_one_or_none=None)]
    )
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()
    resp = await authenticated_client.put(
        f"/api/me/skills/{skill.id}",
        json={"content": "---\nname: my-skill\ndescription: Updated\n---\nNew body.\n"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_skill_not_found_or_not_owned(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    # The owned-fetch query filters by user_id → another user's row simply isn't found.
    mock_db.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = await authenticated_client.put(
        f"/api/me/skills/{uuid.uuid4()}", json={"content": _SKILL_MD}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_skill_rename_conflict(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    skill = _user_skill(name="my-skill")
    other = _user_skill(name="taken")
    mock_db.execute = AsyncMock(
        side_effect=[_result(scalar_one_or_none=skill), _result(scalar_one_or_none=other)]
    )
    resp = await authenticated_client.put(
        f"/api/me/skills/{skill.id}", json={"content": "---\nname: taken\n---\nx"}
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_reactivating_skill_respects_quota(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    skill = _user_skill(name="my-skill")
    skill.is_active = False  # currently inactive; re-activating must check the quota
    mock_db.execute = AsyncMock(
        side_effect=[_result(scalar_one_or_none=skill), _result(scalar_one=50)]
    )
    resp = await authenticated_client.put(f"/api/me/skills/{skill.id}", json={"is_active": True})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_skill_ok(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    skill = _user_skill()
    mock_db.execute = AsyncMock(return_value=_result(scalar_one_or_none=skill))
    mock_db.delete = AsyncMock()
    mock_db.flush = AsyncMock()
    resp = await authenticated_client.delete(f"/api/me/skills/{skill.id}")
    assert resp.status_code == 204
    mock_db.delete.assert_awaited_once_with(skill)  # hard delete


@pytest.mark.asyncio
async def test_delete_skill_not_found(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = await authenticated_client.delete(f"/api/me/skills/{uuid.uuid4()}")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Override                                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_override_admin_skill_ok(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    admin = _admin_skill(name="mr-helm-review")
    # admin lookup, existing-personal check (None), active-count (0).
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalar_one_or_none=admin),
            _result(scalar_one_or_none=None),
            _result(scalar_one=0),
        ]
    )
    mock_db.flush = AsyncMock()
    mock_db.refresh = _refresh_stamp()
    resp = await authenticated_client.post("/api/me/skills/override/mr-helm-review")
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "mr-helm-review"
    assert body["is_override"] is True
    assert body["source_admin_skill_id"] == str(admin.id)


@pytest.mark.asyncio
async def test_override_resolves_unslugified_name(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    # Caller passes a human/cased name; the route slugifies before lookup so it resolves.
    admin = _admin_skill(name="mr-helm-review")
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalar_one_or_none=admin),
            _result(scalar_one_or_none=None),
            _result(scalar_one=0),
        ]
    )
    mock_db.flush = AsyncMock()
    mock_db.refresh = _refresh_stamp()
    resp = await authenticated_client.post("/api/me/skills/override/MR Helm Review")
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_override_quota_exceeded(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    admin = _admin_skill(name="mr-helm-review")
    # admin found, no existing personal copy, but active count at cap → 409.
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalar_one_or_none=admin),
            _result(scalar_one_or_none=None),
            _result(scalar_one=50),
        ]
    )
    resp = await authenticated_client.post("/api/me/skills/override/mr-helm-review")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_override_admin_skill_not_found(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    resp = await authenticated_client.post("/api/me/skills/override/nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_override_when_already_overridden(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    admin = _admin_skill(name="mr-helm-review")
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalar_one_or_none=admin),
            _result(scalar_one_or_none=_user_skill(name="mr-helm-review")),
        ]
    )
    resp = await authenticated_client.post("/api/me/skills/override/mr-helm-review")
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# Export / import                                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_export_config(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    prefs = SimpleNamespace(schema_version=1, document={"ui": {"wideComposer": True}})
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalar_one_or_none=prefs),
            _result(scalars_all=[_user_skill(name="a", is_override=True)]),
        ]
    )
    resp = await authenticated_client.get("/api/me/export")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert "exported_at" in body
    assert body["preferences"] == {"ui": {"wideComposer": True}}
    assert body["skills"][0]["name"] == "a"
    assert body["skills"][0]["is_override"] is True


@pytest.mark.asyncio
async def test_import_config_upsert(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    saved = SimpleNamespace(schema_version=1, document={"ui": {"showTimestamps": True}})
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalars_all=[]),  # existing user skills
            _result(scalars_all=[]),  # admin names
            _result(scalar_one=saved),  # set_preferences upsert returning
            _result(scalars_all=[_user_skill(name="a")]),  # final skills
        ]
    )
    mock_db.add = MagicMock()
    mock_db.flush = AsyncMock()
    payload = {
        "schema_version": 1,
        "preferences": {"ui": {"showTimestamps": True}},
        "skills": [{"name": "a", "content": _SKILL_MD, "is_active": True, "is_override": False}],
    }
    resp = await authenticated_client.post("/api/me/import", json=payload)
    assert resp.status_code == 200
    assert resp.json()["skills"][0]["name"] == "a"
    mock_db.add.assert_called_once()  # one new skill inserted


@pytest.mark.asyncio
async def test_import_config_replace_all_deletes_existing(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    old = _user_skill(name="old")
    saved = SimpleNamespace(schema_version=1, document={})
    mock_db.execute = AsyncMock(
        side_effect=[
            _result(scalars_all=[old]),  # existing skills (to be wiped)
            _result(scalars_all=[]),  # admin names
            _result(scalar_one=saved),  # set_preferences
            _result(scalars_all=[_user_skill(name="a")]),  # final
        ]
    )
    mock_db.add = MagicMock()
    mock_db.delete = AsyncMock()
    mock_db.flush = AsyncMock()
    payload = {"schema_version": 1, "preferences": {}, "skills": [{"content": _SKILL_MD}]}
    resp = await authenticated_client.post("/api/me/import?replace_all=true", json=payload)
    assert resp.status_code == 200
    mock_db.delete.assert_awaited_once_with(old)  # old skill wiped first


@pytest.mark.asyncio
async def test_import_rejects_unknown_schema_version(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(side_effect=[_result(scalars_all=[]), _result(scalars_all=[])])
    payload = {"schema_version": 999, "preferences": {}, "skills": []}
    resp = await authenticated_client.post("/api/me/import", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_import_rejects_malformed_skill(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    mock_db.execute = AsyncMock(side_effect=[_result(scalars_all=[]), _result(scalars_all=[])])
    payload = {"schema_version": 1, "preferences": {}, "skills": [{"content": "no frontmatter"}]}
    resp = await authenticated_client.post("/api/me/import", json=payload)
    assert resp.status_code == 422
