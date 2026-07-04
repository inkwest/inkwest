"""
Ephemeral per-turn grant for the LLM auth broker.

The sandbox carries only this short-lived, opaque token in ``AWS_BEARER_TOKEN_BEDROCK`` — never
the real Bedrock API key, which stays in the backend process. The broker
(:mod:`app.api.routes.llm_proxy`) verifies the token, then swaps in the real bearer per request.

Why an opaque token, NOT a JWT: the grant must be **bound to one turn and burned when the turn
ends** (input → the agent makes its Bedrock calls → turn ends → token dead). A JWT is stateless
— it cannot be revoked before its ``exp`` without a server-side store, which defeats its only
advantage. So this is a CSPRNG token resolved against an in-process registry: ``mint`` at turn
start inserts it, the broker ``verify``s by lookup, and the orchestrator ``burn``s it in its
``finally``. A leaked token is then useless the moment the turn ends — the exfiltration window
is the turn's duration, not a fixed JWT lifetime, and the grant existed only for that sandbox.

The TTL is a **backstop only** (cleans up a grant orphaned when the backend is SIGKILLed before
its ``finally`` runs); the explicit ``burn`` is the primary path. The registry lives in this
process and is touched only from the event loop (orchestrator mint/burn + broker route verify),
so no lock is needed — but it assumes the documented single-replica deployment: a sandbox's
broker call must reach the SAME backend process that minted its token (see the metrics note in
``app.core.config``). Scale-out needs a shared store or sticky routing.
"""

import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Backstop lifetime for an un-burned grant (safety net for a crashed orchestrator, not the
# normal path). Turns can legitimately run minutes (streaming + several tool calls); 2 h is
# comfortably longer than any turn while still bounding a leaked-on-crash grant.
_TTL_SECONDS = 2 * 60 * 60

# Human-recognizable prefix on every grant: makes it obvious in logs, /proc/<pid>/environ, or
# a Bedrock error body that this is an Inkwest ephemeral grant — never a real Bedrock API key
# (which is base64 `ABSK…`). Cosmetic only; the 256-bit random suffix is what's unguessable.
_TOKEN_PREFIX = "inkwest-ephemeral-"

# Hard upper bound on live grants. The normal path burns each grant in the orchestrator
# `finally`, so the store holds ~one entry per concurrent turn. This cap is a memory backstop:
# if `burn` is repeatedly skipped (SIGKILL/OOM mid-turn before `finally`), un-burned grants
# would otherwise accumulate up to their full TTL during a mint storm. At the cap, mint evicts
# the OLDEST-expiring grant — bounding store memory regardless of whether burns ever run. Far
# above any realistic concurrent-turn count, so it never evicts a live, in-use grant in practice.
_MAX_GRANTS = 10_000


class ProxyAuthError(Exception):
    """The broker grant is missing, expired, or already burned."""


@dataclass(frozen=True)
class LlmProxyClaims:
    """Identity behind a live grant. Audit/budget correlation only — NOT a request gate.

    ``engine`` and ``turn_id`` are supplied by the orchestrator at mint time (it knows the
    resolved engine and owns the per-turn id) so broker usage rows carry the real engine and
    aggregate per turn.
    """

    user_id: str | None
    session_id: str | None
    engine: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True)
class _Grant:
    user_id: str | None
    session_id: str | None
    expires_at: float  # time.monotonic() deadline (backstop)
    engine: str | None = None
    turn_id: str | None = None


# token -> grant. Event-loop-confined; see module docstring for the single-replica assumption.
_grants: dict[str, _Grant] = {}


def _sweep(now: float) -> None:
    """Drop expired grants. Opportunistic (called on mint) so orphans from a hard crash don't
    accumulate unbounded — the access path also lazily expires the one it looks up."""
    expired = [tok for tok, g in _grants.items() if g.expires_at <= now]
    for tok in expired:
        del _grants[tok]


def mint(
    user_id: object | None,
    session_id: object | None,
    engine: str | None = None,
    turn_id: object | None = None,
) -> str:
    """Mint a fresh opaque grant for this turn and register it. Returns the token to inject.

    ``user_id``/``session_id`` are stored for audit only (stringified); they never gate a
    request. ``engine``/``turn_id`` attribute the broker's usage rows (engine + per-turn
    aggregation) — supplied by the orchestrator, also never a gate. A new turn always gets a
    new token — there is no reuse to keep an injected env stable, because the env is
    re-applied per turn via ``docker exec -e`` (nothing to churn).
    """
    now = time.monotonic()
    _sweep(now)
    # Memory backstop: if expired-grant sweeping didn't bring the store back under the cap (a
    # burst of un-burned, not-yet-expired grants), evict the oldest-expiring ones so the store
    # can never grow without bound between TTLs. Logged so a real leak is visible, not silent.
    if len(_grants) >= _MAX_GRANTS:
        overflow = len(_grants) - _MAX_GRANTS + 1
        for tok in sorted(_grants, key=lambda t: _grants[t].expires_at)[:overflow]:
            del _grants[tok]
        logger.warning(
            "llm_proxy_grant_store_capped",
            extra={"evicted": overflow, "cap": _MAX_GRANTS},
        )
    # 256 bits of CSPRNG entropy (unguessable) behind a recognizable, non-secret prefix.
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    _grants[token] = _Grant(
        user_id=str(user_id) if user_id is not None else None,
        session_id=str(session_id) if session_id is not None else None,
        expires_at=now + _TTL_SECONDS,
        engine=engine,
        turn_id=str(turn_id) if turn_id is not None else None,
    )
    return token


def verify(token: str) -> LlmProxyClaims:
    """Resolve a live grant to its claims, or raise :class:`ProxyAuthError`.

    A token that is unknown (never minted, or already burned), or past its backstop TTL, is
    rejected — so a replay after the turn's ``burn`` fails closed.
    """
    grant = _grants.get(token)
    if grant is None:
        raise ProxyAuthError("unknown or burned grant")
    if grant.expires_at <= time.monotonic():
        _grants.pop(token, None)  # lazily expire the orphan we just found
        raise ProxyAuthError("expired grant")
    return LlmProxyClaims(
        user_id=grant.user_id,
        session_id=grant.session_id,
        engine=grant.engine,
        turn_id=grant.turn_id,
    )


def burn(token: str | None) -> None:
    """Invalidate a grant at turn end. Idempotent and None-safe (callable from a ``finally``)."""
    if token:
        _grants.pop(token, None)
