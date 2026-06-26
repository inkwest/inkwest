"""
Current-user self-service: preferences + personal skills.

Every endpoint operates on ``current_user`` ONLY — no admin role, no cross-user access.
Personal skills are user-authored SKILL.md documents that shadow admin skills of the
same name; the admin ``skills`` table and its endpoints are never touched here.
"""

import json
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_db
from app.core.config import get_settings
from app.core.skill_md import ParsedSkill, parse_skill_md
from app.core.slug import slugify
from app.models.schemas import (
    ConfigExport,
    ConfigImport,
    ExportSkill,
    PreferencesResponse,
    PreferencesUpdate,
    UserSkillCreate,
    UserSkillResponse,
    UserSkillUpdate,
)
from app.models.skill import Skill
from app.models.user_skill import UserSkill
from app.services.user_preferences import (
    CONFIG_SCHEMA_VERSION,
    ConfigImportError,
    get_preferences,
    plan_import,
    set_preferences_document,
)

router = APIRouter(prefix="/me", tags=["me"])


def _parse_or_422(content: str) -> ParsedSkill:
    """Parse a SKILL.md, surfacing malformed frontmatter as a 422."""
    try:
        return parse_skill_md(content)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e


def _enforce_skill_size(content: str) -> None:
    """Reject a personal SKILL.md whose body exceeds the per-skill byte quota."""
    limit = get_settings().user_skill_max_bytes
    if len(content.encode("utf-8")) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Skill exceeds the maximum size of {limit} bytes",
        )


async def _admin_skill_by_name(db: AsyncSession, name: str) -> Skill | None:
    """The admin skill with this exact (slugified) name, or None."""
    result = await db.execute(select(Skill).where(Skill.name == name))
    return result.scalar_one_or_none()


async def _count_active_skills(db: AsyncSession, user_id: UUID, *, exclude_id: UUID | None) -> int:
    """Count the user's ACTIVE personal skills, optionally excluding one row (an update)."""
    query = (
        select(func.count())
        .select_from(UserSkill)
        .where(
            UserSkill.user_id == user_id,
            UserSkill.is_active == True,  # noqa: E712
        )
    )
    if exclude_id is not None:
        query = query.where(UserSkill.id != exclude_id)
    return int((await db.execute(query)).scalar_one())


async def _enforce_active_quota(
    db: AsyncSession, user_id: UUID, *, exclude_id: UUID | None = None
) -> None:
    """Reject adding/activating a skill that would exceed the active-count quota.

    Call this only when the resulting row WILL be active; counts every other active row.
    """
    limit = get_settings().user_skills_max_count
    if await _count_active_skills(db, user_id, exclude_id=exclude_id) >= limit:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Active personal-skill quota reached (max {limit})",
        )


async def _user_skills(db: AsyncSession, user_id: UUID) -> list[UserSkill]:
    """All of the user's personal skills, ordered by name."""
    result = await db.execute(
        select(UserSkill).where(UserSkill.user_id == user_id).order_by(UserSkill.name)
    )
    return list(result.scalars().all())


async def _flush_or_conflict(db: AsyncSession, name: str) -> None:
    """Flush an inserted skill, turning a UNIQUE(user_id, name) violation into a 409.

    The pre-insert existence check narrows the window, but two concurrent creates of the
    same name both pass it and only the DB constraint catches the loser — surface that as
    a clean conflict, not a 500.
    """
    try:
        await db.flush()
    except IntegrityError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already have a personal skill named '{name}'",
        ) from e


# --------------------------------------------------------------------------- #
# Preferences document                                                        #
# --------------------------------------------------------------------------- #


