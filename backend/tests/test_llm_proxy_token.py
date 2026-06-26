"""Tests for the LLM auth broker JWT (app.services.llm_proxy_token)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.auth import JWT_ALGORITHM
from app.core.config import get_settings
from app.services import llm_proxy_token as t
from app.services.llm_proxy_token import ProxyAuthError

_U = uuid.UUID("00000000-0000-4000-8000-000000000001")
_S = uuid.UUID("00000000-0000-4000-8000-000000000002")


def test_mint_verify_round_trip() -> None:
    claims = t.verify(t.mint(_U, _S))
    assert claims.user_id == str(_U)
    assert claims.session_id == str(_S)


def test_verify_tolerates_missing_identity() -> None:
    # sub/sid are audit-only; a token minted without them (e.g. dev with no user) is still valid.
    claims = t.verify(t.mint(None, None))
    assert claims.user_id is None
    assert claims.session_id is None


def test_verify_does_not_reject_non_uuid_identity() -> None:
    # Identity carries no authorization weight — a non-UUID sub/sid must NOT fail a model call
    # (the contrast with the MCP proxy, which parses strict UUIDs).
    claims = t.verify(t.mint("not-a-uuid", "also-not"))
    assert claims.user_id == "not-a-uuid"
    assert claims.session_id == "also-not"


def test_verify_rejects_wrong_type_on_shared_key() -> None:
    # A token of another class signed with the same secret must not pass as a broker token.
    secret = get_settings().secret_key
    now = datetime.now(UTC)
    other = jwt.encode(
        {
            "sub": str(_U),
            "aud": "bedrock-proxy",
            "type": "session",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(ProxyAuthError):
        t.verify(other)


def test_verify_rejects_wrong_aud() -> None:
    # An mcp-proxy token (different aud) signed with the same secret must not pass here.
    secret = get_settings().secret_key
    now = datetime.now(UTC)
    bad_aud = jwt.encode(
        {
            "sub": str(_U),
            "aud": "mcp-proxy",
            "type": "bedrock-proxy",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(ProxyAuthError):
        t.verify(bad_aud)


def test_verify_rejects_expired() -> None:
    secret = get_settings().secret_key
    past = datetime.now(UTC) - timedelta(hours=1)
    expired = jwt.encode(
        {
            "sub": str(_U),
            "aud": "bedrock-proxy",
            "type": "bedrock-proxy",
            "iat": past - timedelta(hours=6),
            "exp": past,
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(ProxyAuthError):
        t.verify(expired)


def test_verify_rejects_garbage() -> None:
    with pytest.raises(ProxyAuthError):
        t.verify("not-a-jwt")


def test_mint_stable_reuses_valid_token() -> None:
    tok = t.mint(_U, _S)
    assert t.mint_stable(tok, _U, _S) == tok  # > 1h left → unchanged


def test_mint_stable_remints_for_different_grant() -> None:
    tok = t.mint(_U, _S)
    other_session = uuid.UUID("00000000-0000-4000-8000-000000000009")
    assert t.mint_stable(tok, _U, other_session) != tok


def test_mint_stable_remints_when_absent_or_garbage() -> None:
    assert t.mint_stable(None, _U, _S)
    assert t.mint_stable("garbage", _U, _S)


def test_mint_stable_remints_when_near_expiry() -> None:
    secret = get_settings().secret_key
    now = datetime.now(UTC)
    near = jwt.encode(
        {
            "sub": str(_U),
            "sid": str(_S),
            "aud": "bedrock-proxy",
            "type": "bedrock-proxy",
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },  # < 1h floor
        secret,
        algorithm=JWT_ALGORITHM,
    )
    assert t.mint_stable(near, _U, _S) != near
