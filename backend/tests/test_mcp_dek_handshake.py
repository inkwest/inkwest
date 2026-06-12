"""Tests for the WS DEK handshake (app.services.mcp_dek_handshake).

The DB-backed branches (``mcp_dek_open`` with a key row, ``mcp_dek_wrapped`` persistence)
are exercised at the e2e tier; here we cover the in-RAM store interactions and the
connection-refcount eviction that enforces connected-only key custody.
"""

import base64
import uuid
from unittest.mock import patch

import pytest

from app.core.crypto import DEK_BYTES, generate_dek
from app.services import mcp_dek_handshake as hs
from app.services.mcp_dek_store import get_dek_store

_USER = uuid.UUID("00000000-0000-4000-8000-0000000000aa")


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
