"""Tests for the WS DEK handshake (app.services.mcp_dek_handshake).

Covers the in-RAM store interactions, the connection-refcount eviction that enforces
connected-only key custody, and the DB-backed branches (``mcp_dek_open`` with a key row →
challenge, and ``mcp_dek_wrapped`` insert/overwrite) with a mocked session — the
Postgres-specific behaviour is left to the optional e2e tier.
"""

import base64
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.crypto import DEK_BYTES, generate_dek
from app.models.mcp_user_key import McpUserKey
from app.services import mcp_dek_handshake as hs
from app.services.mcp_dek_store import get_dek_store

_USER = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


def _fake_session_maker(db):
    """Patch target for ``async_session_maker``: an ``async with`` yielding ``db``."""

    @asynccontextmanager
    async def _maker():
        yield db

    return _maker


@pytest.fixture(autouse=True)
async def _clean_state():
    hs._conn_counts.clear()
    await get_dek_store().delete(_USER)
    yield
    hs._conn_counts.clear()
    await get_dek_store().delete(_USER)


@pytest.mark.asyncio
async def test_supply_puts_dek_in_ram() -> None:
    dek = generate_dek()
    reply = await hs.handle_dek_message(
        _USER, {"type": "mcp_dek_supply", "dek": base64.b64encode(dek).decode()}
    )
    assert reply == {"type": "mcp_dek_ready"}
    assert await get_dek_store().get(_USER) == dek


@pytest.mark.asyncio
async def test_supply_rejects_wrong_length() -> None:
    reply = await hs.handle_dek_message(
        _USER, {"type": "mcp_dek_supply", "dek": base64.b64encode(b"short").decode()}
    )
    assert reply == {"type": "mcp_dek_error", "reason": "bad_dek_length"}
    assert await get_dek_store().get(_USER) is None


@pytest.mark.asyncio
async def test_reset_bootstraps_a_fresh_dek_in_ram() -> None:
    reply = await hs.handle_dek_message(_USER, {"type": "mcp_dek_reset"})
    assert reply is not None and reply["type"] == "mcp_dek_bootstrap"
    dek = base64.b64decode(reply["dek"])
    assert len(dek) == DEK_BYTES
    # The freshly generated DEK is already held server-side for the connected window.
    assert await get_dek_store().get(_USER) == dek


@pytest.mark.asyncio
async def test_refcount_evicts_dek_only_on_last_disconnect() -> None:
    await get_dek_store().put(_USER, generate_dek())
    await hs.note_connect(_USER)
    await hs.note_connect(_USER)  # two tabs of the same profile

    await hs.note_disconnect(_USER)  # one tab closes
    assert await get_dek_store().get(_USER) is not None  # still connected elsewhere

    await hs.note_disconnect(_USER)  # last tab closes
    assert await get_dek_store().get(_USER) is None  # evicted → no plaintext at rest


@pytest.mark.asyncio
async def test_note_disconnect_underflow_is_safe() -> None:
    """A disconnect with no prior connect must evict (not go negative and wedge eviction)."""
    await get_dek_store().put(_USER, generate_dek())
    await hs.note_disconnect(_USER)  # no matching note_connect
    assert await get_dek_store().get(_USER) is None
    assert str(_USER) not in hs._conn_counts


@pytest.mark.asyncio
async def test_bad_base64_supply_returns_error_not_raises() -> None:
    reply = await hs.handle_dek_message(_USER, {"type": "mcp_dek_supply", "dek": "@@@not-b64@@@"})
    assert reply == {"type": "mcp_dek_error", "reason": "bad_base64"}


@pytest.mark.asyncio
async def test_open_is_noop_when_oauth_unconfigured() -> None:
    """No catalog → mcp_dek_open does zero work (no DB read, no bootstrap, no key row)."""
    with patch.object(hs, "oauth_catalog_keys", return_value=[]):
        reply = await hs.handle_dek_message(_USER, {"type": "mcp_dek_open"})
    assert reply == {"type": "mcp_dek_absent"}
    assert await get_dek_store().get(_USER) is None


@pytest.mark.asyncio
async def test_unknown_message_ignored() -> None:
    assert await hs.handle_dek_message(_USER, {"type": "mcp_dek_bogus"}) is None


# ---- DB-backed branches (mocked session) ------------------------------------ #
@pytest.mark.asyncio
async def test_open_with_key_row_returns_challenge() -> None:
    """An existing key row → the wrapped DEK is handed back (base64) for the browser to unwrap,
    rather than bootstrapping a fresh one (which would orphan any sealed grant)."""
    row = McpUserKey(
        user_id=_USER, wrapped_dek=b"WRAPPED-BYTES", wrap_alg="AES-GCM", wrap_iv=b"IV12"
    )
    db = MagicMock()
    db.get = AsyncMock(return_value=row)
    with (
        patch.object(hs, "oauth_catalog_keys", return_value=["notion"]),
        patch.object(hs, "get_settings", return_value=MagicMock(sandbox_enabled=True)),
        patch.object(hs, "async_session_maker", _fake_session_maker(db)),
    ):
        reply = await hs.handle_dek_message(_USER, {"type": "mcp_dek_open"})
    assert reply == {
        "type": "mcp_dek_challenge",
        "wrapped_dek": base64.b64encode(b"WRAPPED-BYTES").decode(),
        "wrap_iv": base64.b64encode(b"IV12").decode(),
        "wrap_alg": "AES-GCM",
    }
    # No RAM DEK installed by a challenge — the browser must unwrap and supply it next.
    assert await get_dek_store().get(_USER) is None


@pytest.mark.asyncio
async def test_wrapped_inserts_when_no_row() -> None:
    """First-time persistence: a new McpUserKey row is added with the wrapped DEK."""
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.add = MagicMock()
    db.commit = AsyncMock()
    with patch.object(hs, "async_session_maker", _fake_session_maker(db)):
        reply = await hs.handle_dek_message(
            _USER,
            {
                "type": "mcp_dek_wrapped",
                "wrapped_dek": base64.b64encode(b"NEWWRAP").decode(),
                "wrap_iv": base64.b64encode(b"NEWIV").decode(),
                "wrap_alg": "AES-GCM",
            },
        )
    assert reply == {"type": "mcp_dek_ready"}
    added = db.add.call_args.args[0]
    assert isinstance(added, McpUserKey)
    assert added.wrapped_dek == b"NEWWRAP" and added.wrap_iv == b"NEWIV"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrapped_overwrites_existing_row() -> None:
    """Re-key: an existing row is overwritten in place (not duplicated)."""
    row = McpUserKey(user_id=_USER, wrapped_dek=b"OLD", wrap_alg="AES-GCM", wrap_iv=b"OLDIV")
    db = MagicMock()
    db.get = AsyncMock(return_value=row)
    db.add = MagicMock()
    db.commit = AsyncMock()
    with patch.object(hs, "async_session_maker", _fake_session_maker(db)):
        reply = await hs.handle_dek_message(
            _USER,
            {"type": "mcp_dek_wrapped", "wrapped_dek": base64.b64encode(b"FRESH").decode()},
        )
    assert reply == {"type": "mcp_dek_ready"}
    db.add.assert_not_called()  # updated in place
    assert row.wrapped_dek == b"FRESH" and row.wrap_iv is None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_wrapped_rejects_empty() -> None:
    reply = await hs.handle_dek_message(_USER, {"type": "mcp_dek_wrapped", "wrapped_dek": ""})
    assert reply == {"type": "mcp_dek_error", "reason": "empty_wrapped_dek"}
