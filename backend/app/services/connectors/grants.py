"""
Derive a session's "previously-granted connectors" from the durable audit log.

Minted credentials live only in RAM and are evicted on expiry, so the credential
store can't tell that "this session had AWS connected to account X" once the cred
is gone. ``connector_audit_log`` is the only durable record. This module reduces it
to the set of connectors a session most recently intended to have, plus the AWS
account/role selection to re-apply on renewal. It never reads or returns secrets.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connector_audit import ConnectorAuditLog

# Latest action that means the connector is currently intended (vs "revoke").
_GRANTED_ACTIONS = {"grant", "select"}


@dataclass
class PriorGrant:
    """A connector a session previously granted, with its last AWS selection (if any)."""

    connector: str
    selection: dict[str, Any] | None = None


async def derive_prior_grants(db: AsyncSession, session_id: uuid.UUID) -> list[PriorGrant]:
    """Reduce ``connector_audit_log`` to the connectors a session can renew.

    A connector is renewable if the session has *ever* granted or selected it. A
    later ``revoke`` does NOT remove it: "Renew access" is precisely how a user
    re-establishes access they revoked or that expired. For AWS the account/role
    selection is recovered from the most recent ``select`` row (a bare device
    ``grant`` carries ``selection=None``).

    Scoped by ``session_id`` only: session ownership is enforced by the caller, and
    historic rows may carry a null ``user_id``.
    """
    rows = (
        await db.execute(
            select(
                ConnectorAuditLog.connector,
                ConnectorAuditLog.action,
                ConnectorAuditLog.selection,
            )
            .where(ConnectorAuditLog.session_id == session_id)
            .order_by(ConnectorAuditLog.occurred_at.desc())
        )
    ).all()

    # Rows are newest-first. Track every connector ever granted/selected (ordered),
    # plus each connector's most recent non-null select selection.
    ever_granted: dict[str, None] = {}
    latest_selection: dict[str, dict[str, Any]] = {}
    for connector, action, selection in rows:
        if action in _GRANTED_ACTIONS:
            ever_granted.setdefault(connector, None)
        if action == "select" and selection is not None:
            latest_selection.setdefault(connector, selection)

    return [
        PriorGrant(connector=connector, selection=latest_selection.get(connector))
        for connector in ever_granted
    ]


def aws_selections_from_grant(grant: PriorGrant) -> list[dict[str, str]]:
    """Map a recovered AWS selection to the ``aws/select`` payload shape.

    ``selection == {"profiles": [{"account_id", "role_name", ...}, ...]}``; returns
    ``[]`` when there is no usable selection (caller falls back to manual pick).
    """
    profiles = (grant.selection or {}).get("profiles") or []
    return [
        {"account_id": p["account_id"], "role_name": p["role_name"]}
        for p in profiles
        if p.get("account_id") and p.get("role_name")
    ]
