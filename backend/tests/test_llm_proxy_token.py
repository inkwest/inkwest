"""Tests for the ephemeral per-turn broker grant store (app.services.llm_proxy_token)."""

import time
import uuid

import pytest

from app.services import llm_proxy_token as t
from app.services.llm_proxy_token import ProxyAuthError

_U = uuid.UUID("00000000-0000-4000-8000-000000000001")
_S = uuid.UUID("00000000-0000-4000-8000-000000000002")


def test_mint_verify_round_trip() -> None:
    tok = t.mint(_U, _S)
    claims = t.verify(tok)
    assert claims.user_id == str(_U)
    assert claims.session_id == str(_S)


def test_mint_is_unique_per_turn() -> None:
    assert t.mint(_U, _S) != t.mint(_U, _S)  # opaque, fresh each turn — never reused


def test_mint_tolerates_missing_identity() -> None:
    claims = t.verify(t.mint(None, None))
    assert claims.user_id is None
    assert claims.session_id is None


def test_burn_invalidates_grant() -> None:
    tok = t.mint(_U, _S)
    t.verify(tok)  # live during the turn
    t.burn(tok)
    with pytest.raises(ProxyAuthError):
        t.verify(tok)  # dead the moment the turn ends — no replay


def test_burn_is_idempotent_and_none_safe() -> None:
    tok = t.mint(_U, _S)
    t.burn(tok)
    t.burn(tok)  # second burn is a no-op, not an error
    t.burn(None)  # callable from a finally with no token


def test_verify_rejects_unknown_token() -> None:
    with pytest.raises(ProxyAuthError):
        t.verify("never-minted")


def test_verify_rejects_and_evicts_expired() -> None:
    tok = t.mint(_U, _S)
    # Force the backstop deadline into the past (simulates a grant orphaned past its TTL).
    grant = t._grants[tok]
    t._grants[tok] = type(grant)(
        user_id=grant.user_id, session_id=grant.session_id, expires_at=time.monotonic() - 1
    )
    with pytest.raises(ProxyAuthError):
        t.verify(tok)
    assert tok not in t._grants  # lazily evicted on the failed lookup


def test_mint_sweeps_expired_grants() -> None:
    stale = t.mint(_U, _S)
    g = t._grants[stale]
    t._grants[stale] = type(g)(
        user_id=g.user_id, session_id=g.session_id, expires_at=time.monotonic() - 1
    )
    t.mint(_U, _S)  # mint sweeps expired entries so crash-orphans don't accumulate
    assert stale not in t._grants
