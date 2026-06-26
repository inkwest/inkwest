"""Tests for admin API routes."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

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
async def test_list_budget_usage(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    """Usage is reported for user budgets; role/global are per-user quotas → null."""
    from app.services.budget_service import UsageSummary

    user_b = MagicMock()
    user_b.id = uuid.uuid4()
    user_b.budget_type = BudgetType.USER
    user_b.target_id = uuid.uuid4()
    user_b.period = BudgetPeriod.MONTHLY

    global_b = MagicMock()
    global_b.id = uuid.uuid4()
    global_b.budget_type = BudgetType.GLOBAL
    global_b.target_id = None
    global_b.period = BudgetPeriod.MONTHLY

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [user_b, global_b]
    mock_db.execute = AsyncMock(return_value=result_mock)

    usage = UsageSummary(
        total_input_tokens=100,
        total_output_tokens=50,
        total_tokens=150,
        total_cost_usd=Decimal("0.30"),
        request_count=2,
    )
    with patch("app.api.routes.admin.get_usage_for_period", AsyncMock(return_value=usage)):
        resp = await admin_client.get("/api/admin/budgets/usage")

    assert resp.status_code == 200
    data = {row["budget_id"]: row for row in resp.json()}
    assert data[str(user_b.id)]["current_cost_usd"] == 0.30
    assert data[str(user_b.id)]["current_total_tokens"] == 150
    assert data[str(global_b.id)]["current_cost_usd"] is None
    assert data[str(global_b.id)]["current_total_tokens"] is None


@pytest.mark.asyncio
async def test_list_budget_members_role(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    """Role budget members: each role member with their per-member usage (zeros included)."""
    bid = uuid.uuid4()
    budget = MagicMock()
    budget.id = bid
    budget.budget_type = BudgetType.ROLE
    budget.target_role = "engineer"
    budget.period = BudgetPeriod.MONTHLY
    mock_db.get = AsyncMock(return_value=budget)

    u1 = MagicMock(id=uuid.uuid4(), display_name="Alice", email="a@x", roles=["engineer"])
    u2 = MagicMock(id=uuid.uuid4(), display_name="Bob", email="b@x", roles=["engineer"])
    users_result = MagicMock()
    users_result.scalars.return_value.all.return_value = [u1, u2]
    usage_rows = MagicMock()
    usage_rows.all.return_value = [(u1.id, Decimal("0.50"), 1000)]  # only Alice has usage
    mock_db.execute = AsyncMock(side_effect=[users_result, usage_rows])

    resp = await admin_client.get(f"/api/admin/budgets/{bid}/members")
    assert resp.status_code == 200
    rows = resp.json()
    by_id = {m["user_id"]: m for m in rows}
    assert by_id[str(u1.id)]["current_cost_usd"] == 0.5
    assert by_id[str(u1.id)]["current_total_tokens"] == 1000
    assert by_id[str(u2.id)]["current_cost_usd"] == 0.0  # no usage → zero, still listed
    assert rows[0]["user_id"] == str(u1.id)  # sorted by cost desc


@pytest.mark.asyncio
async def test_list_budget_members_user_is_empty(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """A user budget has no member list (its single subject is the row itself)."""
    budget = MagicMock()
    budget.budget_type = BudgetType.USER
    budget.target_id = uuid.uuid4()
    mock_db.get = AsyncMock(return_value=budget)

    resp = await admin_client.get(f"/api/admin/budgets/{uuid.uuid4()}/members")
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

    # No existing active budget for this scope (the duplicate-check query).
    no_dup = MagicMock()
    no_dup.scalars.return_value.first.return_value = None
    mock_db.execute = AsyncMock(return_value=no_dup)

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
async def test_create_budget_duplicate_conflict(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """A second active budget for the same scope+target is rejected with 409."""
    existing = MagicMock()
    dup_result = MagicMock()
    dup_result.scalars.return_value.first.return_value = existing
    mock_db.execute = AsyncMock(return_value=dup_result)
    mock_db.commit = AsyncMock()

    resp = await admin_client.post(
        "/api/admin/budgets",
        json={
            "budget_type": "user",
            "target_id": str(uuid.uuid4()),
            "max_cost_usd": 5,
            "period": "monthly",
        },
    )
    assert resp.status_code == 409
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_derive_budget_name() -> None:
    """Budget name is derived from the scope: user name, role, or global."""
    from app.api.routes.admin import BudgetCreate, _derive_budget_name

    db = AsyncMock()
    user = MagicMock()
    user.display_name = "Toto"
    user.email = "toto@localhost"
    db.get = AsyncMock(return_value=user)

    user_budget = BudgetCreate(budget_type="user", target_id=uuid.uuid4(), max_cost_usd=5)
    assert await _derive_budget_name(db, user_budget) == "Toto"

    role_budget = BudgetCreate(budget_type="role", target_role="ops", max_cost_usd=5)
    assert await _derive_budget_name(db, role_budget) == "ops"

    global_budget = BudgetCreate(budget_type="global", max_cost_usd=5)
    assert await _derive_budget_name(db, global_budget) == "Global default"


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
    """Restore the in-memory registry + settings mutated by write tests."""
    from app.services import app_settings, model_registry

    yield
    model_registry._rows = {}
    app_settings._settings = {}


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
    assert "include_all_vendors" in data
    assert "inkwest_agent_enabled" in data
    assert data["catalog"]["source"] in ("live", "static")
    available_ids = [m["id"] for m in data["models"]]
    # Nothing configured yet: all available models are selectable
    assert data["enabled_models"] == available_ids
    # Bedrock model IDs must not be offered (anthropic provider, static fallback)
    assert not any(m.startswith("anthropic.") for m in available_ids)
    # Human-friendly labels, not internal IDs
    labels = {m["id"]: m["label"] for m in data["models"]}
    assert labels["claude-sonnet-4-20250514"] == "Sonnet 4.0"
    assert labels["claude-3-5-sonnet-20241022"] == "Sonnet 3.5"
    # Older generations carry the legacy flag, current ones don't
    legacy = {m["id"]: m["legacy"] for m in data["models"]}
    assert legacy["claude-opus-4-8"] is False
    assert legacy["claude-sonnet-4-20250514"] is True
    # Provider tagging is present for grouping
    assert all(m["provider"] for m in data["models"])
    # P1.8: modality flags are exposed (empty here — static catalog, no live Bedrock data).
    assert all("input_modalities" in m and "output_modalities" in m for m in data["models"])
    assert all(m["input_modalities"] == [] for m in data["models"])


@pytest.mark.asyncio
async def test_system_info_forbidden_for_non_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/admin/system-info")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_system_info(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/api/admin/system-info")

    assert resp.status_code == 200
    data = resp.json()
    # App image build metadata is always populated (dev/local placeholders otherwise).
    assert data["app_version"]
    assert "app_commit" in data
    assert "app_build_timestamp" in data
    # Agent image ref — the broker's worker image, or '' when the sandbox isn't configured.
    assert isinstance(data["agent_image"], str)
    assert data["ai_provider"] == "claude-cli"
    assert isinstance(data["default_model"], str) and data["default_model"]
    # Runtime/feature state.
    assert isinstance(data["engines"], list)
    assert isinstance(data["oidc_enabled"], bool)
    assert isinstance(data["sandbox_enabled"], bool)
    assert isinstance(data["attachments_enabled"], bool)
    # Catalog freshness block.
    assert data["catalog"]["source"] in ("live", "static")
    assert isinstance(data["catalog"]["last_fetch_failed"], bool)
    assert isinstance(data["catalog"]["count"], int)


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
    assert set(data["enabled_models"]) == set(enabled)
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


@pytest.mark.asyncio
async def test_refresh_model_catalog_sets_open_flag(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock())
    with patch("app.api.routes.admin.refresh_catalog", new=AsyncMock()) as refresh:
        resp = await admin_client.post(
            "/api/admin/settings/model/refresh", json={"include_all_vendors": True}
        )
    assert resp.status_code == 200
    assert resp.json()["include_all_vendors"] is True
    refresh.assert_awaited_once_with(force=True)
    mock_db.commit.assert_awaited()  # set_setting persisted the flag


@pytest.mark.asyncio
async def test_add_custom_model(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock())
    resp = await admin_client.post(
        "/api/admin/models",
        json={
            "id": "meta.llama3-1-70b-instruct-v1:0",
            "provider": "bedrock-open",
            "display_name": "Llama 3.1 70B",
            "arn": "arn:aws:bedrock:eu-west-1::foundation-model/meta.llama3",
            "input_price": 0.72,
            "output_price": 0.72,
            "enabled": True,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    added = next(m for m in data["models"] if m["id"] == "meta.llama3-1-70b-instruct-v1:0")
    assert added["source"] == "custom"
    assert added["provider"] == "bedrock-open"
    assert added["priced"] is True
    assert added["input_price"] == 0.72


@pytest.mark.asyncio
async def test_add_custom_model_rejects_lone_price(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    resp = await admin_client.post(
        "/api/admin/models",
        json={"id": "x", "provider": "bedrock-open", "input_price": 1.0},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_add_custom_model_rejects_non_http_endpoint(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    resp = await admin_client.post(
        "/api/admin/models",
        json={"id": "x", "provider": "bedrock-open", "endpoint": "file:///etc/passwd"},
    )
    assert resp.status_code == 422  # rejected by the endpoint URL validator
    mock_db.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "endpoint",
    ["http://169.254.169.254/latest/meta-data", "http://localhost:8080", "http://10.0.0.5"],
)
@pytest.mark.asyncio
async def test_add_custom_model_rejects_internal_endpoint(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None, endpoint: str
) -> None:
    resp = await admin_client.post(
        "/api/admin/models",
        json={"id": "x", "provider": "bedrock-open", "endpoint": endpoint},
    )
    assert resp.status_code == 422  # SSRF guard rejects internal/metadata targets
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_custom_model_rejects_unknown_provider(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    resp = await admin_client.post("/api/admin/models", json={"id": "x", "provider": "gpt"})
    assert resp.status_code == 422  # provider must be one of the known tags
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_custom_model_rejects_malformed_arn(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    resp = await admin_client.post(
        "/api/admin/models",
        json={"id": "x", "provider": "bedrock-open", "arn": "not-an-arn"},
    )
    assert resp.status_code == 422
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_custom_model_metadata(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock())
    resp = await admin_client.put(
        "/api/admin/models/claude-opus-4-8",
        json={"display_name": "House Opus"},
    )
    assert resp.status_code == 200
    data = resp.json()
    edited = next(m for m in data["models"] if m["id"] == "claude-opus-4-8")
    assert edited["label"] == "House Opus"


@pytest.mark.asyncio
async def test_delete_custom_model(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    from app.services import model_registry

    mock_db.execute = AsyncMock(return_value=MagicMock())
    model_registry._rows = {
        "meta.llama3": {
            "model_id": "meta.llama3",
            "provider": "bedrock-open",
            "display_name": None,
            "endpoint": None,
            "arn": None,
            "options": {},
            "input_price": None,
            "output_price": None,
            "enabled": False,
            "is_default": False,
            "source": "custom",
            "legacy": False,
        }
    }
    resp = await admin_client.request("DELETE", "/api/admin/models/meta.llama3")
    assert resp.status_code == 200
    assert "meta.llama3" not in [m["id"] for m in resp.json()["models"]]


@pytest.mark.asyncio
async def test_delete_discovered_model_rejected(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_model_settings: None
) -> None:
    from app.services import model_registry

    model_registry._rows = {
        "claude-opus-4-8": {
            "model_id": "claude-opus-4-8",
            "provider": "anthropic",
            "display_name": None,
            "endpoint": None,
            "arn": None,
            "options": {},
            "input_price": None,
            "output_price": None,
            "enabled": True,
            "is_default": False,
            "source": "discovered",
            "legacy": False,
        }
    }
    resp = await admin_client.request("DELETE", "/api/admin/models/claude-opus-4-8")
    assert resp.status_code == 400
    assert "cannot be deleted" in resp.json()["detail"].lower()


# ============================================================================
# OAuth2 MCP catalog CRUD (admin)
# ============================================================================


@pytest.mark.asyncio
async def test_mcp_oauth_forbidden_for_non_admin(authenticated_client: AsyncClient) -> None:
    resp = await authenticated_client.get("/api/admin/mcp-oauth")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_mcp_oauth_returns_entries_secret_free(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    entry = {
        "key": "notion",
        "display_name": None,
        "url": "https://m.example.com/mcp",
        "scopes": ["mcp:read"],
        "client_id": None,
        "has_secret": True,
        "editable": True,
        "enabled": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    with patch(
        "app.api.routes.admin.mcp_oauth_catalog.list_oauth_entries",
        new=AsyncMock(return_value=[entry]),
    ):
        resp = await admin_client.get("/api/admin/mcp-oauth")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["key"] == "notion" and body[0]["has_secret"] is True
    assert "client_secret" not in body[0]  # the raw secret is never serialized


@pytest.mark.asyncio
async def test_create_mcp_oauth_server(admin_client: AsyncClient, mock_db: AsyncMock) -> None:
    with (
        patch(
            "app.api.routes.admin.mcp_oauth_catalog.upsert_oauth_server", new=AsyncMock()
        ) as upsert,
        patch(
            "app.api.routes.admin.mcp_oauth_catalog.list_oauth_entries",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await admin_client.post(
            "/api/admin/mcp-oauth",
            json={"key": "gh", "url": "https://gh.example.com/mcp", "scopes": ["repo"]},
        )
    assert resp.status_code == 201
    upsert.assert_awaited_once()
    assert upsert.await_args.kwargs["key"] == "gh"


@pytest.mark.asyncio
async def test_create_mcp_oauth_duplicate_key_is_409(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    from app.services import mcp_oauth_catalog as cat

    with patch(
        "app.api.routes.admin.mcp_oauth_catalog.upsert_oauth_server",
        new=AsyncMock(side_effect=cat.DuplicateKeyError("gh")),
    ):
        resp = await admin_client.post(
            "/api/admin/mcp-oauth", json={"key": "gh", "url": "https://gh.example.com/mcp"}
        )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_mcp_oauth_rejects_bad_url(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    with patch(
        "app.api.routes.admin.mcp_oauth_catalog.upsert_oauth_server",
        new=AsyncMock(side_effect=ValueError("MCP server url must be an https:// URL")),
    ):
        resp = await admin_client.post(
            "/api/admin/mcp-oauth", json={"key": "x", "url": "http://insecure/mcp"}
        )
    assert resp.status_code == 400
    assert "https" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_unknown_mcp_oauth_is_404(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    with patch(
        "app.api.routes.admin.mcp_oauth_catalog.delete_oauth_server",
        new=AsyncMock(side_effect=ValueError("Unknown MCP server key: nope")),
    ):
        resp = await admin_client.request("DELETE", "/api/admin/mcp-oauth/nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mcp_oauth_meta_reports_secret_storage(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    with patch("app.api.routes.admin.mcp_oauth_catalog.secret_storage_enabled", return_value=True):
        resp = await admin_client.get("/api/admin/mcp-oauth/meta")
    assert resp.status_code == 200
    assert resp.json() == {"secret_storage_enabled": True}


@pytest.mark.asyncio
async def test_mcp_oauth_meta_false_when_unconfigured(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    with patch("app.api.routes.admin.mcp_oauth_catalog.secret_storage_enabled", return_value=False):
        resp = await admin_client.get("/api/admin/mcp-oauth/meta")
    assert resp.json() == {"secret_storage_enabled": False}


@pytest.fixture
def reset_app_settings():
    from app.services import app_settings

    yield
    app_settings._settings = {}


@pytest.mark.asyncio
async def test_sandbox_settings_forbidden_for_non_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/admin/settings/sandbox")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_sandbox_settings_default(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/api/admin/settings/sandbox")
    assert resp.status_code == 200
    data = resp.json()
    # No override set -> effective equals the env default (5).
    assert data["max_sessions_per_user"] == 5
    assert data["default_max_sessions_per_user"] == 5
    # Hard ceiling: cap 5 + max(2, 5//4=1) = 7.
    assert data["hard_ceiling"] == 7


@pytest.mark.asyncio
async def test_update_sandbox_settings(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_app_settings: None
) -> None:
    from app.services import app_settings

    mock_db.execute = AsyncMock(return_value=MagicMock())
    resp = await admin_client.put("/api/admin/settings/sandbox", json={"max_sessions_per_user": 12})
    assert resp.status_code == 200
    assert resp.json()["max_sessions_per_user"] == 12
    mock_db.commit.assert_awaited()
    # Effective immediately in memory (no restart).
    assert app_settings.get_int_setting(app_settings.SANDBOX_MAX_SESSIONS_PER_USER_KEY, 0) == 12


@pytest.mark.asyncio
async def test_update_sandbox_settings_allows_zero_unlimited(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_app_settings: None
) -> None:
    mock_db.execute = AsyncMock(return_value=MagicMock())
    resp = await admin_client.put("/api/admin/settings/sandbox", json={"max_sessions_per_user": 0})
    assert resp.status_code == 200
    assert resp.json()["max_sessions_per_user"] == 0


@pytest.mark.asyncio
async def test_update_sandbox_settings_rejects_negative(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    resp = await admin_client.put("/api/admin/settings/sandbox", json={"max_sessions_per_user": -1})
    assert resp.status_code == 422
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_settings_forbidden_for_non_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/admin/settings/budget")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_budget_settings_default(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/api/admin/settings/budget")
    assert resp.status_code == 200
    data = resp.json()
    # No override set -> effective equals the env defaults (off, no cap).
    assert data["enabled"] is False
    assert data["default_monthly_cost_limit_usd"] == 0.0


@pytest.mark.asyncio
async def test_update_budget_settings(
    admin_client: AsyncClient, mock_db: AsyncMock, reset_app_settings: None
) -> None:
    from app.services import budget_service

    mock_db.execute = AsyncMock(return_value=MagicMock())
    resp = await admin_client.put(
        "/api/admin/settings/budget",
        json={"enabled": True, "default_monthly_cost_limit_usd": 25.5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["default_monthly_cost_limit_usd"] == 25.5
    mock_db.commit.assert_awaited()
    # Effective immediately in memory (no restart).
    assert budget_service.is_budget_enabled() is True
    assert budget_service.default_monthly_cost_limit() == Decimal("25.5")


@pytest.mark.asyncio
async def test_update_budget_settings_rejects_negative_cap(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    resp = await admin_client.put(
        "/api/admin/settings/budget",
        json={"enabled": True, "default_monthly_cost_limit_usd": -1},
    )
    assert resp.status_code == 422
    mock_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_active_sandboxes_resolves_emails(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """Running sandboxes are listed with owner UUIDs resolved to emails (one batched query)."""
    from app.services.sandbox import SandboxInfo

    uid = uuid.uuid4()
    infos = [
        SandboxInfo(session_id="sid-1", user_id=str(uid), status="Up 3 minutes", in_use=1),
        # orphan with no owner label
        SandboxInfo(session_id="sid-2", user_id=None, status="Up 1 hour", in_use=0),
    ]
    user = MagicMock(id=uid, email="owner@x")
    users_result = MagicMock()
    users_result.scalars.return_value = [user]
    mock_db.execute = AsyncMock(return_value=users_result)

    with patch("app.api.routes.admin.list_sandboxes", AsyncMock(return_value=infos)):
        resp = await admin_client.get("/api/admin/sandboxes")

    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    by_sid = {s["session_id"]: s for s in data["sandboxes"]}
    assert by_sid["sid-1"]["user_email"] == "owner@x"
    assert by_sid["sid-1"]["in_use"] == 1
    assert by_sid["sid-2"]["user_email"] is None  # unattributed orphan


@pytest.mark.asyncio
async def test_get_active_sandboxes_empty_skips_user_query(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """No running sandboxes → empty list and no user lookup is issued."""
    with patch("app.api.routes.admin.list_sandboxes", AsyncMock(return_value=[])):
        resp = await admin_client.get("/api/admin/sandboxes")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "sandboxes": []}
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_active_sandboxes_daemon_error_returns_502(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """A wedged/unreachable daemon surfaces as 502 — never a fake empty list or a 500."""
    with patch(
        "app.api.routes.admin.list_sandboxes", AsyncMock(side_effect=RuntimeError("daemon down"))
    ):
        resp = await admin_client.get("/api/admin/sandboxes")
    assert resp.status_code == 502
    mock_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminate_active_sandbox(admin_client: AsyncClient) -> None:
    """DELETE soft-kills the pair by id (durable dir kept) and returns 204."""
    sid = uuid.uuid4()
    term = AsyncMock(return_value=True)
    with patch("app.api.routes.admin.terminate_sandbox", term):
        resp = await admin_client.delete(f"/api/admin/sandboxes/{sid}")
    assert resp.status_code == 204
    term.assert_awaited_once_with(str(sid))


@pytest.mark.asyncio
async def test_terminate_active_sandbox_rejects_non_uuid(admin_client: AsyncClient) -> None:
    """A non-UUID id is refused by validation — can't address arbitrary containers."""
    term = AsyncMock()
    with patch("app.api.routes.admin.terminate_sandbox", term):
        resp = await admin_client.delete("/api/admin/sandboxes/not-a-uuid")
    assert resp.status_code == 422
    term.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminate_active_sandbox_daemon_error_returns_502(
    admin_client: AsyncClient,
) -> None:
    """A wedged daemon during terminate surfaces as 502, not an unhandled 500."""
    sid = uuid.uuid4()
    term = AsyncMock(side_effect=TimeoutError("wedged"))
    with patch("app.api.routes.admin.terminate_sandbox", term):
        resp = await admin_client.delete(f"/api/admin/sandboxes/{sid}")
    assert resp.status_code == 502
