"""
Internal JWT for the LLM auth broker (port of ``mcp_proxy_token`` to the model call).

The sandbox carries only this short-lived internal token in ``AWS_BEARER_TOKEN_BEDROCK`` —
never the real Bedrock API key, which stays in the backend process. The broker
(:mod:`app.api.routes.llm_proxy`) verifies it, then swaps in the real bearer per request.

HS256 on the shared ``Settings.secret_key`` (same key as the user-session, WS-ticket and
MCP-proxy tokens), so the verifier MUST enforce both ``aud`` and ``type`` — otherwise the
token classes are interchangeable on the shared key. ``sub``/``sid`` (user + session) ride
the token for audit/budget (future-proofing) but are NOT enforced against the request: the
broker restricts by ``model_id``/``op``, not by identity, so a malformed ``sub``/``sid`` must
never fail a model call — they are read leniently, unlike the MCP proxy's strict UUID claims.

TTL is 6 h (mirrors the MCP proxy): comfortably longer than a turn so ``mint_stable`` is a
no-op across a conversation, while short enough that a leaked token self-expires. Unlike the
MCP proxy's token this never lands on durable sandbox disk (it is injected per turn via
``docker exec -e``), so the durability concern that pinned the MCP TTL does not apply here.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from app.core.auth import JWT_ALGORITHM
from app.core.config import get_settings

logger = logging.getLogger(__name__)

_AUD = "bedrock-proxy"
_TYPE = "bedrock-proxy"
_TTL = timedelta(hours=6)
# Re-mint only when the existing token has less than this left — keeps the per-turn injected
# env stable so an idempotent re-mint doesn't churn it (parity with mcp_proxy_token).
_MINT_STABLE_FLOOR = timedelta(hours=1)


class ProxyAuthError(Exception):
    """The internal broker JWT is missing, malformed, or not valid for the model call."""


@dataclass(frozen=True)
class LlmProxyClaims:
    """Identity carried by the broker JWT. Audit/budget correlation only — NOT enforced.

    ``user_id``/``session_id`` are the raw ``sub``/``sid`` strings (or None when absent); they
    are deliberately not parsed as UUIDs, so a non-UUID value never blocks a model call.
    """

    user_id: str | None
    session_id: str | None


def mint(user_id: UUID | str | None, session_id: UUID | str | None) -> str:
    """Mint a fresh internal broker JWT carrying ``(user, session)`` for audit."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "aud": _AUD,
        "type": _TYPE,
        "iat": now,
        "exp": now + _TTL,
    }
    if user_id is not None:
        payload["sub"] = str(user_id)
    if session_id is not None:
        payload["sid"] = str(session_id)
    return jwt.encode(payload, get_settings().secret_key, algorithm=JWT_ALGORITHM)


def verify(token: str) -> LlmProxyClaims:
    """Verify a broker JWT. Raises :class:`ProxyAuthError` on any failure.

    Enforces signature, expiry, ``aud`` and ``type`` (so a session/ws/mcp-proxy token signed
    with the same secret can't be replayed here). ``sub``/``sid`` are returned as-is — they
    carry no authorization weight, so they are never a rejection reason.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[JWT_ALGORITHM],
            audience=_AUD,
        )
    except jwt.InvalidTokenError as e:
        raise ProxyAuthError(f"invalid broker token: {type(e).__name__}") from e

    if payload.get("type") != _TYPE:
        raise ProxyAuthError("wrong token type")
    sub = payload.get("sub")
    sid = payload.get("sid")
    return LlmProxyClaims(
        user_id=str(sub) if sub is not None else None,
        session_id=str(sid) if sid is not None else None,
    )


def mint_stable(
    existing: str | None, user_id: UUID | str | None, session_id: UUID | str | None
) -> str:
    """Return ``existing`` if it is still valid for this grant with > 1 h left, else mint anew.

    Keeps the per-turn injected ``AWS_BEARER_TOKEN_BEDROCK`` stable across turns.
    """
    if existing:
        try:
            payload = jwt.decode(
                existing,
                get_settings().secret_key,
                algorithms=[JWT_ALGORITHM],
                audience=_AUD,
            )
            same_grant = (
                payload.get("type") == _TYPE
                and payload.get("sub") == (str(user_id) if user_id is not None else None)
                and payload.get("sid") == (str(session_id) if session_id is not None else None)
            )
            exp = datetime.fromtimestamp(payload["exp"], UTC)
            if same_grant and exp - datetime.now(UTC) > _MINT_STABLE_FLOOR:
                return existing
        except (jwt.InvalidTokenError, KeyError, ValueError):
            pass  # fall through to a fresh mint
    return mint(user_id, session_id)
