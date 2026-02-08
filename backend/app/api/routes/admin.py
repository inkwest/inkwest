"""
Admin API routes.

Protected routes for budget management, user administration, and system configuration.
Requires admin role via OIDC.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AdminUser, get_db
from app.models.budget import Budget, BudgetPeriod, BudgetType, UsageRecord
from app.models.role_mapping import RoleKubeconfigMapping
from app.models.session import Session
from app.models.user import User
from app.services.budget_service import (
    get_period_start,
)

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
