"""Tests for chat WebSocket route."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def ws_client() -> AsyncClient:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


@pytest.mark.asyncio
async def test_chat_ws_auth_returns_none_in_oidc_mode() -> None:
    """validate_websocket_auth returns None when OIDC is enabled and no token."""
    from app.api.routes.chat import validate_websocket_auth

    ws = MagicMock()
    ws.headers = {}
    mock_db = AsyncMock()

    with patch("app.api.routes.chat.get_settings") as mock_settings:
        settings = MagicMock()
        settings.oidc_enabled = True
        mock_settings.return_value = settings

        result = await validate_websocket_auth(ws, mock_db)

    assert result is None


@pytest.mark.asyncio
async def test_chat_validate_session_not_found() -> None:
    """validate_session returns error when session doesn't exist."""
    from app.api.routes.chat import validate_session

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=None)

    session, cluster, error = await validate_session(mock_db, "nonexistent")
    assert session is None
    assert error == "Session not found"


@pytest.mark.asyncio
async def test_chat_validate_session_inactive_cluster() -> None:
    """validate_session returns error when cluster is inactive."""
    from app.api.routes.chat import validate_session

    mock_session = MagicMock()
    mock_session.cluster_id = "cluster-id"
    mock_session.user_id = None

    mock_cluster = MagicMock()
    mock_cluster.is_active = False

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(
        side_effect=lambda model, id: {
            "session-id": mock_session,
            "cluster-id": mock_cluster,
        }.get(id)
    )

    session, cluster, error = await validate_session(mock_db, "session-id")
    assert error == "Cluster is not active"


@pytest.mark.asyncio
async def test_chat_validate_session_access_denied() -> None:
    """validate_session returns error when user doesn't own session."""
    from app.api.routes.chat import validate_session

    mock_session = MagicMock()
    mock_session.cluster_id = "cluster-id"
    mock_session.user_id = "other-user"

    user = MagicMock()
    user.id = "my-user"
    user.is_admin = lambda: False

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(
        side_effect=lambda model, id: {
            "session-id": mock_session,
        }.get(id)
    )

    session, cluster, error = await validate_session(mock_db, "session-id", user)
    assert error == "Access denied"


@pytest.mark.asyncio
async def test_chat_validate_session_success() -> None:
    """validate_session returns session and cluster on success."""
    from app.api.routes.chat import validate_session

    mock_session = MagicMock()
    mock_session.cluster_id = "cluster-id"
    mock_session.user_id = None

    mock_cluster = MagicMock()
    mock_cluster.is_active = True

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(
        side_effect=lambda model, id: {
            "session-id": mock_session,
            "cluster-id": mock_cluster,
        }.get(id)
    )

    session, cluster, error = await validate_session(mock_db, "session-id")
    assert session is mock_session
    assert cluster is mock_cluster
    assert error is None


@pytest.mark.asyncio
async def test_chat_connection_tracking() -> None:
    """Test active_connections tracking."""
    from app.api.routes.chat import (
        active_connections,
        disconnect,
        get_active_connection_count,
    )

    ws = MagicMock()
    sid = "track-test"
    active_connections[sid] = {ws}

    assert await get_active_connection_count(sid) == 1

    await disconnect(sid, ws)
    assert await get_active_connection_count(sid) == 0


@pytest.mark.asyncio
async def test_send_error() -> None:
    """send_error sends JSON and optionally closes."""
    from app.api.routes.chat import send_error

    ws = AsyncMock()
    await send_error(ws, "something broke", close_code=4004)
    ws.send_json.assert_awaited_once_with({"type": "error", "content": "something broke"})
    ws.close.assert_awaited_once_with(code=4004)
