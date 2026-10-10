"""Tests for chat WebSocket route."""

from decimal import Decimal
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

    session, cluster, _role, error = await validate_session(mock_db, "nonexistent")
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

    session, cluster, _role, error = await validate_session(mock_db, "session-id")
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
    # No accepted share for this user → get_session_role returns None → access denied.
    mock_db.scalar = AsyncMock(return_value=None)

    session, cluster, _role, error = await validate_session(mock_db, "session-id", user)
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

    session, cluster, _role, error = await validate_session(mock_db, "session-id")
    assert session is mock_session
    assert cluster is mock_cluster
    assert error is None


@pytest.mark.asyncio
async def test_chat_validate_session_without_cluster() -> None:
    """A session with no cluster validates with cluster=None, no error."""
    from app.api.routes.chat import validate_session

    mock_session = MagicMock()
    mock_session.cluster_id = None
    mock_session.user_id = None

    mock_db = AsyncMock()
    mock_db.get = AsyncMock(side_effect=lambda model, id: {"session-id": mock_session}.get(id))

    session, cluster, _role, error = await validate_session(mock_db, "session-id")
    assert session is mock_session
    assert cluster is None
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


# ============================================================================
# Budget enforcement gate (_budget_block_reason)
# ============================================================================


def _budget_status(*, is_allowed: bool, action: str | None):
    from app.services.budget_service import BudgetStatus

    return BudgetStatus(
        is_allowed=is_allowed,
        remaining_tokens=None,
        remaining_cost_usd=Decimal("0"),
        usage_percent=100.0 if not is_allowed else 0.0,
        warning_message=None,
        budget_name="b",
        hard_limit_action=action,
    )


def _session_cm(db: object) -> MagicMock:
    """Mock for `async with async_session_maker() as db`."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm)


@pytest.mark.asyncio
async def test_budget_gate_disabled_skips_db() -> None:
    """When enforcement is off, the gate allows the turn without touching the DB."""
    from uuid import uuid4

    from app.api.routes import chat

    with (
        patch.object(chat, "is_budget_enabled", return_value=False),
        patch.object(chat, "async_session_maker") as maker,
    ):
        result = await chat._budget_block_reason(uuid4(), ["user"])

    assert result is None
    maker.assert_not_called()


@pytest.mark.asyncio
async def test_budget_gate_blocks_when_over_block_limit() -> None:
    """Over a block-action limit → returns a user-facing message."""
    from uuid import uuid4

    from app.api.routes import chat

    with (
        patch.object(chat, "is_budget_enabled", return_value=True),
        patch.object(chat, "async_session_maker", _session_cm(AsyncMock())),
        patch.object(
            chat,
            "check_budget",
            AsyncMock(return_value=_budget_status(is_allowed=False, action="block")),
        ),
    ):
        result = await chat._budget_block_reason(uuid4(), ["user"])

    assert result is not None
    assert "budget" in result.lower()


@pytest.mark.asyncio
async def test_budget_gate_warn_action_does_not_block() -> None:
    """Over limit but action=warn → does not block (warn budgets only report)."""
    from uuid import uuid4

    from app.api.routes import chat

    with (
        patch.object(chat, "is_budget_enabled", return_value=True),
        patch.object(chat, "async_session_maker", _session_cm(AsyncMock())),
        patch.object(
            chat,
            "check_budget",
            AsyncMock(return_value=_budget_status(is_allowed=False, action="warn")),
        ),
    ):
        result = await chat._budget_block_reason(uuid4(), ["user"])

    assert result is None


@pytest.mark.asyncio
async def test_budget_gate_allows_when_under_limit() -> None:
    """Under limit → allowed."""
    from uuid import uuid4

    from app.api.routes import chat

    with (
        patch.object(chat, "is_budget_enabled", return_value=True),
        patch.object(chat, "async_session_maker", _session_cm(AsyncMock())),
        patch.object(
            chat,
            "check_budget",
            AsyncMock(return_value=_budget_status(is_allowed=True, action="block")),
        ),
    ):
        result = await chat._budget_block_reason(uuid4(), ["user"])

    assert result is None


def test_turn_rate_gate_blocks_after_burst() -> None:
    """The per-user WS turn limiter allows a burst then blocks; disabled → never blocks."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.api.routes import chat

    uid = uuid4()
    # Enabled, tiny cap: capacity == rate == 2 → 2 allowed, 3rd blocked (same instant).
    chat._turn_limiter = None
    with patch.object(
        chat,
        "get_settings",
        return_value=SimpleNamespace(
            rate_limit_enabled=True, rate_limit_turn_per_user_per_minute=2
        ),
    ):
        assert chat._turn_rate_block_reason(uid) is None
        assert chat._turn_rate_block_reason(uid) is None
        assert chat._turn_rate_block_reason(uid) is not None
        # A different user has an independent bucket.
        assert chat._turn_rate_block_reason(uuid4()) is None

    # Disabled → always allowed regardless of rate.
    chat._turn_limiter = None
    with patch.object(
        chat,
        "get_settings",
        return_value=SimpleNamespace(
            rate_limit_enabled=False, rate_limit_turn_per_user_per_minute=1
        ),
    ):
        assert all(chat._turn_rate_block_reason(uid) is None for _ in range(10))
    chat._turn_limiter = None  # reset shared state for other tests


@pytest.mark.asyncio
async def test_budget_gate_fails_closed_on_error() -> None:
    """With enforcement ON, a budget-check error must BLOCK the turn (fail-closed) rather
    than silently allow unbounded LLM spend on a public endpoint."""
    from uuid import uuid4

    from app.api.routes import chat

    with (
        patch.object(chat, "is_budget_enabled", return_value=True),
        patch.object(chat, "async_session_maker", _session_cm(AsyncMock())),
        patch.object(chat, "check_budget", AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        result = await chat._budget_block_reason(uuid4(), ["user"])

    assert result is not None
    assert "budget" in result.lower()
