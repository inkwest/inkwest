"""
Per-user preferences + effective-skills resolution.

Unlike :mod:`app.services.app_settings` (a tiny global table cached in process), per-user
state is fetched on demand — one prefs row + one personal-skills query per turn is cheap,
and an all-users in-memory cache would be a correctness/footprint hazard across replicas.
Writes go through the request's ``get_db`` (flush here, commit there), matching the skills
route. The only document field the backend interprets is ``skills.disabled_admin``; the
rest (UI settings) is opaque storage owned by the frontend, bounded only by a size quota.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.skill import Skill
from app.models.user_preferences import UserPreferences
from app.models.user_skill import UserSkill


def _disabled_admin(document: dict[str, Any]) -> list[str]:
    """The admin-skill names the user turned OFF, defensively parsed.

    The document is user-supplied JSON (import/PUT), so every level is type-checked:
    a malformed ``skills``/``disabled_admin`` degrades to "nothing disabled" rather
    than raising mid-turn.
    """
    skills = document.get("skills") if isinstance(document, dict) else None
    if not isinstance(skills, dict):
        return []
    disabled = skills.get("disabled_admin")
    if not isinstance(disabled, list):
        return []
    return [name for name in disabled if isinstance(name, str)]


async def get_preferences(db: AsyncSession, user_id: uuid.UUID) -> UserPreferences | None:
    """The user's preferences row, or None if they have never saved any."""
    result = await db.execute(select(UserPreferences).where(UserPreferences.user_id == user_id))
    return result.scalar_one_or_none()


async def get_preferences_document(db: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """The user's preferences document, or an empty dict when unset."""
    prefs = await get_preferences(db, user_id)
    return prefs.document if prefs is not None else {}


async def set_preferences_document(
    db: AsyncSession, user_id: uuid.UUID, document: dict[str, Any]
) -> UserPreferences:
    """Upsert the user's preferences document (atomic ON CONFLICT, like app_settings).

    Flushes within the caller's transaction; the request's ``get_db`` commits. Size/shape
    validation is the route's job (quota + schema) — this is the persistence primitive.
    """
    stmt = (
        pg_insert(UserPreferences)
        .values(user_id=user_id, document=document)
        .on_conflict_do_update(
            index_elements=[UserPreferences.user_id],
            set_={"document": document},
        )
        .returning(UserPreferences)
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.scalar_one()


async def get_effective_skills(
    db: AsyncSession, user_id: uuid.UUID | None
) -> list[Skill | UserSkill]:
    """The skills materialized for ``user_id``'s turn, ordered by name.

    Precedence: active admin skills the user has NOT disabled, then the user's active
    personal skills — which **always win** on a name collision (one SKILL.md per slug).
    ``user_id is None`` (ownerless turn) degrades to the global active-admin set.
    """
    admin_result = await db.execute(
        select(Skill).where(Skill.is_active == True)  # noqa: E712
    )
    admin = list(admin_result.scalars().all())
    if user_id is None:
        return sorted(admin, key=lambda s: s.name)

    disabled = set(_disabled_admin(await get_preferences_document(db, user_id)))
    user_result = await db.execute(
        select(UserSkill).where(
            UserSkill.user_id == user_id,
            UserSkill.is_active == True,  # noqa: E712
        )
    )
    effective: dict[str, Skill | UserSkill] = {s.name: s for s in admin if s.name not in disabled}
    for personal in user_result.scalars().all():
        effective[personal.name] = personal  # personal always shadows admin
    return [effective[name] for name in sorted(effective)]
