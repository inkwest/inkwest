"""
System prompt management routes.

Read operations require authentication, write operations require admin role.
System prompts are global (shared across all users); exactly one is the default.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AdminUser, CurrentUser, get_db
from app.models.schemas import (
    SystemPromptCreate,
    SystemPromptList,
    SystemPromptResponse,
    SystemPromptUpdate,
)
from app.models.system_prompt import SystemPrompt

router = APIRouter(prefix="/system-prompts", tags=["system-prompts"])


async def _clear_default(db: AsyncSession, except_id: UUID | None = None) -> None:
    """Unset the current default so a new one can be set (single-default invariant)."""
    stmt = update(SystemPrompt).where(SystemPrompt.is_default == True)  # noqa: E712
    if except_id is not None:
        stmt = stmt.where(SystemPrompt.id != except_id)
    await db.execute(stmt.values(is_default=False))


@router.get("", response_model=SystemPromptList)
async def list_system_prompts(
    current_user: CurrentUser,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
) -> SystemPromptList:
    query = select(SystemPrompt)
    if active_only:
        query = query.where(SystemPrompt.is_active == True)  # noqa: E712

    query = query.order_by(SystemPrompt.name)

    result = await db.execute(query)
    prompts = result.scalars().all()

    return SystemPromptList(
        items=[SystemPromptResponse.model_validate(p) for p in prompts],
        total=len(prompts),
    )


@router.post("", response_model=SystemPromptResponse, status_code=status.HTTP_201_CREATED)
async def create_system_prompt(
    prompt_data: SystemPromptCreate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SystemPromptResponse:
    existing = await db.execute(select(SystemPrompt).where(SystemPrompt.name == prompt_data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"System prompt with name '{prompt_data.name}' already exists",
        )

    if prompt_data.is_default:
        await _clear_default(db)

    prompt = SystemPrompt(**prompt_data.model_dump())
    db.add(prompt)
    await db.flush()
    await db.refresh(prompt)

    return SystemPromptResponse.model_validate(prompt)


@router.get("/{prompt_id}", response_model=SystemPromptResponse)
async def get_system_prompt_route(
    prompt_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SystemPromptResponse:
    result = await db.execute(select(SystemPrompt).where(SystemPrompt.id == prompt_id))
    prompt = result.scalar_one_or_none()

    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System prompt with ID '{prompt_id}' not found",
        )

    return SystemPromptResponse.model_validate(prompt)


@router.patch("/{prompt_id}", response_model=SystemPromptResponse)
async def update_system_prompt(
    prompt_id: UUID,
    prompt_data: SystemPromptUpdate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> SystemPromptResponse:
    result = await db.execute(select(SystemPrompt).where(SystemPrompt.id == prompt_id))
    prompt = result.scalar_one_or_none()

    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System prompt with ID '{prompt_id}' not found",
        )

    update_data = prompt_data.model_dump(exclude_unset=True)

    # Name is editable, so a rename must not collide with another prompt.
    new_name = update_data.get("name")
    if new_name and new_name != prompt.name:
        existing = await db.execute(select(SystemPrompt).where(SystemPrompt.name == new_name))
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"System prompt with name '{new_name}' already exists",
            )

    # The default is promoted, never demoted in place: clearing the only default
    # would leave the system with none. To change it, set another as default.
    if update_data.get("is_default") is True:
        await _clear_default(db, except_id=prompt.id)
    elif "is_default" in update_data and not update_data["is_default"] and prompt.is_default:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot unset the default; promote another system prompt instead",
        )

    # Deactivating the default would leave non-admins without a prompt.
    if update_data.get("is_active") is False and prompt.is_default:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot deactivate the default system prompt",
        )

    for field, value in update_data.items():
        setattr(prompt, field, value)

    await db.flush()
    await db.refresh(prompt)

    return SystemPromptResponse.model_validate(prompt)


@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_system_prompt(
    prompt_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(SystemPrompt).where(SystemPrompt.id == prompt_id))
    prompt = result.scalar_one_or_none()

    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System prompt with ID '{prompt_id}' not found",
        )

    if prompt.is_default:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete the default system prompt; promote another first",
        )

    # Hard delete: deactivation is already covered by PATCH is_active, and a
    # soft-deleted row would keep the unique name taken forever (409 on re-create).
    # Sessions referencing this prompt fall back to the default via the
    # ON DELETE SET NULL FK, and the default itself is undeletable (409 above).
    await db.delete(prompt)
    await db.flush()
