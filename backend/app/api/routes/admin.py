"""
Admin API routes.

Protected routes for budget management, user administration, and system configuration.
Requires admin role via OIDC.
"""

import datetime as dt
import ipaddress
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AdminUser, get_db
from app.core.config import get_settings
from app.models.budget import Budget, BudgetPeriod, BudgetType, UsageRecord
from app.models.role_mapping import RoleKubeconfigMapping
from app.models.session import Session
from app.models.user import User
from app.services import mcp_oauth_catalog, model_registry
from app.services.app_settings import (
    BEDROCK_INCLUDE_OPEN_KEY,
    DEFAULT_ENGINE_KEY,
    SANDBOX_MAX_SESSIONS_PER_USER_KEY,
    get_int_setting,
    set_setting,
)
from app.services.budget_service import (
    get_period_start,
)
from app.services.engines import (
    capable_engines,
    configured_default_engine,
    enabled_engines,
    engine_for_turn,
    inkwest_agent_enabled,
)
from app.services.model_catalog import bedrock_include_open, catalog_status, refresh_catalog
from app.services.sandbox import hard_ceiling

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# =============================================================================
# Schemas
# =============================================================================


class BudgetCreate(BaseModel):
    """Schema for creating a budget."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    budget_type: BudgetType
    target_id: UUID | None = None
    target_role: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    period: BudgetPeriod = BudgetPeriod.DAILY
    soft_limit_percent: int = Field(default=80, ge=0, le=100)
    hard_limit_action: str = "block"


class BudgetUpdate(BaseModel):
    """Schema for updating a budget."""

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    period: BudgetPeriod | None = None
    soft_limit_percent: int | None = Field(None, ge=0, le=100)
    hard_limit_action: str | None = None
    is_active: bool | None = None


class BudgetResponse(BaseModel):
    """Schema for budget response."""

    id: UUID
    name: str
    description: str | None
    budget_type: BudgetType
    target_id: UUID | None
    target_role: str | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    max_total_tokens: int | None
    max_cost_usd: float | None
    period: BudgetPeriod
    soft_limit_percent: int
    hard_limit_action: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class RoleMappingCreate(BaseModel):
    """Schema for creating a role mapping."""

    role_name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    allowed_clusters: list[UUID] = []
    kubeconfig_override: str | None = None
    allowed_namespaces: list[str] = []
    denied_namespaces: list[str] = []
    can_web_search: bool = False
    daily_token_budget: int = 0


class RoleMappingUpdate(BaseModel):
    """Schema for updating a role mapping."""

    description: str | None = None
    allowed_clusters: list[UUID] | None = None
    kubeconfig_override: str | None = None
    allowed_namespaces: list[str] | None = None
    denied_namespaces: list[str] | None = None
    can_web_search: bool | None = None
    daily_token_budget: int | None = None
    is_active: bool | None = None


class RoleMappingResponse(BaseModel):
    """Schema for role mapping response."""

    id: UUID
    role_name: str
    description: str | None
    allowed_clusters: list[UUID]
    kubeconfig_override: str | None
    allowed_namespaces: list[str]
    denied_namespaces: list[str]
    can_web_search: bool
    daily_token_budget: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class UserResponse(BaseModel):
    """Schema for user response."""

    id: UUID
    oidc_subject: str
    email: str
    display_name: str
    roles: list[str]
    is_active: bool
    last_login: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class UsageSummaryResponse(BaseModel):
    """Schema for usage summary."""

    period: str
    period_start: datetime
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: float
    request_count: int


class ModelInfo(BaseModel):
    """A registry model as the admin console sees it: identity + config + pricing."""

    id: str
    label: str
    provider: str
    enabled: bool
    is_default: bool
    # discovered (from the live provider catalog) | custom (operator-added).
    source: str
    legacy: bool
    # False = no real pricing (no override and not in the static table); cost
    # tracking falls back to a conservative default — surfaced "unpriced" in UI.
    priced: bool
    endpoint: str | None
    arn: str | None
    options: dict[str, Any]
    # Engines *capable* of running this model (pure vendor capability — which engine
    # sections the model appears in). The engines it is actually *offered* on live in
    # options["engines"] (absent = all capable). See engines.capable_engines.
    capable_engines: list[str]
    # Modality capabilities from the live catalog (P1.8): input TEXT/IMAGE/VIDEO/SPEECH,
    # output TEXT/EMBEDDING/IMAGE/VIDEO. Empty = unknown/text-only. Drives the admin
    # modality icons + the non-chat (embedding/image-gen output) badge + capability filter.
    input_modalities: list[str]
    output_modalities: list[str]
    # Effective per-million-token prices, null when unpriced.
    input_price: float | None
    output_price: float | None


class CatalogStatus(BaseModel):
    """Live-discovery status surfaced next to the Refresh control."""

    last_refreshed_at: str | None
    source: str  # live | static
    last_fetch_failed: bool
    count: int


class ModelSettingsResponse(BaseModel):
    """Schema for model settings."""

    provider: str
    default_model: str
    # The engine the default model runs on (admin choice, else resolved heuristically).
    default_engine: str
    # Model IDs users may pick per session (admin-enabled subset).
    enabled_models: list[str]
    # Whether all Bedrock vendors (not just Anthropic) are surfaced in the catalog.
    include_all_vendors: bool
    # Whether the custom (inkwest-agent) engine is enabled — gates the
    # all-vendors toggle, which is only useful when an engine can run them.
    inkwest_agent_enabled: bool
    # Engines enabled in this deployment (AGENTIC_ENGINES) — the admin renders one
    # section per engine and curates per-(engine, model) availability against this.
    engines: list[str]
    catalog: CatalogStatus
    # The full merged registry: discovered models + custom rows.
    models: list[ModelInfo]


class UpdateModelSettingsRequest(BaseModel):
    """Bulk save: the enabled subset, the default model, and per-engine availability."""

    default_model: str = Field(..., min_length=1, max_length=512)
    # The engine the default model runs on; None = resolve heuristically.
    default_engine: str | None = Field(None, max_length=50)
    enabled_models: list[str] = Field(..., min_length=1)
    # Optional per-(engine, model) offering: model_id -> engine ids it's exposed on.
    # Omitted = leave each model's engine offering untouched (legacy enable-only save).
    engine_availability: dict[str, list[str]] | None = None


class RefreshModelsRequest(BaseModel):
    """Force a catalog refresh, optionally flipping the all-vendors flag first."""

    include_all_vendors: bool | None = None


# The provider tags the registry understands (drives chat routing + UI grouping):
# per-vendor Bedrock namespaces + the off-Bedrock providers, plus the legacy
# ``bedrock``/``bedrock-open`` tags kept so rows written before per-vendor tagging
# still validate and route. Only ``anthropic``/``bedrock`` speak Messages.
ModelProvider = Literal[
    "anthropic",
    "amazon",
    "meta",
    "mistral",
    "cohere",
    "ai21",
    "deepseek",
    "stability",
    "claude-cli",
    "bedrock",
    "bedrock-open",
]

# Hostnames that must never be reachable via an operator-supplied endpoint.
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}


def _require_safe_endpoint(value: str | None) -> str | None:
    """Validate the operator-supplied custom endpoint.

    The field is stored metadata today, but a future server-side consumer must
    not be steerable to internal/SSRF targets — so enforce http(s) AND reject
    loopback/private/link-local/reserved IP literals and known-internal hosts at
    write time. (DNS-rebind via a hostname that resolves internally can't be
    caught here; a consumer that dials this must re-validate the resolved IP.)
    """
    if not value or not value.strip():
        return value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("endpoint must be an http(s) URL")
    host = parsed.hostname
    if not host:
        raise ValueError("endpoint must include a host")
    if host.lower() in _BLOCKED_HOSTS:
        raise ValueError("endpoint host is not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # a hostname, not an IP literal
    if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
        raise ValueError("endpoint must not point at an internal address")
    return value


def _require_arn(value: str | None) -> str | None:
    """A Bedrock ARN, when set, must look like one (it becomes the invoke target)."""
    if value and value.strip() and not value.startswith("arn:"):
        raise ValueError("arn must start with 'arn:'")
    return value


class AddModelRequest(BaseModel):
    """Add an operator-defined model the discovery filter does not surface."""

    model_config = {"protected_namespaces": ()}

    id: str = Field(..., min_length=1, max_length=512)
    provider: ModelProvider
    display_name: str | None = Field(None, max_length=255)
    endpoint: str | None = Field(None, max_length=1024)
    arn: str | None = Field(None, max_length=1024)
    options: dict[str, Any] = Field(default_factory=dict)
    input_price: float | None = Field(None, ge=0)
    output_price: float | None = Field(None, ge=0)
    enabled: bool = False
    legacy: bool = False

    _check_endpoint = field_validator("endpoint")(_require_safe_endpoint)
    _check_arn = field_validator("arn")(_require_arn)


class UpdateModelRequest(BaseModel):
    """Patch a model's metadata; omitted fields are left unchanged."""

    display_name: str | None = None
    endpoint: str | None = None
    arn: str | None = None
    options: dict[str, Any] | None = None
    input_price: float | None = Field(None, ge=0)
    output_price: float | None = Field(None, ge=0)
    legacy: bool | None = None
    enabled: bool | None = None

    _check_endpoint = field_validator("endpoint")(_require_safe_endpoint)
    _check_arn = field_validator("arn")(_require_arn)


