"""
Parse a SKILL.md document (YAML frontmatter + body) — the single source of truth
for a skill.

A skill is authored as one ``content`` field holding a full Claude Code SKILL.md:

    ---
    name: pod-crash-triage
    description: Triages a crash-looping pod from its events and logs …
    arguments: [pod, --namespace]
    ---
    <instructions…>

``name``/``description``/``arguments`` are read from the frontmatter (not separate
DB inputs). The ``name`` is normalized to a slug so the sandbox directory, the
``/<slug>`` trigger and the frontmatter agree (no path traversal — ``slugify``
constrains it to ``[a-z0-9-]``).
"""

import json
import re
from dataclasses import dataclass

import yaml

from app.core.slug import parse_skill_name, slugify

# A SKILL.md: leading "---", YAML frontmatter, "---", then the body.
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", re.DOTALL)


@dataclass(frozen=True)
class ParsedSkill:
    slug: str  # normalized frontmatter name → dir + /trigger + the name: line
    description: str | None
    argument_hint: str | None
    content: str  # the full SKILL.md, with the name: line normalized to the slug


def _format_arg_hint(arguments: object) -> str | None:
    """Render the frontmatter ``arguments`` (YAML list or string) as a hint string."""
    if arguments is None:
        return None
    if isinstance(arguments, list):
        items = [str(a).strip() for a in arguments if str(a).strip()]
        return ", ".join(items) or None
    text = str(arguments).strip()
    return text or None


def parse_skill_md(content: str) -> ParsedSkill:
    """Parse a SKILL.md ``content`` string. Raises ``ValueError`` if invalid.

    Validation: must start with a YAML frontmatter block carrying a non-empty
    ``name``. The returned ``content`` has its ``name:`` normalized to the slug.
    """
    # Normalize line endings so a SKILL.md pasted from a Windows/CRLF source still
    # matches the frontmatter regex (and the stored/generated file stays LF-only).
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    m = _FRONTMATTER_RE.match(content.lstrip("\n"))
    if not m:
        raise ValueError("Content must start with a YAML frontmatter block: --- … ---")
    fm_text, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML frontmatter: {e}") from e
    if not isinstance(fm, dict):
        raise ValueError("Frontmatter must be a YAML mapping.")

    raw_name = fm.get("name")
    if raw_name is None or not str(raw_name).strip():
        raise ValueError("Frontmatter must include a non-empty 'name'.")
    slug = slugify(str(raw_name))

    description = fm.get("description")
    description = str(description).strip() if description is not None else None
    argument_hint = _format_arg_hint(fm.get("argument-hint") or fm.get("arguments"))

    # Normalize the name line to the slug (Option B) so dir/trigger/file all agree,
    # leaving the rest of the frontmatter untouched.
    new_fm = re.sub(r"(?m)^([ \t]*name:).*$", rf"\1 {slug}", fm_text, count=1)
    normalized = f"---\n{new_fm}\n---\n{body}"
    return ParsedSkill(
        slug=slug, description=description, argument_hint=argument_hint, content=normalized
    )


def frontmatter_argument_hint(content: str) -> str | None:
    """Argument hint from a SKILL.md frontmatter; None if absent or unparseable."""
    try:
        return parse_skill_md(content).argument_hint
    except ValueError:
        return None


def legacy_to_skill_md(
    name: str, body: str, description: str | None, slug: str | None = None
) -> tuple[str, str]:
    """Convert a pre-frontmatter skill (old ``name`` with ``(...)`` + plain body) to
    ``(slug, full SKILL.md)``. Used to migrate rows created before the SKILL.md format.

    ``slug`` defaults to the slugified base of ``name``; pass an explicit one to resolve
    a collision during migration (the frontmatter ``name`` uses it too).
    """
    base_slug, arg_hint = parse_skill_name(name)
    final_slug = slug or base_slug
    lines = [f"name: {final_slug}"]
    if description:
        lines.append(f"description: {json.dumps(description, ensure_ascii=False)}")
    if arg_hint:
        lines.append(f"argument-hint: {json.dumps(arg_hint, ensure_ascii=False)}")
    trimmed = body.rstrip()
    if not re.search(r"\$(?:ARGUMENTS|\d)", trimmed):
        trimmed = f"{trimmed}\n\n$ARGUMENTS"
    content = "---\n" + "\n".join(lines) + "\n---\n" + trimmed + "\n"
    return final_slug, content
