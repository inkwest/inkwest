"""
Tests for generating Claude skills from skills.

Invariants: name → stable slug; SKILL.md carries the description + a usable
``$ARGUMENTS`` placeholder and NO ``disable-model-invocation`` (auto-invocation stays
on); the skills dir is reconciled to the active set (orphans pruned); no-op off-sandbox.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.core.slug import parse_skill_name, slugify
from app.services.session_skills import (
    apply_session_skills,
    render_skill_md,
)


def _tmpl(name: str, content: str = "Do the thing.", description: str | None = "Investigate X"):
    t = MagicMock()
    t.name = name
    t.content = content
    t.description = description
    return t


def test_slugify():
    assert slugify("Déploiement EKS") == "deploiement-eks"
    assert slugify("  Multiple   spaces!! ") == "multiple-spaces"
    assert slugify("already-slug") == "already-slug"
    assert slugify("***") == "skill"  # never empty


def test_parse_skill_name():
    # Trailing "(...)" is an inline arg signature → split from the slug.
    assert parse_skill_name("Summarize MR(merge request URL, ticket)") == (
        "summarize-mr",
        "merge request URL, ticket",
    )
    assert parse_skill_name("Diagnose Pod") == ("diagnose-pod", None)
    assert parse_skill_name("Foo()") == ("foo", None)  # empty parens → no hint
    # The parens must NOT leak into the slug.
    assert parse_skill_name("Summarize MR(url)")[0] == "summarize-mr"


def test_render_appends_arguments_and_has_no_disable_flag():
    md = render_skill_md(_tmpl("Analyse logs", content="Analyse the logs."), "analyse-logs")
    assert md.startswith("---\nname: analyse-logs\n")
    assert "\ndescription: " in md
    assert "disable-model-invocation" not in md  # auto-invocation stays enabled
    assert md.rstrip().endswith("$ARGUMENTS")  # appended since body had no placeholder


def test_render_keeps_existing_placeholder():
    md = render_skill_md(_tmpl("X", content="Check $ARGUMENTS now."), "x")
    assert md.count("$ARGUMENTS") == 1  # not duplicated
    md2 = render_skill_md(_tmpl("Y", content="First $1 then $2."), "y")
    assert "$ARGUMENTS" not in md2  # positional placeholders count as present


def test_render_argument_hint_frontmatter():
    md = render_skill_md(_tmpl("X"), "x", "merge request URL")
    assert 'argument-hint: "merge request URL"' in md
    assert "argument-hint:" not in render_skill_md(_tmpl("X"), "x")  # omitted when absent


def test_render_description_is_single_safe_yaml_line():
    md = render_skill_md(_tmpl("X", description='Use: when "logs" fail\nmulti-line'), "x")
    desc_lines = [ln for ln in md.splitlines() if ln.startswith("description: ")]
    assert len(desc_lines) == 1
    assert desc_lines[0].endswith('"')  # JSON-quoted scalar, collapsed to one line


def test_response_exposes_computed_slug():
    from app.models.schemas import SkillResponse

    src = SimpleNamespace(
        id=uuid.uuid4(),
        name="Analyse Logs",
        content="x",
        description="d",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    resp = SkillResponse.model_validate(src)
    assert resp.slug == "analyse-logs"
    assert resp.argument_hint is None


def test_response_exposes_slug_and_hint_parsed_from_name():
    from app.models.schemas import SkillResponse

    src = SimpleNamespace(
        id=uuid.uuid4(),
        name="Summarize MR(merge request URL)",
        content="x",
        description="d",
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    resp = SkillResponse.model_validate(src)
    assert resp.slug == "summarize-mr"  # parens stripped from the trigger
    assert resp.argument_hint == "merge request URL"


def test_apply_is_noop_when_sandbox_disabled():
    settings = MagicMock(sandbox_enabled=False)
    with patch("app.services.session_skills.get_settings", return_value=settings):
        # Must not raise nor need a session dir.
        apply_session_skills(str(uuid.uuid4()), [_tmpl("A")])


def test_apply_writes_set_and_prunes_orphans(tmp_path):
    sid = str(uuid.uuid4())
    skills_root = tmp_path / "claude" / "skills"
    settings = MagicMock(sandbox_enabled=True)
    with (
        patch("app.services.session_skills.get_settings", return_value=settings),
        patch("app.services.sandbox.session_dir", return_value=str(tmp_path)),
    ):
        apply_session_skills(sid, [_tmpl("Analyse Logs"), _tmpl("Summarize Incident")])
        assert (skills_root / "analyse-logs" / "SKILL.md").is_file()
        assert (skills_root / "summarize-incident" / "SKILL.md").is_file()

        # Re-apply with one template removed → its folder is pruned, the other stays.
        apply_session_skills(sid, [_tmpl("Analyse Logs")])
        assert (skills_root / "analyse-logs" / "SKILL.md").is_file()
        assert not (skills_root / "summarize-incident").exists()


def test_apply_uses_clean_slug_and_argument_hint_from_name(tmp_path):
    sid = str(uuid.uuid4())
    settings = MagicMock(sandbox_enabled=True)
    with (
        patch("app.services.session_skills.get_settings", return_value=settings),
        patch("app.services.sandbox.session_dir", return_value=str(tmp_path)),
    ):
        apply_session_skills(sid, [_tmpl("Summarize MR(merge request URL)")])
    skill = tmp_path / "claude" / "skills" / "summarize-mr" / "SKILL.md"
    assert skill.is_file()  # slug clean — no parens in the folder name
    assert 'argument-hint: "merge request URL"' in skill.read_text()