# =============================================================================
# Budget Management Endpoints
# =============================================================================


@router.post("/budgets", response_model=BudgetResponse, status_code=status.HTTP_201_CREATED)
async def create_budget(
    budget: BudgetCreate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> Budget:
    """Create a new budget."""
    db_budget = Budget(
        name=budget.name,
        description=budget.description,
        budget_type=budget.budget_type,
        target_id=budget.target_id,
        target_role=budget.target_role,
        max_input_tokens=budget.max_input_tokens,
        max_output_tokens=budget.max_output_tokens,
        max_total_tokens=budget.max_total_tokens,
        max_cost_usd=budget.max_cost_usd,
        period=budget.period,
        soft_limit_percent=budget.soft_limit_percent,
        hard_limit_action=budget.hard_limit_action,
    )

    db.add(db_budget)
    await db.commit()
    await db.refresh(db_budget)

    logger.info(f"Created budget: {db_budget.name} (type={db_budget.budget_type.value})")
    return db_budget


@router.get("/budgets", response_model=list[BudgetResponse])
async def list_budgets(
    admin: AdminUser,
    budget_type: BudgetType | None = None,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
) -> Sequence[Budget]:
    """List all budgets."""
    query = select(Budget)

    if budget_type:
        query = query.where(Budget.budget_type == budget_type)
    if is_active is not None:
        query = query.where(Budget.is_active == is_active)

    query = query.order_by(Budget.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/budgets/{budget_id}", response_model=BudgetResponse)
async def get_budget(
    budget_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> Budget:
    """Get a budget by ID."""
    result = await db.execute(select(Budget).where(Budget.id == budget_id))
    budget = result.scalar_one_or_none()

    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")

    return budget


@router.put("/budgets/{budget_id}", response_model=BudgetResponse)
async def update_budget(
    budget_id: UUID,
    update: BudgetUpdate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> Budget:
    """Update a budget."""
    result = await db.execute(select(Budget).where(Budget.id == budget_id))
    budget = result.scalar_one_or_none()

    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")

    update_data = update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(budget, key, value)

    await db.commit()
    await db.refresh(budget)

    logger.info(f"Updated budget: {budget.name}")
    return budget


@router.delete("/budgets/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(
    budget_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a budget."""
    result = await db.execute(select(Budget).where(Budget.id == budget_id))
    budget = result.scalar_one_or_none()

    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")

    await db.delete(budget)
    await db.commit()

    logger.info(f"Deleted budget: {budget.name}")


# =============================================================================
# Role Mapping Endpoints
# =============================================================================


@router.post(
    "/role-mappings", response_model=RoleMappingResponse, status_code=status.HTTP_201_CREATED
)
async def create_role_mapping(
    mapping: RoleMappingCreate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> RoleKubeconfigMapping:
    """Create a new role mapping."""
    # Check if role already exists
    result = await db.execute(
        select(RoleKubeconfigMapping).where(RoleKubeconfigMapping.role_name == mapping.role_name)
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Role mapping for '{mapping.role_name}' already exists",
        )

    db_mapping = RoleKubeconfigMapping(
        role_name=mapping.role_name,
        description=mapping.description,
        allowed_clusters=mapping.allowed_clusters,
        kubeconfig_override=mapping.kubeconfig_override,
        allowed_namespaces=mapping.allowed_namespaces,
        denied_namespaces=mapping.denied_namespaces,
        can_web_search=mapping.can_web_search,
        daily_token_budget=mapping.daily_token_budget,
    )

    db.add(db_mapping)
    await db.commit()
    await db.refresh(db_mapping)

    logger.info(f"Created role mapping: {db_mapping.role_name}")
    return db_mapping


@router.get("/role-mappings", response_model=list[RoleMappingResponse])
async def list_role_mappings(
    admin: AdminUser,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
) -> Sequence[RoleKubeconfigMapping]:
    """List all role mappings."""
    query = select(RoleKubeconfigMapping)

    if is_active is not None:
        query = query.where(RoleKubeconfigMapping.is_active == is_active)

    query = query.order_by(RoleKubeconfigMapping.role_name)
    result = await db.execute(query)
    return result.scalars().all()


@router.put("/role-mappings/{mapping_id}", response_model=RoleMappingResponse)
async def update_role_mapping(
    mapping_id: UUID,
    update: RoleMappingUpdate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> RoleKubeconfigMapping:
    """Update a role mapping."""
    result = await db.execute(
        select(RoleKubeconfigMapping).where(RoleKubeconfigMapping.id == mapping_id)
    )
    mapping = result.scalar_one_or_none()

    if not mapping:
        raise HTTPException(status_code=404, detail="Role mapping not found")

    update_data = update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(mapping, key, value)

    await db.commit()
    await db.refresh(mapping)

    logger.info(f"Updated role mapping: {mapping.role_name}")
    return mapping


@router.delete("/role-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role_mapping(
    mapping_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a role mapping."""
    result = await db.execute(
        select(RoleKubeconfigMapping).where(RoleKubeconfigMapping.id == mapping_id)
    )
    mapping = result.scalar_one_or_none()

    if not mapping:
        raise HTTPException(status_code=404, detail="Role mapping not found")

    await db.delete(mapping)
    await db.commit()

    logger.info(f"Deleted role mapping: {mapping.role_name}")


# =============================================================================
# User Management Endpoints
# =============================================================================


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    admin: AdminUser,
    is_active: bool | None = None,
    role: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Sequence[User]:
    """List all users."""
    query = select(User)

    if is_active is not None:
        query = query.where(User.is_active == is_active)
    if role:
        query = query.where(User.roles.contains(cast([role], ARRAY(String))))

    query = query.order_by(User.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Get a user by ID."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return user


class UpdateUserRolesRequest(BaseModel):
    """Schema for updating user roles."""

    roles: list[str]


class UpdateUserStatusRequest(BaseModel):
    """Schema for updating user status."""

    is_active: bool


@router.put("/users/{user_id}/roles", response_model=UserResponse)
async def update_user_roles(
    user_id: UUID,
    body: UpdateUserRolesRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Update a user's roles."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.roles = body.roles
    await db.commit()
    await db.refresh(user)

    logger.info(f"Updated user roles: {user.email} -> {body.roles}")
    return user


@router.put("/users/{user_id}/status", response_model=UserResponse)
async def update_user_status(
    user_id: UUID,
    body: UpdateUserStatusRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Enable or disable a user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = body.is_active
    await db.commit()
    await db.refresh(user)

    logger.info(f"Updated user status: {user.email} -> active={body.is_active}")
    return user


# =============================================================================
# Usage Reports
# =============================================================================


def _usage_date_filters(
    period: BudgetPeriod,
    start_date: dt.date | None,
    end_date: dt.date | None,
) -> tuple[datetime, datetime | None]:
    """Resolve date range from explicit dates or budget period."""
    if start_date:
        start = datetime.combine(start_date, dt.time.min, tzinfo=UTC)
        end = datetime.combine(end_date, dt.time.max, tzinfo=UTC) if end_date else None
        return start, end
    return get_period_start(period), None


@router.get("/usage/summary", response_model=UsageSummaryResponse)
async def get_usage_summary(
    admin: AdminUser,
    period: BudgetPeriod = BudgetPeriod.DAILY,
    user_id: UUID | None = None,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
) -> UsageSummaryResponse:
    """Get usage summary for a period or custom date range."""
    filter_start, filter_end = _usage_date_filters(period, start_date, end_date)

    conditions = [UsageRecord.recorded_at >= filter_start]
    if filter_end:
        conditions.append(UsageRecord.recorded_at <= filter_end)
    if user_id is not None:
        conditions.append(UsageRecord.user_id == user_id)

    query = select(
        func.coalesce(func.sum(UsageRecord.input_tokens), 0),
        func.coalesce(func.sum(UsageRecord.output_tokens), 0),
        func.coalesce(func.sum(UsageRecord.cost_usd), 0),
        func.count(UsageRecord.id),
    ).where(*conditions)

    result = await db.execute(query)
    row = result.one()
    input_tokens = int(row[0])
    output_tokens = int(row[1])

    return UsageSummaryResponse(
        period="custom" if start_date else period.value,
        period_start=filter_start,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        total_cost_usd=float(row[2]),
        request_count=int(row[3]),
    )


@router.get("/usage/by-user")
async def get_usage_by_user(
    admin: AdminUser,
    period: BudgetPeriod = BudgetPeriod.DAILY,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    limit: int = Query(default=50, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Get usage breakdown by user."""
    filter_start, filter_end = _usage_date_filters(period, start_date, end_date)

    conditions = [UsageRecord.recorded_at >= filter_start]
    if filter_end:
        conditions.append(UsageRecord.recorded_at <= filter_end)

    query = (
        select(
            UsageRecord.user_id,
            func.sum(UsageRecord.input_tokens).label("input_tokens"),
            func.sum(UsageRecord.output_tokens).label("output_tokens"),
            func.sum(UsageRecord.cost_usd).label("cost_usd"),
            func.count(UsageRecord.id).label("request_count"),
        )
        .where(*conditions)
        .group_by(UsageRecord.user_id)
        .order_by(func.sum(UsageRecord.cost_usd).desc())
        .limit(limit)
    )

    result = await db.execute(query)
    rows = result.all()

    user_ids = [row.user_id for row in rows if row.user_id]
    users_map: dict[str, User] = {}
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {str(u.id): u for u in users_result.scalars().all()}

    return [
        {
            "user_id": str(row.user_id) if row.user_id else None,
            "email": users_map[str(row.user_id)].email
            if row.user_id and str(row.user_id) in users_map
            else "",
            "display_name": users_map[str(row.user_id)].display_name
            if row.user_id and str(row.user_id) in users_map
            else "Unknown",
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "total_tokens": row.input_tokens + row.output_tokens,
            "cost_usd": float(row.cost_usd),
            "request_count": row.request_count,
        }
        for row in rows
    ]


@router.get("/usage/by-model")
async def get_usage_by_model(
    admin: AdminUser,
    period: BudgetPeriod = BudgetPeriod.DAILY,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Get usage breakdown by model."""
    filter_start, filter_end = _usage_date_filters(period, start_date, end_date)

    conditions = [UsageRecord.recorded_at >= filter_start]
    if filter_end:
        conditions.append(UsageRecord.recorded_at <= filter_end)

    query = (
        select(
            UsageRecord.model,
            UsageRecord.provider,
            func.sum(UsageRecord.input_tokens).label("input_tokens"),
            func.sum(UsageRecord.output_tokens).label("output_tokens"),
            func.sum(UsageRecord.cost_usd).label("cost_usd"),
            func.count(UsageRecord.id).label("request_count"),
        )
        .where(*conditions)
        .group_by(UsageRecord.model, UsageRecord.provider)
        .order_by(func.sum(UsageRecord.cost_usd).desc())
    )

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "model": row.model,
            "provider": row.provider,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "total_tokens": row.input_tokens + row.output_tokens,
            "cost_usd": float(row.cost_usd),
            "request_count": row.request_count,
        }
        for row in rows
    ]


@router.get("/usage/by-user/{user_id}/sessions")
async def get_usage_by_user_sessions(
    user_id: UUID,
    admin: AdminUser,
    period: BudgetPeriod = BudgetPeriod.DAILY,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get per-session usage breakdown for a specific user."""
    filter_start, filter_end = _usage_date_filters(period, start_date, end_date)

    conditions = [UsageRecord.user_id == user_id, UsageRecord.recorded_at >= filter_start]
    if filter_end:
        conditions.append(UsageRecord.recorded_at <= filter_end)

    query = (
        select(
            UsageRecord.session_id,
            func.sum(UsageRecord.input_tokens).label("input_tokens"),
            func.sum(UsageRecord.output_tokens).label("output_tokens"),
            func.sum(UsageRecord.cost_usd).label("cost_usd"),
            func.count(UsageRecord.id).label("request_count"),
            func.min(UsageRecord.recorded_at).label("first_request"),
            func.max(UsageRecord.recorded_at).label("last_request"),
        )
        .where(*conditions)
        .group_by(UsageRecord.session_id)
        .order_by(func.sum(UsageRecord.cost_usd).desc())
    )

    result = await db.execute(query)
    rows = result.all()

    session_ids = [row.session_id for row in rows]
    sessions_map: dict[str, str] = {}
    if session_ids:
        sessions_result = await db.execute(
            select(Session.id, Session.title).where(Session.id.in_(session_ids))
        )
        sessions_map = {str(s.id): s.title or "Untitled" for s in sessions_result.all()}

    user = await db.get(User, user_id)

    return {
        "user_id": str(user_id),
        "display_name": user.display_name if user else "Unknown",
        "email": user.email if user else "",
        "sessions": [
            {
                "session_id": str(row.session_id),
                "title": sessions_map.get(str(row.session_id), "Untitled"),
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "total_tokens": row.input_tokens + row.output_tokens,
                "cost_usd": float(row.cost_usd),
                "request_count": row.request_count,
                "first_request": row.first_request.isoformat() if row.first_request else None,
                "last_request": row.last_request.isoformat() if row.last_request else None,
            }
            for row in rows
        ],
    }


@router.get("/usage/timeseries")
async def get_usage_timeseries(
    admin: AdminUser,
    granularity: str = Query(default="day", pattern="^(hour|day)$"),
    period: BudgetPeriod = BudgetPeriod.DAILY,
    start_date: dt.date | None = None,
    end_date: dt.date | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Get usage timeseries grouped by hour or day."""
    filter_start, filter_end = _usage_date_filters(period, start_date, end_date)

    valid_granularities = {"hour", "day"}
    if granularity not in valid_granularities:
        raise HTTPException(status_code=400, detail="Invalid granularity")
    bucket = func.date_trunc(granularity, UsageRecord.recorded_at).label("bucket")

    conditions = [UsageRecord.recorded_at >= filter_start]
    if filter_end:
        conditions.append(UsageRecord.recorded_at <= filter_end)

    query = (
        select(
            bucket,
            func.sum(UsageRecord.input_tokens).label("input_tokens"),
            func.sum(UsageRecord.output_tokens).label("output_tokens"),
            func.sum(UsageRecord.cost_usd).label("cost_usd"),
            func.count(UsageRecord.id).label("request_count"),
        )
        .where(*conditions)
        .group_by(bucket)
        .order_by(bucket)
    )

    result = await db.execute(query)
    return [
        {
            "timestamp": row.bucket.isoformat(),
            "input_tokens": int(row.input_tokens),
            "output_tokens": int(row.output_tokens),
            "cost_usd": float(row.cost_usd),
            "request_count": int(row.request_count),
        }
        for row in result.all()
    ]


# =============================================================================
# Model Settings Endpoints
# =============================================================================


def _model_info(entry: model_registry.ModelEntry) -> ModelInfo:
    return ModelInfo(
        id=entry.model_id,
        label=entry.label,
        provider=entry.provider,
        enabled=entry.enabled,
        is_default=entry.is_default,
        source=entry.source,
        legacy=entry.legacy,
        priced=entry.priced,
        endpoint=entry.endpoint,
        arn=entry.arn,
        options=entry.options,
        capable_engines=capable_engines(entry.model_id),
        input_modalities=entry.input_modalities,
        output_modalities=entry.output_modalities,
        input_price=float(entry.input_price) if entry.input_price is not None else None,
        output_price=float(entry.output_price) if entry.output_price is not None else None,
    )


def _model_settings_response() -> ModelSettingsResponse:
    default_id = model_registry.default_model()
    return ModelSettingsResponse(
        provider=get_settings().ai_provider,
        default_model=default_id,
        default_engine=engine_for_turn(default_id, configured_default_engine()),
        enabled_models=model_registry.selectable_models(),
        include_all_vendors=bedrock_include_open(),
        inkwest_agent_enabled=inkwest_agent_enabled(),
        engines=enabled_engines(),
        catalog=CatalogStatus(**catalog_status()),
        models=[_model_info(e) for e in model_registry.merged_entries()],
    )


def _price(value: float | None) -> Decimal | None:
    # str() keeps the decimal the admin typed exact (Decimal(0.1) would not).
    return Decimal(str(value)) if value is not None else None


@router.get("/settings/model", response_model=ModelSettingsResponse)
async def get_model_settings(admin: AdminUser) -> ModelSettingsResponse:
    """Get the model configuration: default, enabled subset, full merged catalog."""
    await refresh_catalog()  # TTL-aware; no-op while fresh
    return _model_settings_response()


@router.put("/settings/model", response_model=ModelSettingsResponse)
async def update_model_settings(
    body: UpdateModelSettingsRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> ModelSettingsResponse:
    """Bulk-set the enabled models and the default model."""
    await refresh_catalog()  # validate against a current view of the provider catalog

    available = {e.model_id for e in model_registry.merged_entries()}
    unknown = [m for m in body.enabled_models if m not in available]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown models: {', '.join(unknown)}",
        )
    # Validate per-engine availability: each entry must name a known model and only
    # engines that model is *capable* of (a non-Claude model can't be offered on
    # claude-cli). Reject otherwise so a stale UI can't persist an impossible pairing.
    if body.engine_availability is not None:
        for model_id, engines in body.engine_availability.items():
            if model_id not in available:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown model in engine_availability: {model_id}",
                )
            bad = [e for e in engines if e not in capable_engines(model_id)]
            if bad:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Model '{model_id}' cannot run on engine(s): {', '.join(bad)}",
                )
    # The default engine (if pinned) must be one the default model is capable of.
    if body.default_engine is not None and body.default_engine not in capable_engines(
        body.default_model
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Default model '{body.default_model}' cannot run on engine "
            f"'{body.default_engine}'",
        )
    try:
        await model_registry.set_model_settings(
            db, body.default_model, body.enabled_models, availability=body.engine_availability
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    # Persist the default engine (an app-setting, separate from the registry's model
    # default flag). Empty string clears it → resolve heuristically.
    await set_setting(db, DEFAULT_ENGINE_KEY, body.default_engine or "")
    logger.info(
        f"Model settings changed by {admin.email}: default={body.default_model} "
        f"(engine={body.default_engine}), enabled={body.enabled_models}"
    )
    return _model_settings_response()


class SandboxSettingsResponse(BaseModel):
    """Runtime sandbox limits an admin may tune without a redeploy."""

    # Effective per-user concurrent-sandbox cap (admin override or env default). 0 = unlimited.
    # Idle conversations are reaped back down to this; the value is the headline limit.
    max_sessions_per_user: int
    # The env baseline, shown so the UI can label when an override is in effect.
    default_max_sessions_per_user: int
    # Busy-burst ceiling (cap + max(2, 25%); 0 when unlimited): a turn that would push the
    # user's running sandboxes past it is refused. Exposed so the admin sees the real limit.
    hard_ceiling: int


class UpdateSandboxSettingsRequest(BaseModel):
    max_sessions_per_user: int = Field(..., ge=0, le=1000)


def _sandbox_settings_response() -> SandboxSettingsResponse:
    env_default = get_settings().sandbox_max_sessions_per_user
    cap = get_int_setting(SANDBOX_MAX_SESSIONS_PER_USER_KEY, env_default)
    return SandboxSettingsResponse(
        max_sessions_per_user=cap,
        default_max_sessions_per_user=env_default,
        hard_ceiling=hard_ceiling(cap) if cap > 0 else 0,
    )


@router.get("/settings/sandbox", response_model=SandboxSettingsResponse)
async def get_sandbox_settings(admin: AdminUser) -> SandboxSettingsResponse:
    """Get the runtime sandbox limits (currently the per-user concurrency cap)."""
    return _sandbox_settings_response()


@router.put("/settings/sandbox", response_model=SandboxSettingsResponse)
async def update_sandbox_settings(
    body: UpdateSandboxSettingsRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SandboxSettingsResponse:
    """Set the per-user concurrent-sandbox cap (0 = unlimited). Effective immediately."""
    await set_setting(db, SANDBOX_MAX_SESSIONS_PER_USER_KEY, str(body.max_sessions_per_user))
    logger.info(
        f"Sandbox settings changed by {admin.email}: "
        f"max_sessions_per_user={body.max_sessions_per_user}"
    )
    return _sandbox_settings_response()


@router.post("/settings/model/refresh", response_model=ModelSettingsResponse)
async def refresh_model_catalog(
    body: RefreshModelsRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> ModelSettingsResponse:
    """Force a live catalog refresh; optionally flip the all-vendors flag first."""
    if body.include_all_vendors is not None:
        await set_setting(
            db, BEDROCK_INCLUDE_OPEN_KEY, "true" if body.include_all_vendors else "false"
        )
    await refresh_catalog(force=True)
    logger.info(
        f"Model catalog refreshed by {admin.email}"
        + (
            f" (include_all_vendors={body.include_all_vendors})"
            if body.include_all_vendors is not None
            else ""
        )
    )
    return _model_settings_response()


@router.post("/models", response_model=ModelSettingsResponse, status_code=status.HTTP_201_CREATED)
async def add_model(
    body: AddModelRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> ModelSettingsResponse:
    """Add an operator-defined custom model."""
    try:
        await model_registry.add_custom_model(
            db,
            model_id=body.id,
            provider=body.provider,
            display_name=body.display_name,
            endpoint=body.endpoint,
            arn=body.arn,
            options=body.options,
            input_price=_price(body.input_price),
            output_price=_price(body.output_price),
            enabled=body.enabled,
            legacy=body.legacy,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    logger.info(f"Custom model added by {admin.email}: {body.id}")
    return _model_settings_response()


@router.put("/models/{model_id}", response_model=ModelSettingsResponse)
async def update_model(
    model_id: str,
    body: UpdateModelRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> ModelSettingsResponse:
    """Patch a single model's metadata / pricing / enabled flag."""
    fields = body.model_dump(exclude_unset=True)
    if "input_price" in fields:
        fields["input_price"] = _price(fields["input_price"])
    if "output_price" in fields:
        fields["output_price"] = _price(fields["output_price"])
    if not fields:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")
    try:
        await model_registry.update_model(db, model_id, **fields)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    logger.info(f"Model {model_id} updated by {admin.email}: {', '.join(fields)}")
    return _model_settings_response()


@router.delete("/models/{model_id}", response_model=ModelSettingsResponse)
async def delete_model(
    model_id: str,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> ModelSettingsResponse:
    """Delete a custom model. Discovered models can only be disabled."""
    try:
        await model_registry.delete_model(db, model_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    logger.info(f"Custom model deleted by {admin.email}: {model_id}")
    return _model_settings_response()


# =============================================================================
# OAuth2 MCP Catalog (admin-managed; replaces the MCP_CATALOG_OAUTH2 env var)
# =============================================================================


class McpOauthEntryResponse(BaseModel):
    """A catalog entry as the admin panel sees it — secret-free."""

    key: str
    display_name: str | None
    url: str
    scopes: list[str]
    client_id: str | None
    # The raw client_secret is never serialized; the panel only learns whether one is set.
    has_secret: bool
    # False when a sealed secret can't be decrypted with the current keyring — the panel
    # blocks editing such a server (it's already dropped from service); delete/recreate only.
    editable: bool
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class McpOauthCreateRequest(BaseModel):
    """Create an OAuth2 MCP catalog entry."""

    key: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=255)
    scopes: list[str] = Field(default_factory=list)
    client_id: str | None = Field(default=None, max_length=512)
    # Optional (public clients use Dynamic Client Registration and have none).
    client_secret: str | None = None
    enabled: bool = True


class McpOauthUpdateRequest(BaseModel):
    """Update an OAuth2 MCP catalog entry (key comes from the path).

    Omit ``client_secret`` to keep the stored one; send ``""`` to clear it.
    """

    url: str = Field(min_length=1, max_length=1024)
    display_name: str | None = Field(default=None, max_length=255)
    scopes: list[str] = Field(default_factory=list)
    client_id: str | None = Field(default=None, max_length=512)
    client_secret: str | None = None
    enabled: bool = True


class McpOauthMetaResponse(BaseModel):
    """Server-side capabilities the MCP catalog form needs to know before submit."""

    # Whether at-rest secret encryption is configured (SECRETBOX_KEYS). When false, the
    # form disables the Confidential auth type (a client secret can't be stored).
    secret_storage_enabled: bool


@router.get("/mcp-oauth/meta", response_model=McpOauthMetaResponse)
async def get_mcp_oauth_meta(admin: AdminUser) -> McpOauthMetaResponse:
    """Report whether the server can store client secrets (gates the Confidential option)."""
    return McpOauthMetaResponse(secret_storage_enabled=mcp_oauth_catalog.secret_storage_enabled())


@router.get("/mcp-oauth", response_model=list[McpOauthEntryResponse])
async def list_mcp_oauth_servers(
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """List the configured OAuth2 MCP servers (incl. disabled). Secrets are not returned."""
    return await mcp_oauth_catalog.list_oauth_entries(db)


@router.post(
    "/mcp-oauth",
    response_model=list[McpOauthEntryResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_mcp_oauth_server(
    body: McpOauthCreateRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Add an OAuth2 MCP server to the catalog."""
    try:
        await mcp_oauth_catalog.upsert_oauth_server(
            db,
            key=body.key,
            url=body.url,
            scopes=body.scopes,
            client_id=body.client_id,
            client_secret=body.client_secret,
            display_name=body.display_name,
            enabled=body.enabled,
            create_only=True,
        )
    except mcp_oauth_catalog.DuplicateKeyError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An MCP server with key '{body.key}' already exists.",
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    logger.info(f"OAuth2 MCP server added by {admin.email}: {body.key}")
    return await mcp_oauth_catalog.list_oauth_entries(db)


@router.put("/mcp-oauth/{key}", response_model=list[McpOauthEntryResponse])
async def update_mcp_oauth_server(
    key: str,
    body: McpOauthUpdateRequest,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Update an OAuth2 MCP server. Omitting client_secret preserves the stored one."""
    try:
        await mcp_oauth_catalog.upsert_oauth_server(
            db,
            key=key,
            url=body.url,
            scopes=body.scopes,
            client_id=body.client_id,
            client_secret=body.client_secret,
            display_name=body.display_name,
            enabled=body.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    logger.info(f"OAuth2 MCP server updated by {admin.email}: {key}")
    return await mcp_oauth_catalog.list_oauth_entries(db)


@router.delete("/mcp-oauth/{key}", response_model=list[McpOauthEntryResponse])
async def delete_mcp_oauth_server(
    key: str,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Remove an OAuth2 MCP server from the catalog."""
    try:
        await mcp_oauth_catalog.delete_oauth_server(db, key)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    logger.info(f"OAuth2 MCP server deleted by {admin.email}: {key}")
    return await mcp_oauth_catalog.list_oauth_entries(db)


# =============================================================================
# System Info Endpoint
# =============================================================================


class SystemInfoResponse(BaseModel):
    """Read-only build + deployment info for the admin console."""

    # App/API image — the combined prod image (backend + baked SPA). Injected as env
    # vars at image build time (see Dockerfile: VERSION / COMMIT_HASH / BUILD_TIMESTAMP).
    app_version: str
    app_commit: str
    app_build_timestamp: str
    # Image the broker spawns the inkwest-agent worker in. The runtime only knows the
    # reference it was told to run (tag), not the agent's own build metadata. Empty when
    # the sandbox is not configured.
    agent_image: str
    # Resolved runtime environment.
    app_env: str
    ai_provider: str
    default_model: str
    # Agentic engines live in this deployment (AGENTIC_ENGINES).
    engines: list[str]
    # Auth + sandbox runtime state.
    oidc_enabled: bool
    sandbox_enabled: bool
    attachments_enabled: bool
    # At-rest encryption for operator secrets (SECRETBOX_KEYS configured). Gates the
    # Confidential MCP auth type; surfaced so operators can see the deployment's posture.
    secret_storage_enabled: bool
    # Model/pricing discovery freshness (live vs static, last refresh, fetch health).
    catalog: CatalogStatus


@router.get("/system-info", response_model=SystemInfoResponse)
async def get_system_info(admin: AdminUser) -> SystemInfoResponse:
    """Backend build metadata, the agent image ref, and the resolved runtime environment."""
    await refresh_catalog()  # TTL-aware; keeps the resolved default model + catalog status fresh
    settings = get_settings()
    return SystemInfoResponse(
        app_version=settings.version,
        app_commit=settings.commit_hash,
        app_build_timestamp=settings.build_timestamp,
        agent_image=settings.agent_image,
        app_env=settings.app_env,
        ai_provider=settings.ai_provider,
        default_model=model_registry.default_model(),
        engines=enabled_engines(),
        oidc_enabled=settings.oidc_enabled,
        sandbox_enabled=settings.sandbox_enabled,
        # Mirrors /api/config: attachments need the per-conversation sandbox (claude-cli).
        attachments_enabled=settings.sandbox_enabled and settings.ai_provider == "claude-cli",
        secret_storage_enabled=mcp_oauth_catalog.secret_storage_enabled(),
        catalog=CatalogStatus(**catalog_status()),
    )
