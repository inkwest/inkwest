"""
Silent renewal of expired connector credentials from their warm material.

A minted credential can outlive its usable lifetime: GitLab holds a refresh token
(policy-capped window) and AWS holds the ~8h SSO access token next to the ~1h STS
profiles. When a caller needs a credential and finds it expired, the helpers here
re-derive a fresh one in place — no device flow, no OAuth redirect, no popup —
and store it back. Single-flight per (user, session, connector): a burst of
concurrent tool executions hitting the same expired credential performs exactly
one provider round-trip (GitLab refresh-token rotation makes a concurrent second
refresh self-destructive, not just wasteful).
"""

import asyncio
import logging
from uuid import UUID

from app.services.connectors.base import ConnectorError, ConnectorType, MintedCredential
from app.services.connectors.registry import get_connector
from app.services.connectors.store import get_credential_store

logger = logging.getLogger(__name__)

# Keyed locks; bounded by active (user, session, connector) triples — same accepted
# growth profile as the credential store itself.
_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def _lock_for(user_id: UUID, session_id: UUID, connector: ConnectorType) -> asyncio.Lock:
    return _locks.setdefault((str(user_id), str(session_id), connector), asyncio.Lock())


async def get_or_renew_cred(
    user_id: UUID | None, session_id: UUID, connector: ConnectorType
) -> MintedCredential | None:
    """Return a live credential, silently renewing an expired-but-warm one.

    Returns None when nothing is held or the warm material is gone/dead — callers
    fall back to their normal needs-auth path. Renewal failures degrade to None
    (never raise): a broken silent renewal must read as "re-connect", not a 500.
    """
    if user_id is None:
        return None
    store = get_credential_store()
    cred = await store.get(user_id, session_id, connector)
    if cred is not None:
        return cred
    held = await store.get(user_id, session_id, connector, include_expired=True)
    if held is None:
        return None

    async with _lock_for(user_id, session_id, connector):
        # Post-lock recheck: a competitor on this lock may have renewed already.
        cred = await store.get(user_id, session_id, connector)
        if cred is not None:
            return cred
        held = await store.get(user_id, session_id, connector, include_expired=True)
        if held is None:
            return None
        fresh = await _renew(user_id, held)
        if fresh is None:
            return None
        # Persist before returning: with GitLab rotation the old refresh token is
        # already dead — the rotated one must be in the store before first use.
        await store.put(user_id, session_id, fresh)
        if connector == "aws":
            _refresh_aws_projections(session_id, fresh)
        logger.info(
            "connector_silent_renewal",
            extra={"connector": connector, "session_id": str(session_id)},
        )
        return fresh


async def _renew(user_id: UUID, held: MintedCredential) -> MintedCredential | None:
    from app.services.connectors.aws import AwsConnector
    from app.services.connectors.warm import grant_gitlab_from_warm

    conn = get_connector(held.connector)
    if conn is None:  # connector disabled since the grant
        return None
    if held.connector == "gitlab":
        # The refresh token is user-scoped warm material (rotation-safe home);
        # session credentials never carry it.
        return await grant_gitlab_from_warm(user_id)
    try:
        if isinstance(conn, AwsConnector):
            return await conn.remint_profiles(held)
    except ConnectorError as e:
        logger.info(
            "connector_silent_renewal_failed",
            extra={"connector": held.connector, "reason": str(e)},
        )
    return None


def _refresh_aws_projections(session_id: UUID, cred: MintedCredential) -> None:
    """Rewrite the tmpfs ~/.aws projection and reschedule the EKS kubeconfig refresh.

    Kubeconfigs reference profiles by name, so only the credentials file and the
    refresh timer (keyed to the old expiry) need updating.
    """
    # Local imports: connector_exec imports this module for the exec-time renewal
    # hook, so importing it at module level would be circular.
    from app.services.eks_refresh import schedule_session_eks_refresh
    from app.tools.connector_exec import write_aws_profiles

    write_aws_profiles(session_id, cred)
    schedule_session_eks_refresh(session_id, cred)
