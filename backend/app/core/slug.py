"""
Slugify a skill name into a Claude skill identifier.

The slug is the skill's directory name AND its ``/<slug>`` trigger, so it must be a
valid Claude skill name (lowercase ASCII letters, digits, single hyphens). Kept a
pure, dependency-free function so the API response (``app.models.schemas``) and the
sandbox skill generator (``app.services.session_skills``) derive the SAME slug.
"""

import re
import unicodedata


def slugify(name: str) -> str:
    """Return a filesystem/command-safe slug for ``name`` (never empty)."""
    # Strip accents to ASCII first (French names are common) so "Déploiement EKS"
    # becomes "deploiement-eks" rather than dropping the accented letters.
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name).strip("-").lower()
    return slug or "skill"


def parse_skill_name(name: str) -> tuple[str, str | None]:
    """Split a template name into ``(slug, argument_hint)``.

    A trailing ``(...)`` in the name is an inline argument signature — e.g.
    ``"Summarize MR(merge request URL, ticket)"`` → slug ``summarize-mr`` and hint
    ``"merge request URL, ticket"``. Parsed BEFORE slugifying so the parens never leak
    into the ``/<slug>`` trigger. No trailing ``(...)`` → ``(slugify(name), None)``.
    """
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", name)
    if m:
        hint = m.group(2).strip()
        return slugify(m.group(1)), (hint or None)
    return slugify(name), None
