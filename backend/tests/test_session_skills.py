"""
Tests for skill SKILL.md parsing + per-conversation generation.

A skill's ``content`` is a full SKILL.md (frontmatter + body): name/description/arguments
are parsed from the frontmatter, the content is written verbatim to the sandbox, and
legacy (pre-frontmatter) rows are migrated idempotently.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.skill_md import legacy_to_skill_md, parse_skill_md
from app.core.slug import parse_skill_name, slugify
from app.services.session_skills import apply_session_skills, ensure_skills_frontmatter

SKILL_MD = """---
name: summarize-mr
description: Summarize a GitLab MR
arguments: [mr_url, --publish]
---
Summarize the MR. $ARGUMENTS
"""


def _skill(name: str, content: str) -> MagicMock:
    s = MagicMock()
    s.name = name
    s.content = content
    return s


# --- slug helpers (still used by the legacy migration) -----------------------


def test_slugify():
    assert slugify("Déploiement EKS") == "deploiement-eks"
    assert slugify("***") == "skill"  # never empty


def test_parse_skill_name():
    assert parse_skill_name("Summarize MR(merge request URL)") == (
        "summarize-mr",
        "merge request URL",
    )
    assert parse_skill_name("Diagnose Pod") == ("diagnose-pod", None)


# --- SKILL.md parser ---------------------------------------------------------


def test_parse_skill_md_extracts_and_normalizes_name():
    p = parse_skill_md(
        "---\nname: Summarize MR\ndescription: Summ\narguments: [mr_url, --publish]\n"
        "---\nBody $ARGUMENTS\n"
    )
    assert p.slug == "summarize-mr"
    assert p.description == "Summ"
    assert p.argument_hint == "mr_url, --publish"
    assert "name: summarize-mr" in p.content  # name normalized to the slug in the stored md
    assert "Body $ARGUMENTS" in p.content  # body preserved


def test_parse_skill_md_argument_hint_string_form():
    assert (
        parse_skill_md('---\nname: x\nargument-hint: "[url]"\n---\nbody\n').argument_hint == "[url]"
    )


def test_parse_skill_md_normalizes_crlf():
    # A SKILL.md pasted with Windows CRLF must still parse, and store LF-only.
    p = parse_skill_md("---\r\nname: X\r\ndescription: d\r\n---\r\nBody $ARGUMENTS\r\n")
    assert p.slug == "x"
    assert "\r" not in p.content
    assert "Body $ARGUMENTS" in p.content


def test_parse_skill_md_rejects_missing_frontmatter():
    with pytest.raises(ValueError):
        parse_skill_md("no frontmatter here")


def test_parse_skill_md_rejects_missing_name():
    with pytest.raises(ValueError):
        parse_skill_md("---\ndescription: d\n---\nbody\n")


# --- legacy → SKILL.md migration ---------------------------------------------


def test_legacy_to_skill_md():
    slug, content = legacy_to_skill_md("Summarize MR(merge request URL)", "Do it.", "Summarize")
    assert slug == "summarize-mr"
    assert content.startswith("---\nname: summarize-mr\n")
    assert 'description: "Summarize"' in content
    assert 'argument-hint: "merge request URL"' in content
    assert content.rstrip().endswith("$ARGUMENTS")  # appended when body had no placeholder


# --- SkillResponse computed fields -------------------------------------------


def test_response_slug_and_hint_from_frontmatter():
    from app.models.schemas import SkillResponse

    src = SimpleNamespace(
        id=uuid.uuid4(),
        name="summarize-mr",
        content=SKILL_MD,
        description="Summarize a GitLab MR",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    resp = SkillResponse.model_validate(src)
    assert resp.slug == "summarize-mr"
    assert resp.argument_hint == "mr_url, --publish"


# --- generation: write content verbatim --------------------------------------


def test_apply_is_noop_off_sandbox():
    with patch(
        "app.services.session_skills.get_settings", return_value=MagicMock(sandbox_enabled=False)
    ):
        apply_session_skills(str(uuid.uuid4()), [_skill("x", SKILL_MD)])


def test_apply_writes_verbatim_and_prunes_orphans(tmp_path):
    sid = str(uuid.uuid4())
    root = tmp_path / "claude" / "skills"
    a = _skill("summarize-mr", SKILL_MD)
    b = _skill("diagnose-pod", "---\nname: diagnose-pod\n---\nDiagnose.\n")
    with (
        patch(
            "app.services.session_skills.get_settings", return_value=MagicMock(sandbox_enabled=True)
        ),
        patch("app.services.sandbox.session_dir", return_value=str(tmp_path)),
    ):
        apply_session_skills(sid, [a, b])
        assert (root / "summarize-mr" / "SKILL.md").read_text() == SKILL_MD  # verbatim
        assert (root / "diagnose-pod" / "SKILL.md").is_file()

        apply_session_skills(sid, [a])  # b removed → pruned
        assert not (root / "diagnose-pod").exists()


# --- legacy migration over the DB --------------------------------------------


async def test_ensure_skills_frontmatter_converts_only_legacy():
    legacy = MagicMock(
        name="ignored", content="Do it.", description="Summarize"
    )  # MagicMock(name=…) is special; set name explicitly below
    legacy.name = "Summarize MR(merge request URL)"
    modern = MagicMock(content="---\nname: x\n---\nbody")
    modern.name = "x"

    scalars = MagicMock()
    scalars.all.return_value = [legacy, modern]
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=exec_result)

    await ensure_skills_frontmatter(db)

    assert legacy.name == "summarize-mr"
    assert legacy.content.startswith("---\nname: summarize-mr\n")
    assert modern.content == "---\nname: x\n---\nbody"  # already frontmatter → untouched
    db.commit.assert_awaited_once()


async def test_ensure_skills_frontmatter_dedups_colliding_slugs():
    # Two legacy names slugifying to the same base must NOT both become "summarize-mr"
    # (name is UNIQUE → IntegrityError → startup crash); the 2nd is suffixed.
    a = MagicMock(content="Body A.", description=None)
    a.name = "Summarize MR(prod)"
    b = MagicMock(content="Body B.", description=None)
    b.name = "Summarize MR(staging)"

    scalars = MagicMock()
    scalars.all.return_value = [a, b]
    exec_result = MagicMock()
    exec_result.scalars.return_value = scalars
    db = AsyncMock()
    db.execute = AsyncMock(return_value=exec_result)

    await ensure_skills_frontmatter(db)

    assert {a.name, b.name} == {"summarize-mr", "summarize-mr-2"}  # distinct → no collision
    db.commit.assert_awaited_once()
