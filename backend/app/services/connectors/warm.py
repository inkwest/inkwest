"""
Per-user warm platform grants (RAM-only).

The credential store is session-scoped by design — but the *platform login* (AWS
device flow, GitLab OAuth redirect) yields material that is user-scoped: the ~8h
SSO access token, the GitLab refresh token. Holding that material per (user,
platform) lets a new conversation grant access with just the account/role or
project selection — no login replay.

This is the RAM-only precursor of the durable ``platform_grants`` layer designed
in docs/connector-hot-auth.md: same shape, same policy cap, no persistence yet
(a backend restart still means one fresh login per user per platform).

GitLab note: the refresh token lives ONLY here, never in session credentials.
GitLab rotates it on every use (single-use), so two sessions each holding a copy
would brick each other's renewal; all refreshes are serialized per user here and
the rotated token is persisted back before the new access token is used.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from app.services.connectors.base import ConnectorError, ConnectorType, MintedCredential

logger = logging.getLogger(__name__)

# Policy cap: a warm grant never outlives this, whatever the provider allows
# (GitLab refresh tokens never expire provider-side). "Re-consent once a day."
_WARM_WINDOW: Final = timedelta(hours=24)

_Key = tuple[str, str]


@dataclass
class WarmGrant:
    """User-level renewable platform material. ``secret`` never leaves the backend."""

    platform: ConnectorType
    secret: dict[str, Any] = field(repr=False, default_factory=dict)
    not_after: datetime = field(default_factory=lambda: datetime.now(UTC))

    def is_live(self) -> bool:
        return datetime.now(UTC) < self.not_after


class WarmGrantStore:
    """Async-safe in-RAM store of warm grants, keyed (user_id, platform)."""

    def __init__(self) -> None:
        self._grants: dict[_Key, WarmGrant] = {}
        self._lock = asyncio.Lock()
        # Per (user, platform) operation locks: refresh-token rotation must be
        # single-flight across every session of the user.
        self._op_locks: dict[_Key, asyncio.Lock] = {}

    @staticmethod
    def _key(user_id: UUID, platform: ConnectorType) -> _Key:
        return (str(user_id), platform)

    def op_lock(self, user_id: UUID, platform: ConnectorType) -> asyncio.Lock:
        return self._op_locks.setdefault(self._key(user_id, platform), asyncio.Lock())

    async def put(self, user_id: UUID, grant: WarmGrant) -> None:
        async with self._lock:
            self._grants[self._key(user_id, grant.platform)] = grant

    async def get(self, user_id: UUID, platform: ConnectorType) -> WarmGrant | None:
        async with self._lock:
            k = self._key(user_id, platform)
            grant = self._grants.get(k)
            if grant is None:
                return None
            if not grant.is_live():
                del self._grants[k]
                return None
            return grant

    async def delete(self, user_id: UUID, platform: ConnectorType) -> None:
        async with self._lock:
            self._grants.pop(self._key(user_id, platform), None)

    async def sweep(self) -> int:
        async with self._lock:
            dead = [k for k, g in self._grants.items() if not g.is_live()]
            for k in dead:
                del self._grants[k]
            return len(dead)


_store: WarmGrantStore | None = None


def get_warm_store() -> WarmGrantStore:
    """Return the process-wide warm grant store singleton."""
    global _store
    if _store is None:
        _store = WarmGrantStore()
    return _store


def warm_window_cap(provider_not_after: datetime | None = None) -> datetime:
    """The grant's ``not_after``: provider lifetime, capped by the policy window."""
    cap = datetime.now(UTC) + _WARM_WINDOW
    if provider_not_after is None:
        return cap
    return min(provider_not_after, cap)


# --------------------------------------------------------------------------- #
# Grant-from-warm: derive session credentials without a login flow
# --------------------------------------------------------------------------- #
async def store_gitlab_warm(user_id: UUID, refresh_token: str | None) -> None:
    """Capture the refresh token from a fresh OAuth grant as the user's warm grant."""
    if not refresh_token:
        return
    await get_warm_store().put(
        user_id,
        WarmGrant(
            platform="gitlab",
            secret={"refresh_token": refresh_token},
            not_after=warm_window_cap(),
        ),
    )


