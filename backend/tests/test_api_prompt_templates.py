"""Tests for prompt template API routes."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_prompt_templates_empty(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get("/api/prompt-templates")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_prompt_templates_returns_items(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_prompt_template: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [mock_prompt_template]
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get("/api/prompt-templates")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "test-template"


@pytest.mark.asyncio
async def test_create_prompt_template_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.post(
        "/api/prompt-templates",
        json={"name": "new-template", "content": "Do __________."},
    )
    # Regular user -> 403 (admin required via dependency)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_prompt_template_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_prompt_template: MagicMock
) -> None:
    # No existing template with that name
    no_result = MagicMock()
    no_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=no_result)
    mock_db.flush = AsyncMock()

    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_prompt_template.id
        obj.name = "new-template"
        obj.content = "Do __________."
        obj.description = None
        obj.is_active = True
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    resp = await admin_client.post(
        "/api/prompt-templates",
        json={"name": "new-template", "content": "Do __________."},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "new-template"


@pytest.mark.asyncio
async def test_create_prompt_template_conflict(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_prompt_template: MagicMock
) -> None:
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = mock_prompt_template
    mock_db.execute = AsyncMock(return_value=existing_result)

    resp = await admin_client.post(
        "/api/prompt-templates",
        json={"name": "test-template", "content": "Anything"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_prompt_template_not_found(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    tid = uuid.uuid4()
    resp = await authenticated_client.get(f"/api/prompt-templates/{tid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_prompt_template_success(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_prompt_template: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_prompt_template
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get(f"/api/prompt-templates/{mock_prompt_template.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-template"


@pytest.mark.asyncio
async def test_update_prompt_template_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    tid = uuid.uuid4()
    resp = await authenticated_client.patch(
        f"/api/prompt-templates/{tid}",
        json={"description": "Updated"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_prompt_template_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_prompt_template: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_prompt_template
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    resp = await admin_client.patch(
        f"/api/prompt-templates/{mock_prompt_template.id}",
        json={"description": "Updated description"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_prompt_template_rename_conflict(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_prompt_template: MagicMock
) -> None:
    """Renaming to a name owned by another template is rejected."""
    target_result = MagicMock()
    target_result.scalar_one_or_none.return_value = mock_prompt_template

    other = MagicMock()
    other.id = uuid.uuid4()
    conflict_result = MagicMock()
    conflict_result.scalar_one_or_none.return_value = other

    # First execute() loads the template, second checks the new name.
    mock_db.execute = AsyncMock(side_effect=[target_result, conflict_result])

    resp = await admin_client.patch(
        f"/api/prompt-templates/{mock_prompt_template.id}",
        json={"name": "already-taken"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_prompt_template_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    tid = uuid.uuid4()
    resp = await authenticated_client.delete(f"/api/prompt-templates/{tid}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_prompt_template_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_prompt_template: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_prompt_template
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()

    resp = await admin_client.delete(f"/api/prompt-templates/{mock_prompt_template.id}")
    assert resp.status_code == 204
