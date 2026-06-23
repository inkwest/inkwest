"""
Internal JWT for the OAuth2 MCP reverse proxy (Increment 2).

The sandbox ``.claude.json`` carries only this short-lived internal token in the
``Authorization`` header — never the real upstream OAuth token, which stays server-side in
the vault. The proxy (:mod:`app.api.routes.mcp_proxy`) verifies it to identify
``(user_id, session_id, mcp_key)`` and then injects the real bearer per request.

HS256 on the shared ``Settings.secret_key`` (same key as the user-session and WS-ticket
tokens), so the verifier MUST enforce both ``aud`` and ``type`` — otherwise the token
classes are interchangeable on the shared key. TTL is 6 h: it only *identifies* (the real
credential stays server-side and is revocable), but it lives in a durable 0666 sandbox file,
so a 24 h bearer there would be disproportionate.
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

_AUD = "mcp-proxy"
_TYPE = "mcp-proxy"
_TTL = timedelta(hours=6)
# Re-mint only when the existing token has less than this left — keeps the injected
# ``.claude.json`` stable across turns so the merge stays a no-op (the CLI restarts per
# message and re-applies the selection; a fresh JWT every turn would rewrite the file).
_MINT_STABLE_FLOOR = timedelta(hours=1)


class ProxyAuthError(Exception):
    """The internal proxy JWT is missing, malformed, or not valid for this key."""


@dataclass(frozen=True)
class ProxyClaims:
    """Identity carried by the internal proxy JWT. ``session_id`` is correlation only."""

    user_id: UUID
    session_id: UUID
    mcp_key: str


def mint(user_id: UUID, session_id: UUID, mcp_key: str) -> str:
    """Mint a fresh internal proxy JWT for ``(user, session, key)``."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "key": mcp_key,
        "aud": _AUD,
        "type": _TYPE,
        "iat": now,
        "exp": now + _TTL,
    }
    return jwt.encode(payload, get_settings().secret_key, algorithm=JWT_ALGORITHM)


def verify(token: str, *, mcp_key: str) -> ProxyClaims:
    """Verify a proxy JWT for ``mcp_key``. Raises :class:`ProxyAuthError` on any failure.

    Enforces signature, expiry, ``aud``, ``type`` and that the token's ``key`` matches the
    path's ``mcp_key`` (a token minted for server A must not be replayed against server B).
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[JWT_ALGORITHM],
            audience=_AUD,
        )
    except jwt.InvalidTokenError as e:
        raise ProxyAuthError(f"invalid proxy token: {type(e).__name__}") from e

    if payload.get("type") != _TYPE:
        raise ProxyAuthError("wrong token type")
    if payload.get("key") != mcp_key:
        raise ProxyAuthError("token key does not match the requested MCP server")
    try:
        return ProxyClaims(
            user_id=UUID(payload["sub"]),
            session_id=UUID(payload["sid"]),
            mcp_key=str(payload["key"]),
        )
    except (KeyError, ValueError) as e:
        raise ProxyAuthError("malformed proxy token claims") from e


def mint_stable(existing: str | None, user_id: UUID, session_id: UUID, mcp_key: str) -> str:
    """Return ``existing`` if it is still valid for this grant with > 1 h left, else mint anew.

    Keeps the per-conversation ``.claude.json`` write idempotent across turns.
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
                and payload.get("sub") == str(user_id)
                and payload.get("sid") == str(session_id)
                and payload.get("key") == mcp_key
            )
            exp = datetime.fromtimestamp(payload["exp"], UTC)
            if same_grant and exp - datetime.now(UTC) > _MINT_STABLE_FLOOR:
                return existing
        except (jwt.InvalidTokenError, KeyError, ValueError):
            pass  # fall through to a fresh mint
    return mint(user_id, session_id, mcp_key)
