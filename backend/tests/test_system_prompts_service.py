"""Unit tests for system-prompt resolution/seed helpers."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.prompts import (
    _LEGACY_SEEDED_PROMPTS,
    SYSTEM_PROMPT,
    ensure_default_system_prompt,
    get_system_prompt,
    resolve_session_system_prompt,
)


def test_base_prompt_is_connector_agnostic() -> None:
    # the default prompt must not frame Inkwest as Kubernetes-only and
    # must surface the other connectors generically.
    assert "investigation assistant" in SYSTEM_PROMPT.lower()
    assert "specialized in debugging kubernetes" not in SYSTEM_PROMPT.lower()
    for connector in ("Kubernetes", "AWS", "GitLab", "Coralogix"):
        assert connector in SYSTEM_PROMPT
    # kubectl read-only safety constraints are preserved.
    assert "read-only" in SYSTEM_PROMPT.lower()
    assert "kubectl" in SYSTEM_PROMPT
    # Runtime context tokens still present for get_system_prompt() replacement.
    assert "{cluster_name}" in SYSTEM_PROMPT
    assert "{namespaces}" in SYSTEM_PROMPT


def test_current_prompt_not_treated_as_legacy() -> None:
    # Guard the migration contract: the live SYSTEM_PROMPT must never appear in the
    # legacy set, otherwise ensure_default_system_prompt would consider an already
    # up-to-date Default "refreshable" — and, more importantly, this catches a future
    # edit to SYSTEM_PROMPT that forgot to append the previous body to the set
    # (which would silently strand unmodified Defaults on the old wording).
    assert SYSTEM_PROMPT not in _LEGACY_SEEDED_PROMPTS


def test_get_system_prompt_fills_context_from_base() -> None:
    base = "You are a bot for {cluster_name}, namespaces {namespaces}."
    out = get_system_prompt("prod", ["default", "kube-system"], base_prompt=base)
    assert "prod" in out
    assert "default, kube-system" in out


def test_get_system_prompt_custom_with_stray_braces_does_not_raise() -> None:
    # Admin free-text may contain JSON-like braces; literal replacement must not
    # treat them as format fields.
    base = 'Reply as JSON {"k": "v"} for cluster {cluster_name}.'
    out = get_system_prompt("c1", None, base_prompt=base)
    assert '{"k": "v"}' in out
    assert "c1" in out


def test_get_system_prompt_falls_back_to_hardcoded() -> None:
    out = get_system_prompt("c1", None, base_prompt=None)
    assert "Inkwest" in out  # from the hardcoded SYSTEM_PROMPT
    assert SYSTEM_PROMPT.split("\n", 1)[0].split("{")[0][:20] in out


@pytest.mark.asyncio
async def test_resolve_prefers_custom() -> None:
    session = MagicMock()
    session.system_prompt_custom = "be terse"
    session.system_prompt_id = uuid.uuid4()
    db = MagicMock()
    db.get = AsyncMock(return_value=session)

    assert await resolve_session_system_prompt(db, uuid.uuid4()) == "be terse"


@pytest.mark.asyncio
async def test_resolve_uses_selected_prompt() -> None:
    session = MagicMock()
    session.system_prompt_custom = None
    session.system_prompt_id = uuid.uuid4()
    selected = MagicMock()
    selected.is_active = True
    selected.content = "selected body"
    db = MagicMock()
    db.get = AsyncMock(side_effect=[session, selected])

    assert await resolve_session_system_prompt(db, uuid.uuid4()) == "selected body"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_default() -> None:
    session = MagicMock()
    session.system_prompt_custom = None
    session.system_prompt_id = None
    default = MagicMock()
    default.content = "default body"
    db = MagicMock()
    db.get = AsyncMock(return_value=session)
    db.scalar = AsyncMock(return_value=default)

    assert await resolve_session_system_prompt(db, uuid.uuid4()) == "default body"


@pytest.mark.asyncio
async def test_resolve_returns_none_when_nothing_found() -> None:
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.scalar = AsyncMock(return_value=None)

    assert await resolve_session_system_prompt(db, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_ensure_default_seeds_when_empty() -> None:
    db = MagicMock()
    db.scalar = AsyncMock(return_value=0)
    db.add = MagicMock()
    db.commit = AsyncMock()

    await ensure_default_system_prompt(db)

    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_default_no_insert_when_rows_exist() -> None:
    # Non-empty table: never inserts a second Default row.
    existing = MagicMock()
    existing.content = SYSTEM_PROMPT  # already current → nothing to refresh
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[1, existing])
    db.add = MagicMock()
    db.commit = AsyncMock()

    await ensure_default_system_prompt(db)

    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_default_reseeds_unmodified_legacy_default() -> None:
    # Reframe migration: an unmodified, previously-seeded Default gets its body
    # refreshed in place to the new connector-agnostic prompt.
    legacy = next(iter(_LEGACY_SEEDED_PROMPTS))
    row = MagicMock()
    row.content = legacy
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[1, row])
    db.add = MagicMock()
    db.commit = AsyncMock()

    await ensure_default_system_prompt(db)

    assert row.content == SYSTEM_PROMPT
    db.add.assert_not_called()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_default_does_not_clobber_admin_edited_default() -> None:
    # An admin-edited Default body must survive: not in the legacy set → untouched.
    row = MagicMock()
    row.content = "Admin-customised prompt body that must not be overwritten."
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[1, row])
    db.add = MagicMock()
    db.commit = AsyncMock()

    await ensure_default_system_prompt(db)

    assert row.content == "Admin-customised prompt body that must not be overwritten."
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_default_handles_missing_default_row() -> None:
    # Rows exist but none named "Default" (admin renamed it): leave everything be.
    db = MagicMock()
    db.scalar = AsyncMock(side_effect=[2, None])
    db.add = MagicMock()
    db.commit = AsyncMock()

    await ensure_default_system_prompt(db)

    db.add.assert_not_called()
    db.commit.assert_not_called()
