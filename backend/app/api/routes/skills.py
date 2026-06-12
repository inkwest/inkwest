"""
Skill management routes.

Read operations require authentication, write operations require admin role.
Templates are global (shared across all users).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AdminUser, CurrentUser, get_db
from app.models.schemas import (
    SkillCreate,
    SkillList,
    SkillResponse,
    SkillUpdate,
)
from app.models.skill import Skill

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=SkillList)
async def list_skills(
    current_user: CurrentUser,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
) -> SkillList:
    query = select(Skill)
    if active_only:
        query = query.where(Skill.is_active == True)  # noqa: E712

    query = query.order_by(Skill.name)

    result = await db.execute(query)
    templates = result.scalars().all()

    return SkillList(
        items=[SkillResponse.model_validate(t) for t in templates],
        total=len(templates),
    )


@router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(
    template_data: SkillCreate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SkillResponse:
    existing = await db.execute(select(Skill).where(Skill.name == template_data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Skill with name '{template_data.name}' already exists",
        )

    template = Skill(**template_data.model_dump())
    db.add(template)
    await db.flush()
    await db.refresh(template)

    return SkillResponse.model_validate(template)


@router.get("/{template_id}", response_model=SkillResponse)
async def get_skill(
    template_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SkillResponse:
    result = await db.execute(select(Skill).where(Skill.id == template_id))
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill with ID '{template_id}' not found",
        )

    return SkillResponse.model_validate(template)


@router.patch("/{template_id}", response_model=SkillResponse)
async def update_skill(
    template_id: UUID,
    template_data: SkillUpdate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SkillResponse:
    result = await db.execute(select(Skill).where(Skill.id == template_id))
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill with ID '{template_id}' not found",
        )

    update_data = template_data.model_dump(exclude_unset=True)

    # Name is editable, so a rename must not collide with another template.
    new_name = update_data.get("name")
    if new_name and new_name != template.name:
        existing = await db.execute(select(Skill).where(Skill.name == new_name))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Skill with name '{new_name}' already exists",
            )

    for field, value in update_data.items():
        setattr(template, field, value)

    await db.flush()
    await db.refresh(template)

    return SkillResponse.model_validate(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    template_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(Skill).where(Skill.id == template_id))
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill with ID '{template_id}' not found",
        )

    # Hard delete: deactivation is already covered by PATCH is_active, and a
    # soft-deleted row would keep the unique name taken forever (409 on re-create).
    await db.delete(template)
    await db.flush()
