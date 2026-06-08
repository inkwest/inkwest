"""Tests for system prompt API routes and the session-creation guard."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_system_prompts_empty(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get("/api/system-prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_system_prompts_returns_items(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [mock_system_prompt]
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get("/api/system-prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "test-system-prompt"
    assert data["items"][0]["is_default"] is False


@pytest.mark.asyncio
async def test_create_system_prompt_requires_admin(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.post(
        "/api/system-prompts",
        json={"name": "new-prompt", "content": "You are helpful."},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_system_prompt_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    no_result = MagicMock()
    no_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=no_result)
    mock_db.flush = AsyncMock()

    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_system_prompt.id
        obj.name = "new-prompt"
        obj.content = "You are helpful."
        obj.description = None
        obj.is_active = True
        obj.is_default = False
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    resp = await admin_client.post(
        "/api/system-prompts",
        json={"name": "new-prompt", "content": "You are helpful."},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "new-prompt"


@pytest.mark.asyncio
async def test_create_system_prompt_default_clears_previous(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    """Creating a prompt with is_default=true demotes the previous default first."""
    no_result = MagicMock()
    no_result.scalar_one_or_none.return_value = None
    # 1: name-uniqueness check, 2: _clear_default UPDATE
    mock_db.execute = AsyncMock(side_effect=[no_result, MagicMock()])
    mock_db.flush = AsyncMock()

    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_system_prompt.id
        obj.name = "the-default"
        obj.content = "Default body."
        obj.description = None
        obj.is_active = True
        obj.is_default = True
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    resp = await admin_client.post(
        "/api/system-prompts",
        json={"name": "the-default", "content": "Default body.", "is_default": True},
    )
    assert resp.status_code == 201
    assert resp.json()["is_default"] is True
    # name check + clear-default
    assert mock_db.execute.await_count == 2


@pytest.mark.asyncio
async def test_create_system_prompt_conflict(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = mock_system_prompt
    mock_db.execute = AsyncMock(return_value=existing_result)

    resp = await admin_client.post(
        "/api/system-prompts",
        json={"name": "test-system-prompt", "content": "Anything"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_get_system_prompt_not_found(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get(f"/api/system-prompts/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_system_prompt_requires_admin(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.patch(
        f"/api/system-prompts/{uuid.uuid4()}",
        json={"description": "Updated"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_system_prompt_promote_default(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    load_result = MagicMock()
    load_result.scalar_one_or_none.return_value = mock_system_prompt
    # 1: load prompt, 2: _clear_default UPDATE
    mock_db.execute = AsyncMock(side_effect=[load_result, MagicMock()])
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    resp = await admin_client.patch(
        f"/api/system-prompts/{mock_system_prompt.id}",
        json={"is_default": True},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_update_system_prompt_cannot_unset_default(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    mock_system_prompt.is_default = True
    load_result = MagicMock()
    load_result.scalar_one_or_none.return_value = mock_system_prompt
    mock_db.execute = AsyncMock(return_value=load_result)

    resp = await admin_client.patch(
        f"/api/system-prompts/{mock_system_prompt.id}",
        json={"is_default": False},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_update_system_prompt_cannot_deactivate_default(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    mock_system_prompt.is_default = True
    load_result = MagicMock()
    load_result.scalar_one_or_none.return_value = mock_system_prompt
    mock_db.execute = AsyncMock(return_value=load_result)

    resp = await admin_client.patch(
        f"/api/system-prompts/{mock_system_prompt.id}",
        json={"is_active": False},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_system_prompt_requires_admin(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.delete(f"/api/system-prompts/{uuid.uuid4()}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_system_prompt_default_rejected(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    mock_system_prompt.is_default = True
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_system_prompt
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await admin_client.delete(f"/api/system-prompts/{mock_system_prompt.id}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_system_prompt_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_system_prompt: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_system_prompt
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()

    resp = await admin_client.delete(f"/api/system-prompts/{mock_system_prompt.id}")
    assert resp.status_code == 204


# ----------------------------------------------------------------------------
# Session-creation guard: system prompt selection is admin-only.
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_cannot_choose_system_prompt(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.post(
        "/api/sessions",
        json={"system_prompt_custom": "be terse"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_cannot_set_both_id_and_custom(admin_client: AsyncClient) -> None:
    resp = await admin_client.post(
        "/api/sessions",
        json={"system_prompt_id": str(uuid.uuid4()), "system_prompt_custom": "be terse"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_admin_unknown_system_prompt_id(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await admin_client.post(
        "/api/sessions",
        json={"system_prompt_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404
