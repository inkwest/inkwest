"""
Prompt template management routes.

Read operations require authentication, write operations require admin role.
Templates are global (shared across all users).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AdminUser, CurrentUser, get_db
from app.models.prompt_template import PromptTemplate
from app.models.schemas import (
    PromptTemplateCreate,
    PromptTemplateList,
    PromptTemplateResponse,
    PromptTemplateUpdate,
)

router = APIRouter(prefix="/prompt-templates", tags=["prompt-templates"])


@router.get("", response_model=PromptTemplateList)
async def list_prompt_templates(
    current_user: CurrentUser,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
) -> PromptTemplateList:
    query = select(PromptTemplate)
    if active_only:
        query = query.where(PromptTemplate.is_active == True)  # noqa: E712

    query = query.order_by(PromptTemplate.name)

    result = await db.execute(query)
    templates = result.scalars().all()

    return PromptTemplateList(
        items=[PromptTemplateResponse.model_validate(t) for t in templates],
        total=len(templates),
    )


@router.post("", response_model=PromptTemplateResponse, status_code=status.HTTP_201_CREATED)
async def create_prompt_template(
    template_data: PromptTemplateCreate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> PromptTemplateResponse:
    existing = await db.execute(
        select(PromptTemplate).where(PromptTemplate.name == template_data.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Prompt template with name '{template_data.name}' already exists",
        )

    template = PromptTemplate(**template_data.model_dump())
    db.add(template)
    await db.flush()
    await db.refresh(template)

    return PromptTemplateResponse.model_validate(template)


@router.get("/{template_id}", response_model=PromptTemplateResponse)
async def get_prompt_template(
    template_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PromptTemplateResponse:
    result = await db.execute(select(PromptTemplate).where(PromptTemplate.id == template_id))
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Prompt template with ID '{template_id}' not found",
        )

    return PromptTemplateResponse.model_validate(template)


@router.patch("/{template_id}", response_model=PromptTemplateResponse)
async def update_prompt_template(
    template_id: UUID,
    template_data: PromptTemplateUpdate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> PromptTemplateResponse:
    result = await db.execute(select(PromptTemplate).where(PromptTemplate.id == template_id))
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Prompt template with ID '{template_id}' not found",
        )

    update_data = template_data.model_dump(exclude_unset=True)

    # Name is editable, so a rename must not collide with another template.
    new_name = update_data.get("name")
    if new_name and new_name != template.name:
        existing = await db.execute(select(PromptTemplate).where(PromptTemplate.name == new_name))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Prompt template with name '{new_name}' already exists",
            )

    for field, value in update_data.items():
        setattr(template, field, value)

    await db.flush()
    await db.refresh(template)

    return PromptTemplateResponse.model_validate(template)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt_template(
    template_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(PromptTemplate).where(PromptTemplate.id == template_id))
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Prompt template with ID '{template_id}' not found",
        )

    # Hard delete: deactivation is already covered by PATCH is_active, and a
    # soft-deleted row would keep the unique name taken forever (409 on re-create).
    await db.delete(template)
    await db.flush()
