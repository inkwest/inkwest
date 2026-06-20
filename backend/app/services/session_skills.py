"""
Generate per-conversation Claude skill files from the DB skills.

Each skill's ``content`` IS a full SKILL.md (frontmatter + body, authored in the admin
UI). It is written **verbatim** to ``<CLAUDE_CONFIG_DIR>/skills/<slug>/SKILL.md`` inside
the conversation's sandbox, so the agent can trigger it (``/<slug> <args>``) and the
model can auto-invoke it from the frontmatter ``description``.

Mirrors the per-conversation MCP reconcile in :mod:`app.services.mcp_config`: the skills
dir is owned entirely by Inkwest, reconciled to a desired state (write the active set,
prune the rest), idempotent atomic writes, no-op outside sandbox mode. The orchestrator
regenerates it every turn from the DB.
"""

import contextlib
import logging
import os
import shutil
import tempfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.skill_md import legacy_to_skill_md
from app.core.slug import slugify
from app.models.skill import Skill

logger = logging.getLogger(__name__)


def skills_dir(session_id: str) -> str | None:
    """Host path of the conversation's Claude skills dir, or None outside sandbox mode.

    Sibling of the conversation's ``.claude.json`` (see ``mcp_config.claude_json_path``):
    both live under the ``claude/`` mount that maps to ``<sandbox_home>/.claude``.
    """
    if not get_settings().sandbox_enabled:
        return None
    # Imported here to avoid a hard import-time dependency on the sandbox module.
    from app.services import sandbox

    return os.path.join(sandbox.session_dir(session_id), "claude", "skills")


def skills_container_root() -> str:
    """In-container path of the skills dir (the inkwest-agent reads SKILL.md from here).

    The host ``claude/skills`` dir is mounted at ``<sandbox_home>/.claude`` in-container
    (see app.services.sandbox), so the agent resolves a skill at
    ``<sandbox_home>/.claude/skills/<slug>/SKILL.md`` — the broker passes this root in the
    turn-spec (claude-cli has no equivalent: it discovers skills from the same files itself).
    """
    return f"{get_settings().sandbox_home.rstrip('/')}/.claude/skills"


def apply_session_skills(session_id: str, skills: list[Skill]) -> None:
    """Reconcile the conversation's skills dir to ``skills`` (no-op off-sandbox).

    Each skill's ``content`` is already a full SKILL.md → written verbatim. Idempotent:
    unchanged files are left untouched; folders for skills no longer present are pruned.
    """
    target = skills_dir(session_id)
    if target is None:
        return

    desired: dict[str, str] = {}
    for skill in skills:
        slug = slugify(skill.name)  # name is the normalized slug; slugify is idempotent
        if slug in desired:
            # name is unique, but two distinct names can slugify alike; keep the first
            # deterministically and skip the collision rather than overwrite.
            logger.warning("skill_slug_collision", extra={"slug": slug, "skill_name": skill.name})
            continue
        content = skill.content if skill.content.endswith("\n") else skill.content + "\n"
        desired[slug] = content

    # 0755 so the toolbox (a different uid) can traverse/read; per-conversation dir.
    os.makedirs(target, mode=0o755, exist_ok=True)
    os.chmod(target, 0o755)

    existing: set[str] = set()
    with contextlib.suppress(FileNotFoundError):
        existing = {n for n in os.listdir(target) if os.path.isdir(os.path.join(target, n))}
    for slug in existing - desired.keys():
        shutil.rmtree(os.path.join(target, slug), ignore_errors=True)

    for slug, content in desired.items():
        skill_dir = os.path.join(target, slug)
        os.makedirs(skill_dir, mode=0o755, exist_ok=True)
        os.chmod(skill_dir, 0o755)
        path = os.path.join(skill_dir, "SKILL.md")
        if _read(path) != content:  # idempotent: skip rewrite when unchanged
            _write_atomic(path, content)


async def ensure_skills_frontmatter(db: AsyncSession) -> None:
    """Idempotently migrate legacy skills (``content`` without a frontmatter) to the
    SKILL.md format, rebuilding the frontmatter from the old ``name`` (``(...)`` arg
    signature) + ``description``. No-op once converted; run at startup.
    """
    skills = list((await db.execute(select(Skill))).scalars().all())
    # `name` is UNIQUE: a legacy slug must not collide with a modern row's name or with
    # another legacy slug — else the single commit below raises IntegrityError and crashes
    # startup. The old "(...)" name encoding makes collisions plausible (e.g. two skills
    # sharing a base name), so suffix on collision to keep the migration always safe.
    taken = {s.name for s in skills if s.content.lstrip().startswith("---")}
    converted = 0
    for skill in skills:
        if skill.content.lstrip().startswith("---"):
            continue  # already a SKILL.md
        slug, content = legacy_to_skill_md(skill.name, skill.content, skill.description)
        if slug in taken:
            n = 2
            while f"{slug}-{n}" in taken:
                n += 1
            resolved = f"{slug}-{n}"
            logger.warning(
                "skill_slug_collision_on_migration",
                extra={"skill_name": skill.name, "slug": slug, "resolved": resolved},
            )
            slug, content = legacy_to_skill_md(
                skill.name, skill.content, skill.description, slug=resolved
            )
        taken.add(slug)
        skill.name = slug
        skill.content = content
        converted += 1
    if converted:
        await db.commit()
        logger.info("skills_frontmatter_migrated", extra={"count": converted})


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write_atomic(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".SKILL.md.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o644)  # toolbox (other uid) must read the skill
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