@router.get("/preferences", response_model=PreferencesResponse)
async def get_my_preferences(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PreferencesResponse:
    prefs = await get_preferences(db, current_user.id)
    skills = await _user_skills(db, current_user.id)
    return PreferencesResponse(
        schema_version=prefs.schema_version if prefs is not None else 1,
        document=prefs.document if prefs is not None else {},
        skills=[UserSkillResponse.model_validate(s) for s in skills],
    )


@router.put("/preferences", response_model=PreferencesResponse)
async def update_my_preferences(
    payload: PreferencesUpdate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PreferencesResponse:
    limit = get_settings().user_prefs_doc_max_bytes
    serialized = json.dumps(payload.document, separators=(",", ":")).encode("utf-8")
    if len(serialized) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Preferences document exceeds the maximum size of {limit} bytes",
        )
    prefs = await set_preferences_document(db, current_user.id, payload.document)
    skills = await _user_skills(db, current_user.id)
    return PreferencesResponse(
        schema_version=prefs.schema_version,
        document=prefs.document,
        skills=[UserSkillResponse.model_validate(s) for s in skills],
    )


# --------------------------------------------------------------------------- #
# Personal skills CRUD                                                         #
# --------------------------------------------------------------------------- #


@router.get("/skills", response_model=list[UserSkillResponse])
async def list_my_skills(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[UserSkillResponse]:
    skills = await _user_skills(db, current_user.id)
    return [UserSkillResponse.model_validate(s) for s in skills]


@router.post("/skills", response_model=UserSkillResponse, status_code=status.HTTP_201_CREATED)
async def create_my_skill(
    payload: UserSkillCreate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserSkillResponse:
    parsed = _parse_or_422(payload.content)
    _enforce_skill_size(parsed.content)

    existing = await db.execute(
        select(UserSkill).where(UserSkill.user_id == current_user.id, UserSkill.name == parsed.slug)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already have a personal skill named '{parsed.slug}'",
        )
    await _enforce_active_quota(db, current_user.id)

    # is_override reflects whether this name shadows an admin skill (drives the badge);
    # a user-authored collision is NOT a fork, so source_admin_skill_id stays NULL.
    shadows_admin = await _admin_skill_by_name(db, parsed.slug) is not None
    skill = UserSkill(
        user_id=current_user.id,
        name=parsed.slug,
        content=parsed.content,
        description=parsed.description,
        is_override=shadows_admin,
    )
    db.add(skill)
    await _flush_or_conflict(db, parsed.slug)
    await db.refresh(skill)
    return UserSkillResponse.model_validate(skill)


async def _get_owned_skill(db: AsyncSession, user_id: UUID, skill_id: UUID) -> UserSkill:
    """Fetch a personal skill the user owns, or 404 (never leaks another user's row)."""
    result = await db.execute(
        select(UserSkill).where(UserSkill.id == skill_id, UserSkill.user_id == user_id)
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Personal skill '{skill_id}' not found",
        )
    return skill


@router.put("/skills/{skill_id}", response_model=UserSkillResponse)
async def update_my_skill(
    skill_id: UUID,
    payload: UserSkillUpdate,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserSkillResponse:
    skill = await _get_owned_skill(db, current_user.id, skill_id)
    update_data = payload.model_dump(exclude_unset=True)

    if "content" in update_data:
        parsed = _parse_or_422(update_data["content"])
        _enforce_skill_size(parsed.content)
        if parsed.slug != skill.name:
            clash = await db.execute(
                select(UserSkill).where(
                    UserSkill.user_id == current_user.id, UserSkill.name == parsed.slug
                )
            )
            if clash.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"You already have a personal skill named '{parsed.slug}'",
                )
        skill.name = parsed.slug
        skill.content = parsed.content
        skill.description = parsed.description
        # Re-derive shadowing after a possible rename; a manual edit is not a fork, so the
        # provenance link is cleared when it no longer shadows an admin skill.
        admin = await _admin_skill_by_name(db, parsed.slug)
        skill.is_override = admin is not None
        if admin is None:
            skill.source_admin_skill_id = None

    if "is_active" in update_data:
        # Re-activating counts against the active quota; deactivating never does.
        if update_data["is_active"] and not skill.is_active:
            await _enforce_active_quota(db, current_user.id, exclude_id=skill.id)
        skill.is_active = update_data["is_active"]

    await db.flush()
    await db.refresh(skill)
    return UserSkillResponse.model_validate(skill)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_skill(
    skill_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    skill = await _get_owned_skill(db, current_user.id, skill_id)
    await db.delete(skill)
    await db.flush()


@router.post(
    "/skills/override/{admin_name}",
    response_model=UserSkillResponse,
    status_code=status.HTTP_201_CREATED,
)
async def override_admin_skill(
    admin_name: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> UserSkillResponse:
    """Fork an admin skill into an editable personal skill that instantly shadows it."""
    # Admin skills are stored under their normalized slug; normalize the path param too so
    # a caller passing the human name (or a differently-cased slug) still resolves.
    admin = await _admin_skill_by_name(db, slugify(admin_name))
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Admin skill '{admin_name}' not found",
        )

    existing = await db.execute(
        select(UserSkill).where(UserSkill.user_id == current_user.id, UserSkill.name == admin.name)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"You already have a personal skill named '{admin.name}'",
        )
    await _enforce_active_quota(db, current_user.id)

    skill = UserSkill(
        user_id=current_user.id,
        name=admin.name,
        content=admin.content,
        description=admin.description,
        is_active=True,
        is_override=True,
        source_admin_skill_id=admin.id,
    )
    db.add(skill)
    await _flush_or_conflict(db, admin.name)
    await db.refresh(skill)
    return UserSkillResponse.model_validate(skill)


# --------------------------------------------------------------------------- #
# Export / import (versioned, self-only)                                       #
# --------------------------------------------------------------------------- #


@router.get("/export", response_model=ConfigExport)
async def export_my_config(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConfigExport:
    """Download the user's preferences + personal skills as a versioned, re-importable doc."""
    prefs = await get_preferences(db, current_user.id)
    skills = await _user_skills(db, current_user.id)
    return ConfigExport(
        schema_version=CONFIG_SCHEMA_VERSION,
        exported_at=datetime.now(UTC),
        preferences=prefs.document if prefs is not None else {},
        skills=[
            ExportSkill(
                name=s.name, content=s.content, is_active=s.is_active, is_override=s.is_override
            )
            for s in skills
        ],
    )


async def _admin_skill_names(db: AsyncSession) -> set[str]:
    """All admin skill names (for recomputing is_override on import)."""
    result = await db.execute(select(Skill.name))
    return set(result.scalars().all())


@router.post("/import", response_model=PreferencesResponse)
async def import_my_config(
    payload: ConfigImport,
    current_user: CurrentUser,
    replace_all: bool = False,
    db: AsyncSession = Depends(get_db),
) -> PreferencesResponse:
    """Apply an exported config to the CURRENT user only.

    Self-only: never touches admin skills or other users. ``replace_all=false`` (default)
    upserts by name and keeps personal skills absent from the file; ``replace_all=true``
    first deletes all the user's personal skills. The whole import is validated before any
    write (atomic within the request transaction) — a bad entry rejects the lot.
    """
    existing = await _user_skills(db, current_user.id)
    try:
        plan = plan_import(
            schema_version=payload.schema_version,
            document=payload.preferences,
            raw_skills=[(s.content, s.is_active) for s in payload.skills],
            replace_all=replace_all,
            existing_active={s.name: s.is_active for s in existing},
            admin_names=await _admin_skill_names(db),
        )
    except ConfigImportError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e

    # Apply skills, then the preferences document — all in the request transaction, which
    # get_db commits at the end and rolls back wholesale on any error (atomic import).
    by_name = {s.name: s for s in existing}
    if plan.replace_all:
        for stale in existing:
            await db.delete(stale)
        await db.flush()  # release UNIQUE(user_id, name) before re-inserting same names
        by_name = {}

    for p in plan.skills:
        row = by_name.get(p.name)
        if row is not None:
            row.content = p.content
            row.description = p.description
            row.is_active = p.is_active
            row.is_override = p.is_override
            row.source_admin_skill_id = None  # an import is not a fork
        else:
            db.add(
                UserSkill(
                    user_id=current_user.id,
                    name=p.name,
                    content=p.content,
                    description=p.description,
                    is_active=p.is_active,
                    is_override=p.is_override,
                )
            )
    await db.flush()

    prefs = await set_preferences_document(db, current_user.id, plan.document)
    skills = await _user_skills(db, current_user.id)
    return PreferencesResponse(
        schema_version=prefs.schema_version,
        document=prefs.document,
        skills=[UserSkillResponse.model_validate(s) for s in skills],
    )
