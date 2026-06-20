"""
Skills-as-tools executor for the standalone agent (P1.7).

The Converse loop has no native skill system (the claude-cli engine does), so
skills are re-expressed as a single synthetic ``read_skill`` tool — *progressive
disclosure*: the tool's description lists each skill's short description (always
visible), and the SKILL.md **body** is loaded only when the model calls the tool.

The broker materializes each skill's SKILL.md into the sandbox (parity with
``app.services.session_skills.apply_session_skills``) under ``skills_root`` and
advertises ``skills[{name, description}]`` in the turn-spec; this module reads the
body on demand and returns it as the tool_result. Bundled scripts a skill ships
run via the existing kubectl/connector tools — nothing extra needed here.
"""

import os

from app.core.slug import slugify

from .converse import READ_SKILL_TOOL
from .protocol import SkillSpec


def is_skill_tool(name: str) -> bool:
    """Whether a tool call targets the synthetic skill loader."""
    return name == READ_SKILL_TOOL


def read_skill(
    tool_input: dict[str, object],
    skills: list[SkillSpec],
    skills_root: str | None,
) -> tuple[str, bool]:
    """Load the requested skill's SKILL.md body. Returns (content, is_error).

    The model passes ``{"name": "<skill>"}``. We resolve it against the advertised
    set (rejecting anything off-list so a hallucinated name can't read an arbitrary
    file), then read ``<skills_root>/<slug>/SKILL.md`` — the path the broker wrote,
    using the same slug rule as ``apply_session_skills`` so the two always agree.
    """
    requested = tool_input.get("name")
    if not isinstance(requested, str) or not requested:
        return ("Error: 'name' (the skill to load) is required.", True)

    known = {s.name for s in skills}
    if requested not in known:
        available = ", ".join(sorted(known)) or "(none)"
        return (f"Error: unknown skill '{requested}'. Available skills: {available}.", True)

    if not skills_root:
        return ("Error: skills are not available in this session.", True)

    # Same slug rule as the broker's materialization (session_skills.apply_session_skills),
    # so the file we read is exactly the one written. Path stays inside skills_root.
    path = os.path.join(skills_root, slugify(requested), "SKILL.md")
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read()
    except FileNotFoundError:
        return (f"Error: skill '{requested}' is advertised but its body is missing.", True)
    except OSError as e:
        return (f"Error reading skill '{requested}': {e}", True)

    return (body, False)
