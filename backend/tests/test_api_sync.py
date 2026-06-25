"""Tests for sync WebSocket route."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.routes.sync import _authenticate_sync_ws


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
