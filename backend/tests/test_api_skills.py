"""Tests for skill API routes."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_skills_empty(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get("/api/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_skills_returns_items(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_skill: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [mock_skill]
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get("/api/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "test-template"


@pytest.mark.asyncio
async def test_create_skill_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.post(
        "/api/skills",
        json={"content": "---\nname: x\n---\nbody"},
    )
    # Regular user -> 403 (admin required via dependency)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_skill_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_skill: MagicMock
) -> None:
    # No existing template with that name
    no_result = MagicMock()
    no_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=no_result)
    mock_db.flush = AsyncMock()

    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_skill.id
        obj.name = "new-skill"
        obj.content = "---\nname: new-skill\ndescription: New\n---\nDo $ARGUMENTS.\n"
        obj.description = "New"
        obj.is_active = True
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    # Frontmatter name "New Skill" → slug "new-skill".
    resp = await admin_client.post(
        "/api/skills",
        json={"content": "---\nname: New Skill\ndescription: New\n---\nDo $ARGUMENTS."},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "new-skill"


@pytest.mark.asyncio
async def test_create_skill_conflict(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_skill: MagicMock
) -> None:
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = mock_skill
    mock_db.execute = AsyncMock(return_value=existing_result)

    resp = await admin_client.post(
        "/api/skills",
        json={"content": "---\nname: test-template\n---\nx"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_skill_invalid_frontmatter(admin_client: AsyncClient) -> None:
    # No frontmatter → 422 before any DB work.
    resp = await admin_client.post("/api/skills", json={"content": "no frontmatter here"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_skill_not_found(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    tid = uuid.uuid4()
    resp = await authenticated_client.get(f"/api/skills/{tid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_skill_success(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_skill: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_skill
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get(f"/api/skills/{mock_skill.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-template"


@pytest.mark.asyncio
async def test_update_skill_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    tid = uuid.uuid4()
    resp = await authenticated_client.patch(
        f"/api/skills/{tid}",
        json={"content": "---\nname: x\n---\nbody"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_skill_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_skill: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_skill
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    # Same slug ("test-template") → no rename-conflict lookup.
    resp = await admin_client.patch(
        f"/api/skills/{mock_skill.id}",
        json={"content": "---\nname: test-template\ndescription: Updated\n---\nDo $ARGUMENTS."},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_skill_rename_conflict(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_skill: MagicMock
) -> None:
    """Renaming to a name owned by another template is rejected."""
    target_result = MagicMock()
    target_result.scalar_one_or_none.return_value = mock_skill

    other = MagicMock()
    other.id = uuid.uuid4()
    conflict_result = MagicMock()
    conflict_result.scalar_one_or_none.return_value = other

    # First execute() loads the template, second checks the new name.
    mock_db.execute = AsyncMock(side_effect=[target_result, conflict_result])

    resp = await admin_client.patch(
        f"/api/skills/{mock_skill.id}",
        json={"content": "---\nname: already-taken\n---\nx"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_skill_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    tid = uuid.uuid4()
    resp = await authenticated_client.delete(f"/api/skills/{tid}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_skill_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_skill: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_skill
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.delete = AsyncMock()
    mock_db.flush = AsyncMock()

    resp = await admin_client.delete(f"/api/skills/{mock_skill.id}")
    assert resp.status_code == 204
    # Must be a hard delete, not an is_active flip
    mock_db.delete.assert_awaited_once_with(mock_skill)
