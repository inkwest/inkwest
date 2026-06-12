"""
Generate per-conversation Claude skill files from the DB skills.

Each active skill is materialized as a Claude skill at
``<CLAUDE_CONFIG_DIR>/skills/<slug>/SKILL.md`` inside the conversation's sandbox, so
the agent can trigger it explicitly (``/<slug> <args>``) and the model can auto-invoke
it based on the skill ``description``.

Mirrors the per-conversation MCP reconcile in :mod:`app.services.mcp_config`: the
skills dir is owned entirely by Inkwest, reconciled to a desired state (write the
active set, prune the rest), with idempotent atomic writes, and a no-op outside
sandbox mode. The orchestrator regenerates it every turn from the DB — the CLI reads
its config at launch, so the next turn of every session picks up template edits and
the set survives a sandbox rebuild.
"""

import contextlib
import json
import logging
import os
import re
import shutil
import tempfile

from app.core.config import get_settings
from app.core.slug import parse_skill_name
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


def render_skill_md(template: Skill, slug: str, argument_hint: str | None = None) -> str:
    """Render one template as ``SKILL.md`` content (frontmatter + body).

    No ``disable-model-invocation`` flag: the model may auto-invoke based on
    ``description``. ``$ARGUMENTS`` is appended when the body references no argument
    placeholder, so a ``/<slug> args`` invocation reaches the body — while staying
    tolerant of an empty value when the model auto-invokes without arguments. The
    optional ``argument-hint`` mirrors the inline ``(...)`` signature from the name.
    """
    # Collapse whitespace to single valid YAML lines; JSON-dump (a valid YAML scalar)
    # to escape any quote/colon in the user-authored text.
    description = " ".join((template.description or template.name).split())
    body = template.content.rstrip()
    if not re.search(r"\$(?:ARGUMENTS|\d)", body):
        body = f"{body}\n\n$ARGUMENTS"
    lines = [f"name: {slug}", f"description: {json.dumps(description, ensure_ascii=False)}"]
    if argument_hint:
        lines.append(f"argument-hint: {json.dumps(argument_hint, ensure_ascii=False)}")
    frontmatter = "---\n" + "\n".join(lines) + "\n---\n"
    return f"{frontmatter}{body}\n"


def apply_session_skills(session_id: str, templates: list[Skill]) -> None:
    """Reconcile the conversation's skills dir to ``templates`` (no-op off-sandbox).

    Idempotent: unchanged ``SKILL.md`` files are left untouched; folders for templates
    no longer present are pruned.
    """
    target = skills_dir(session_id)
    if target is None:
        return

    desired: dict[str, str] = {}
    for t in templates:
        slug, argument_hint = parse_skill_name(t.name)
        if slug in desired:
            # name is unique, but two distinct names can slugify alike; keep the first
            # deterministically and skip the collision rather than overwrite.
            logger.warning("skill_slug_collision", extra={"slug": slug, "name": t.name})
            continue
        desired[slug] = render_skill_md(t, slug, argument_hint)

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
