"""Tests for sync WebSocket route."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.api.routes.sync import _authenticate_sync_ws, _handle_inbound


@pytest.mark.asyncio
async def test_sync_auth_dev_mode_scopes_to_dev_user() -> None:
    """In dev mode (OIDC disabled), the sync WS is accepted and scoped to the dev
    user's concrete id — NOT None — so a share_invite addressed to another user
    (e.g. a seeded account) is not delivered to the dev user's socket."""
    ws = MagicMock()
    mock_db = AsyncMock()
    dev = MagicMock()
    dev.id = "dev-user-id"

    with (
        patch("app.api.routes.sync.get_settings") as mock_settings,
        patch("app.api.dependencies.get_or_create_dev_user", AsyncMock(return_value=dev)),
    ):
        settings = MagicMock()
        settings.oidc_enabled = False
        mock_settings.return_value = settings

        result = await _authenticate_sync_ws(ws, mock_db)

    assert result == (True, "dev-user-id")


@pytest.mark.asyncio
async def test_sync_auth_oidc_no_token_rejects() -> None:
    """In OIDC mode without ticket, sync WS is rejected."""
    ws = MagicMock()
    ws.headers = {}
    mock_db = AsyncMock()

    with patch("app.api.routes.sync.get_settings") as mock_settings:
        settings = MagicMock()
        settings.oidc_enabled = True
        mock_settings.return_value = settings

        result = await _authenticate_sync_ws(ws, mock_db)

    assert result == (False, None)


@pytest.mark.asyncio
async def test_sync_auth_oidc_invalid_ticket_rejects() -> None:
    """In OIDC mode with invalid ticket, sync WS is rejected."""
    ws = MagicMock()
    ws.headers = {"sec-websocket-protocol": "inkwest, ticket|bad-ticket"}
    mock_db = AsyncMock()

    with (
        patch("app.api.routes.sync.get_settings") as mock_settings,
        patch("app.api.routes.sync.verify_ws_ticket", return_value=None),
    ):
        settings = MagicMock()
        settings.oidc_enabled = True
        mock_settings.return_value = settings

        result = await _authenticate_sync_ws(ws, mock_db)

    assert result == (False, None)


@pytest.mark.asyncio
async def test_sync_auth_oidc_valid_ticket_accepts() -> None:
    """In OIDC mode with valid ticket and active user, sync WS is accepted."""
    ws = MagicMock()
    ws.headers = {"sec-websocket-protocol": "inkwest, ticket|valid-ticket"}

    mock_user = MagicMock()
    mock_user.is_active = True
    mock_user.id = "user-id"
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with (
        patch("app.api.routes.sync.get_settings") as mock_settings,
        patch(
            "app.api.routes.sync.verify_ws_ticket",
            return_value={"sub": "user-id", "type": "ws"},
        ),
    ):
        settings = MagicMock()
        settings.oidc_enabled = True
        mock_settings.return_value = settings

        result = await _authenticate_sync_ws(ws, mock_db)

    # Accepted and scoped to the authenticated user's id (events isolated per user).
    assert result == (True, "user-id")


@pytest.mark.asyncio
async def test_sync_auth_oidc_inactive_user_rejects() -> None:
    """In OIDC mode with valid ticket but inactive user, sync WS is rejected."""
    ws = MagicMock()
    ws.headers = {"sec-websocket-protocol": "inkwest, ticket|valid-ticket"}

    mock_user = MagicMock()
    mock_user.is_active = False
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_user
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with (
        patch("app.api.routes.sync.get_settings") as mock_settings,
        patch(
            "app.api.routes.sync.verify_ws_ticket",
            return_value={"sub": "user-id", "type": "ws"},
        ),
    ):
        settings = MagicMock()
        settings.oidc_enabled = True
        mock_settings.return_value = settings

        result = await _authenticate_sync_ws(ws, mock_db)

    assert result == (False, None)