async def store_aws_warm(user_id: UUID, provisional: MintedCredential) -> None:
    """Capture the SSO token from a fresh device grant as the user's warm grant."""
    access_token = provisional.secret.get("sso_access_token")
    if not access_token:
        return
    await get_warm_store().put(
        user_id,
        WarmGrant(
            platform="aws",
            secret={
                "sso_access_token": access_token,
                "sso_refresh_token": provisional.secret.get("sso_refresh_token"),
                "region": provisional.secret.get("region"),
            },
            not_after=warm_window_cap(provisional.expires_at),
        ),
    )


async def grant_gitlab_from_warm(user_id: UUID) -> MintedCredential | None:
    """Mint a session GitLab credential from the user's warm refresh token.

    Returns None when no live warm grant exists or the refresh fails (grant is
    then dropped — the next attempt falls back to the OAuth flow). The returned
    credential carries the access token only; the rotated refresh token stays in
    the warm store, persisted before the new access token is handed out.
    """
    from app.services.connectors.gitlab import GitLabConnector

    store = get_warm_store()
    if await store.get(user_id, "gitlab") is None:
        return None
    async with store.op_lock(user_id, "gitlab"):
        warm = await store.get(user_id, "gitlab")  # re-check under the lock
        if warm is None:
            return None
        try:
            cred = await GitLabConnector().refresh_from_token(warm.secret["refresh_token"])
        except ConnectorError as e:
            logger.info("gitlab_warm_refresh_failed", extra={"reason": str(e), "fatal": e.fatal})
            # Only a fatal failure (invalid_grant — token revoked or rotated
            # elsewhere) kills the grant. A transient provider error must NOT:
            # destroying a 24h grant over a 10s blip forces a needless re-login.
            if e.fatal:
                await store.delete(user_id, "gitlab")
            return None
        rotated = cred.secret.pop("refresh_token", None)
        if rotated:
            warm.secret["refresh_token"] = rotated
            await store.put(user_id, warm)  # persist rotation BEFORE first use
        cred.renew_until = warm.not_after
        return cred


async def grant_aws_provisional_from_warm(user_id: UUID) -> MintedCredential | None:
    """Build a provisional (``needs_selection``) AWS credential from the warm SSO token.

    Enumerates accounts/roles with the held token — the user goes straight to the
    selection popup, no device flow. Returns None when no live warm grant exists
    or enumeration fails (token revoked SSO-side; the grant is dropped).
    """
    from app.services.connectors.aws import AwsConnector

    store = get_warm_store()
    warm = await store.get(user_id, "aws")
    if warm is None:
        return None
    expires_in = int((warm.not_after - datetime.now(UTC)).total_seconds())
    try:
        cred = await AwsConnector().provisional_from_token(
            warm.secret["sso_access_token"],
            expires_in,
            refresh_token=warm.secret.get("sso_refresh_token"),
        )
    except Exception as e:  # noqa: BLE001 — boto raises varied client errors here
        fatal = _aws_error_is_fatal(e)
        logger.info("aws_warm_enumeration_failed", extra={"reason": str(e), "fatal": fatal})
        # Same policy as GitLab: only a dead SSO token kills the grant; a
        # throttle/timeout/5xx keeps it for the next attempt.
        if fatal:
            await store.delete(user_id, "aws")
        return None
    return cred


def _aws_error_is_fatal(e: Exception) -> bool:
    """Whether a boto error means the SSO token itself is dead (vs a provider blip)."""
    resp = getattr(e, "response", None)  # botocore ClientError shape
    code = resp.get("Error", {}).get("Code", "") if isinstance(resp, dict) else ""
    return code in {"UnauthorizedException", "ExpiredTokenException", "AccessDeniedException"}
