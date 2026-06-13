"""Tests for the internal MCP proxy JWT (app.services.mcp_proxy_token)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.auth import JWT_ALGORITHM
from app.core.config import get_settings
from app.services import mcp_proxy_token as t
from app.services.mcp_proxy_token import ProxyAuthError

_U = uuid.UUID("00000000-0000-4000-8000-000000000001")
_S = uuid.UUID("00000000-0000-4000-8000-000000000002")


def test_mint_verify_round_trip() -> None:
    claims = t.verify(t.mint(_U, _S, "notion"), mcp_key="notion")
    assert claims.user_id == _U
    assert claims.session_id == _S
    assert claims.mcp_key == "notion"


def test_verify_rejects_key_mismatch() -> None:
    tok = t.mint(_U, _S, "notion")
    with pytest.raises(ProxyAuthError):
        t.verify(tok, mcp_key="slack")  # token minted for a different server


def test_verify_rejects_wrong_type_on_shared_key() -> None:
    # A token of another class signed with the same secret must not pass as a proxy token.
    secret = get_settings().secret_key
    now = datetime.now(UTC)
    other = jwt.encode(
        {
            "sub": str(_U),
            "sid": str(_S),
            "key": "notion",
            "aud": "mcp-proxy",
            "type": "session",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(ProxyAuthError):
        t.verify(other, mcp_key="notion")


def test_verify_rejects_wrong_aud() -> None:
    secret = get_settings().secret_key
    now = datetime.now(UTC)
    bad_aud = jwt.encode(
        {
            "sub": str(_U),
            "sid": str(_S),
            "key": "notion",
            "aud": "session",
            "type": "mcp-proxy",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(ProxyAuthError):
        t.verify(bad_aud, mcp_key="notion")


def test_verify_rejects_expired() -> None:
    secret = get_settings().secret_key
    past = datetime.now(UTC) - timedelta(hours=1)
    expired = jwt.encode(
        {
            "sub": str(_U),
            "sid": str(_S),
            "key": "notion",
            "aud": "mcp-proxy",
            "type": "mcp-proxy",
            "iat": past - timedelta(hours=6),
            "exp": past,
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(ProxyAuthError):
        t.verify(expired, mcp_key="notion")


def test_verify_rejects_garbage() -> None:
    with pytest.raises(ProxyAuthError):
        t.verify("not-a-jwt", mcp_key="notion")


def test_mint_stable_reuses_valid_token() -> None:
    tok = t.mint(_U, _S, "notion")
    assert t.mint_stable(tok, _U, _S, "notion") == tok  # > 1h left → unchanged


def test_mint_stable_remints_for_different_grant() -> None:
    tok = t.mint(_U, _S, "notion")
    # Same token, different session → must NOT be reused.
    other_session = uuid.UUID("00000000-0000-4000-8000-000000000009")
    assert t.mint_stable(tok, _U, other_session, "notion") != tok


def test_mint_stable_remints_when_absent_or_garbage() -> None:
    assert t.mint_stable(None, _U, _S, "notion")
    assert t.mint_stable("garbage", _U, _S, "notion")


def test_mint_stable_remints_when_near_expiry() -> None:
    secret = get_settings().secret_key
    now = datetime.now(UTC)
    near = jwt.encode(
        {
            "sub": str(_U),
            "sid": str(_S),
            "key": "notion",
            "aud": "mcp-proxy",
            "type": "mcp-proxy",
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },  # < 1h floor
        secret,
        algorithm=JWT_ALGORITHM,
    )
    assert t.mint_stable(near, _U, _S, "notion") != near
