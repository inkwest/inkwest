"""Tests for admin API routes."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient

from app.models.budget import BudgetPeriod, BudgetType

# ============================================================================
# Non-admin access is forbidden
# ============================================================================


@pytest.mark.asyncio
async def test_budgets_forbidden_for_non_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/admin/budgets")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_role_mappings_forbidden_for_non_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/admin/role-mappings")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_users_forbidden_for_non_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/admin/users")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_usage_summary_forbidden_for_non_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/admin/usage/summary")
    assert resp.status_code == 403


# ============================================================================
# Budget CRUD (admin)
# ============================================================================


@pytest.mark.asyncio
async def test_list_budgets_empty(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await admin_client.get("/api/admin/budgets")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_budget(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    budget = MagicMock()
    budget.id = uuid.uuid4()
    budget.name = "daily-limit"
    budget.description = None
    budget.budget_type = BudgetType.GLOBAL
    budget.target_id = None
    budget.target_role = None
    budget.max_input_tokens = 50000
    budget.max_output_tokens = None
    budget.max_total_tokens = None
    budget.max_cost_usd = None
    budget.period = BudgetPeriod.DAILY
    budget.soft_limit_percent = 80
    budget.hard_limit_action = "block"
    budget.is_active = True
    budget.created_at = datetime.now(UTC)
    budget.updated_at = datetime.now(UTC)

    mock_db.commit = AsyncMock()
    mock_db.refresh = AsyncMock()

    def set_budget_attrs(obj: MagicMock) -> None:
        for attr in (
            "id",
            "name",
            "description",
            "budget_type",
            "target_id",
            "target_role",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
            "max_cost_usd",
            "period",
            "soft_limit_percent",
            "hard_limit_action",
            "is_active",
            "created_at",
            "updated_at",
        ):
            setattr(obj, attr, getattr(budget, attr))

    mock_db.refresh = AsyncMock(side_effect=set_budget_attrs)

    resp = await admin_client.post(
        "/api/admin/budgets",
        json={
            "name": "daily-limit",
            "budget_type": "global",
            "max_input_tokens": 50000,
            "period": "daily",
        },
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_get_budget_not_found(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    bid = uuid.uuid4()
    resp = await admin_client.get(f"/api/admin/budgets/{bid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_budget_not_found(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    bid = uuid.uuid4()
    resp = await admin_client.delete(f"/api/admin/budgets/{bid}")
    assert resp.status_code == 404


# ============================================================================
# Role Mapping CRUD (admin)
# ============================================================================


@pytest.mark.asyncio
async def test_list_role_mappings_empty(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await admin_client.get("/api/admin/role-mappings")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_role_mapping_conflict(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    existing = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await admin_client.post(
        "/api/admin/role-mappings",
        json={"role_name": "viewer"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_role_mapping_not_found(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    mid = uuid.uuid4()
    resp = await admin_client.delete(f"/api/admin/role-mappings/{mid}")
    assert resp.status_code == 404


# ============================================================================
# User Management (admin)
# ============================================================================


@pytest.mark.asyncio
async def test_list_users_empty(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await admin_client.get("/api/admin/users")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_user_not_found(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    uid = uuid.uuid4()
    resp = await admin_client.get(f"/api/admin/users/{uid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_user_roles_not_found(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    uid = uuid.uuid4()
    resp = await admin_client.put(
        f"/api/admin/users/{uid}/roles",
        json={"roles": ["admin"]},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_user_status_not_found(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    uid = uuid.uuid4()
    resp = await admin_client.put(
        f"/api/admin/users/{uid}/status",
        json={"is_active": False},
    )
    assert resp.status_code == 404


# ============================================================================
# Usage Reports (admin)
# ============================================================================


@pytest.mark.asyncio
async def test_usage_summary(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    row = (5000, 3000, 0.12, 10)
    mock_result = MagicMock()
    mock_result.one.return_value = row
    mock_db.execute = AsyncMock(return_value=mock_result)

    resp = await admin_client.get("/api/admin/usage/summary")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_tokens"] == 8000
    assert data["request_count"] == 10


@pytest.mark.asyncio
async def test_usage_by_user(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

    resp = await admin_client.get("/api/admin/usage/by-user")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_usage_by_model(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

    resp = await admin_client.get("/api/admin/usage/by-model")
    assert resp.status_code == 200


# ============================================================================
# Model Settings (admin)
# ============================================================================


@pytest.fixture
def reset_model_settings():
    """Restore the in-memory model settings mutated by PUT tests."""
    from app.services import app_settings

    yield
    app_settings._model_override = None
    app_settings._enabled_models = None


@pytest.mark.asyncio
async def test_model_settings_forbidden_for_non_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/admin/settings/model")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_model_settings(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/api/admin/settings/model")

    assert resp.status_code == 200
    data = resp.json()
    assert data["default_model"] in data["enabled_models"]
    available_ids = [m["id"] for m in data["available_models"]]
    # Nothing configured yet: all available models are enabled
    assert data["enabled_models"] == available_ids
    # Bedrock model IDs must not be offered
    assert not any(m.startswith("anthropic.") for m in available_ids)
    # Human-friendly labels, not internal IDs
    labels = {m["id"]: m["label"] for m in data["available_models"]}
    assert labels["claude-sonnet-4-20250514"] == "Sonnet 4.0"
    assert labels["claude-3-5-sonnet-20241022"] == "Sonnet 3.5"
    # Older generations carry the legacy flag, current ones don't
    legacy = {m["id"]: m["legacy"] for m in data["available_models"]}
    assert legacy["claude-opus-4-8"] is False
    assert legacy["claude-sonnet-4-20250514"] is True


@pytest.mark.asyncio
async def test_update_model_settings(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock())

    enabled = ["claude-opus-4-20250514", "claude-sonnet-4-20250514"]
    resp = await admin_client.put(
        "/api/admin/settings/model",
        json={"default_model": "claude-opus-4-20250514", "enabled_models": enabled},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["default_model"] == "claude-opus-4-20250514"
    assert data["enabled_models"] == enabled
    assert mock_db.execute.await_count == 2  # one ON CONFLICT upsert per settings key
    mock_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_update_model_settings_unknown_model(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    resp = await admin_client.put(
        "/api/admin/settings/model",
        json={"default_model": "gpt-4o", "enabled_models": ["gpt-4o"]},
    )
    assert resp.status_code == 400
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_model_settings_default_not_enabled(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    resp = await admin_client.put(
        "/api/admin/settings/model",
        json={
            "default_model": "claude-opus-4-20250514",
            "enabled_models": ["claude-sonnet-4-20250514"],
        },
    )
    assert resp.status_code == 400
    assert "default model" in resp.json()["detail"].lower()
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_model_settings_empty_enabled_list(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    resp = await admin_client.put(
        "/api/admin/settings/model",
        json={"default_model": "claude-sonnet-4-20250514", "enabled_models": []},
    )
    assert resp.status_code == 422  # rejected by schema (min_length=1)
    mock_db.commit.assert_not_awaited()