@pytest.mark.asyncio
async def test_sync_auth_oidc_unknown_user_rejects() -> None:
    """In OIDC mode with valid ticket but user not in DB, sync WS is rejected."""
    ws = MagicMock()
    ws.headers = {"sec-websocket-protocol": "inkwest, ticket|valid-ticket"}

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with (
        patch("app.api.routes.sync.get_settings") as mock_settings,
        patch(
            "app.api.routes.sync.verify_ws_ticket",
            return_value={"sub": "user-id", "type": "ws"},
        ),
    ):
        settings = MagicMock()
        settings.oidc_enabled = True
        mock_settings.return_value = settings

        result = await _authenticate_sync_ws(ws, mock_db)

    assert result == (False, None)


@pytest.mark.asyncio
async def test_sync_auth_exception_returns_false() -> None:
    """Any exception during auth fails closed (returns False)."""
    ws = MagicMock()
    mock_db = AsyncMock()

    with patch("app.api.routes.sync.get_settings", side_effect=RuntimeError("config broken")):
        result = await _authenticate_sync_ws(ws, mock_db)

    assert result == (False, None)


@pytest.mark.asyncio
async def test_handle_inbound_ping_replies_pong() -> None:
    """A ping frame is answered with a pong; the DEK handler is not touched."""
    ws = MagicMock()
    ws.send_json = AsyncMock()

    with patch("app.api.routes.sync.handle_dek_message", AsyncMock()) as mock_dek:
        await _handle_inbound(ws, uuid4(), {"type": "ping"})

    ws.send_json.assert_awaited_once_with({"type": "pong"})
    mock_dek.assert_not_called()


@pytest.mark.asyncio
async def test_handle_inbound_dek_frame_routes_and_forwards_reply() -> None:
    """An mcp_dek_* frame is dispatched to the DEK handler and its reply is sent back.

    This is the fix: the DEK handshake now rides the app-wide sync socket, so an OAuth2 MCP
    connect from the settings panel (no chat socket open) still seats the user's key.
    """
    ws = MagicMock()
    ws.send_json = AsyncMock()
    uid = uuid4()
    frame = {"type": "mcp_dek_open"}

    with patch(
        "app.api.routes.sync.handle_dek_message",
        AsyncMock(return_value={"type": "mcp_dek_bootstrap", "dek": "abc"}),
    ) as mock_dek:
        await _handle_inbound(ws, uid, frame)

    mock_dek.assert_awaited_once_with(uid, frame)
    ws.send_json.assert_awaited_once_with({"type": "mcp_dek_bootstrap", "dek": "abc"})


@pytest.mark.asyncio
async def test_handle_inbound_dek_no_reply_sends_nothing() -> None:
    """When the DEK handler returns None (nothing to say), no frame is sent."""
    ws = MagicMock()
    ws.send_json = AsyncMock()

    with patch("app.api.routes.sync.handle_dek_message", AsyncMock(return_value=None)):
        await _handle_inbound(ws, uuid4(), {"type": "mcp_dek_supply", "dek": "x"})

    ws.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_handle_inbound_unknown_type_ignored() -> None:
    """An unrecognized frame type is silently ignored (no reply, no DEK call)."""
    ws = MagicMock()
    ws.send_json = AsyncMock()

    with patch("app.api.routes.sync.handle_dek_message", AsyncMock()) as mock_dek:
        await _handle_inbound(ws, uuid4(), {"type": "something_else"})

    ws.send_json.assert_not_called()
    mock_dek.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [["not", "an", "object"], "scalar", 42, None])
async def test_handle_inbound_non_dict_frame_ignored(frame: object) -> None:
    """A non-object JSON frame is ignored, not crashed on (would tear the socket down)."""
    ws = MagicMock()
    ws.send_json = AsyncMock()

    with patch("app.api.routes.sync.handle_dek_message", AsyncMock()) as mock_dek:
        await _handle_inbound(ws, uuid4(), frame)

    ws.send_json.assert_not_called()
    mock_dek.assert_not_called()
