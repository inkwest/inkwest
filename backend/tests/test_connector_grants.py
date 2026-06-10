"""Tests for deriving a session's previously-granted connector set from the audit log."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from app.services.connectors.grants import (
    PriorGrant,
    aws_selections_from_grant,
    derive_prior_grants,
)


def _db_with_rows(rows: list[tuple]) -> AsyncMock:
    """Mock AsyncSession whose execute(...).all() yields (connector, action, selection) rows."""
    db = AsyncMock()
    result = MagicMock()
    result.all.return_value = rows
    db.execute = AsyncMock(return_value=result)
    return db


_SEL = {"profiles": [{"account_id": "111", "role_name": "ReadOnly", "name": "acme-ReadOnly"}]}


async def test_grant_only_included_without_selection() -> None:
    db = _db_with_rows([("gitlab", "grant", None), ("aws", "grant", None)])
    grants = {g.connector: g for g in await derive_prior_grants(db, uuid.uuid4())}
    assert set(grants) == {"gitlab", "aws"}
    assert grants["gitlab"].selection is None


async def test_aws_grant_then_select_recovers_selection() -> None:
    # Newest-first: select (latest) above the bare device grant.
    db = _db_with_rows([("aws", "select", _SEL), ("aws", "grant", None)])
    grants = await derive_prior_grants(db, uuid.uuid4())
    assert len(grants) == 1
    assert grants[0].connector == "aws"
    assert grants[0].selection == _SEL


async def test_revoke_does_not_remove_a_prior_grant() -> None:
    # Revoking then renewing is the whole point — a revoked connector stays renewable.
    db = _db_with_rows([("gitlab", "revoke", None), ("gitlab", "grant", None)])
    grants = await derive_prior_grants(db, uuid.uuid4())
    assert [g.connector for g in grants] == ["gitlab"]


async def test_revoked_aws_still_recovers_its_selection() -> None:
    # Revoke AWS after a select: still renewable, and the saved selection survives.
    db = _db_with_rows([("aws", "revoke", None), ("aws", "select", _SEL), ("aws", "grant", None)])
    grants = await derive_prior_grants(db, uuid.uuid4())
    assert len(grants) == 1
    assert grants[0].connector == "aws"
    assert grants[0].selection == _SEL


async def test_connector_never_granted_is_absent() -> None:
    # A connector that only ever appears as revoke (shouldn't happen, but be safe).
    db = _db_with_rows([("gitlab", "revoke", None)])
    assert await derive_prior_grants(db, uuid.uuid4()) == []


async def test_aws_device_regrant_keeps_last_known_selection() -> None:
    # AWS re-ran device auth (latest is a bare grant, selection None) after a prior
    # select. It stays intended, and the older select's selection is recovered.
    db = _db_with_rows([("aws", "grant", None), ("aws", "select", _SEL)])
    grants = await derive_prior_grants(db, uuid.uuid4())
    assert grants[0].connector == "aws"
    assert grants[0].selection == _SEL


def test_aws_selections_from_grant_maps_profiles() -> None:
    grant = PriorGrant(connector="aws", selection=_SEL)
    assert aws_selections_from_grant(grant) == [{"account_id": "111", "role_name": "ReadOnly"}]


def test_aws_selections_from_grant_empty_when_no_selection() -> None:
    assert aws_selections_from_grant(PriorGrant(connector="aws", selection=None)) == []
    assert aws_selections_from_grant(PriorGrant(connector="aws", selection={})) == []
