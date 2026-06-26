"""
Per-user preferences + effective-skills resolution.

Unlike :mod:`app.services.app_settings` (a tiny global table cached in process), per-user
state is fetched on demand — one prefs row + one personal-skills query per turn is cheap,
and an all-users in-memory cache would be a correctness/footprint hazard across replicas.
Writes go through the request's ``get_db`` (flush here, commit there), matching the skills
route. The only document field the backend interprets is ``skills.disabled_admin``; the
rest (UI settings) is opaque storage owned by the frontend, bounded only by a size quota.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.skill_md import parse_skill_md
from app.models.skill import Skill
from app.models.user_preferences import UserPreferences
from app.models.user_skill import UserSkill

# Version of the export/import config envelope. Bump only on a breaking shape change;
# add a forward migration in plan_import when you do.
CONFIG_SCHEMA_VERSION = 1


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


# --------------------------------------------------------------------------- #
# Export / import config (versioned, self-only)                               #
# --------------------------------------------------------------------------- #


class ConfigImportError(Exception):
    """A rejected import, carrying the HTTP status the route should surface."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class PlannedSkill:
    """A personal skill resolved from an import entry, ready to upsert."""

    name: str
    content: str
    description: str | None
    is_active: bool
    is_override: bool


@dataclass(frozen=True)
class ImportPlan:
    """A validated, side-effect-free import: the document to store + the skills to apply."""

    document: dict[str, Any]
    skills: list[PlannedSkill]
    replace_all: bool


def plan_import(
    *,
    schema_version: int,
    document: dict[str, Any],
    raw_skills: list[tuple[str, bool]],
    replace_all: bool,
    existing_active: dict[str, bool],
    admin_names: set[str],
) -> ImportPlan:
    """Validate an import and compute what to apply — PURE, raises before any DB write.

    ``raw_skills`` is ``[(content, is_active), ...]``; the slug/description come from the
    SKILL.md frontmatter (the source of truth), so a file's stale ``name`` can't desync.
    ``existing_active`` maps the user's current skill names → is_active (for upsert quota
    math). ``is_override`` is recomputed against ``admin_names`` regardless of the file's
    value. Quotas come from settings. The whole import is rejected (never partially
    applied) on any malformed/oversized/duplicate entry or quota breach.
    """
    settings = get_settings()
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ConfigImportError(
            422,
            f"Unsupported config schema_version {schema_version} "
            f"(this server supports {CONFIG_SCHEMA_VERSION})",
        )

    doc_bytes = len(json.dumps(document, separators=(",", ":")).encode("utf-8"))
    if doc_bytes > settings.user_prefs_doc_max_bytes:
        raise ConfigImportError(
            413, f"Preferences document exceeds {settings.user_prefs_doc_max_bytes} bytes"
        )

    planned: list[PlannedSkill] = []
    seen: set[str] = set()
    for content, is_active in raw_skills:
        try:
            parsed = parse_skill_md(content)
        except ValueError as e:
            raise ConfigImportError(422, f"Invalid skill in import: {e}") from e
        if len(parsed.content.encode("utf-8")) > settings.user_skill_max_bytes:
            raise ConfigImportError(
                413, f"Skill '{parsed.slug}' exceeds {settings.user_skill_max_bytes} bytes"
            )
        if parsed.slug in seen:
            raise ConfigImportError(422, f"Duplicate skill '{parsed.slug}' in import")
        seen.add(parsed.slug)
        planned.append(
            PlannedSkill(
                name=parsed.slug,
                content=parsed.content,
                description=parsed.description,
                is_active=is_active,
                is_override=parsed.slug in admin_names,
            )
        )

    # Resulting active-skill count after applying the plan (replace wipes first; upsert
    # keeps existing names not present in the file).
    if replace_all:
        resulting_active = {p.name for p in planned if p.is_active}
    else:
        merged = dict(existing_active)
        for p in planned:
            merged[p.name] = p.is_active
        resulting_active = {name for name, active in merged.items() if active}
    if len(resulting_active) > settings.user_skills_max_count:
        raise ConfigImportError(
            409,
            f"Import would exceed the active personal-skill quota "
            f"(max {settings.user_skills_max_count})",
        )

    return ImportPlan(document=document, skills=planned, replace_all=replace_all)
