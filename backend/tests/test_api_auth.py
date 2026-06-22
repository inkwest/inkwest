"""Tests for auth API routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.auth import (
    JWT_ALGORITHM,
    WS_TICKET_EXPIRATION_SECONDS,
    create_ws_ticket,
    extract_ws_ticket,
    verify_ws_ticket,
)
from app.main import app
from app.services.message_window import truncate_messages


@pytest.fixture
async def client(mock_db: AsyncMock) -> AsyncClient:
    from app.api.dependencies import get_db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.mark.asyncio
async def test_auth_status_dev_mode(client: AsyncClient, mock_db: AsyncMock) -> None:
    """In dev mode (OIDC disabled), returns authenticated dev user."""
    mock_oidc = MagicMock()
    mock_oidc.is_enabled = False

    dev_user = MagicMock()
    dev_user.id = "00000000-0000-4000-8000-000000000099"
    dev_user.email = "dev@localhost"
    dev_user.display_name = "Developer"
    dev_user.roles = ["admin"]
    dev_user.is_admin = lambda: True

    with (
        patch("app.api.routes.auth.get_oidc_client", return_value=mock_oidc),
        patch("app.api.dependencies.get_or_create_dev_user", return_value=dev_user),
    ):
        mock_db.commit = AsyncMock()
        resp = await client.get("/api/auth/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["oidc_enabled"] is False
    assert data["authenticated"] is True
    assert data["user"]["email"] == "dev@localhost"


@pytest.mark.asyncio
async def test_auth_status_oidc_no_token(client: AsyncClient) -> None:
    """In OIDC mode without token, returns unauthenticated."""
    mock_oidc = MagicMock()
    mock_oidc.is_enabled = True

    with patch("app.api.routes.auth.get_oidc_client", return_value=mock_oidc):
        resp = await client.get("/api/auth/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["oidc_enabled"] is True
    assert data["authenticated"] is False


@pytest.mark.asyncio
async def test_auth_status_oidc_invalid_token(client: AsyncClient) -> None:
    mock_oidc = MagicMock()
    mock_oidc.is_enabled = True

    with (
        patch("app.api.routes.auth.get_oidc_client", return_value=mock_oidc),
        patch("app.api.routes.auth.verify_session_token", return_value=None),
    ):
        resp = await client.get(
            "/api/auth/status",
            headers={"Authorization": "Bearer bad-token"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["authenticated"] is False


@pytest.mark.asyncio
async def test_login_disabled_when_no_oidc(client: AsyncClient) -> None:
    mock_oidc = MagicMock()
    mock_oidc.is_enabled = False

    with patch("app.api.routes.auth.get_oidc_client", return_value=mock_oidc):
        resp = await client.get("/api/auth/login")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_callback_disabled_when_no_oidc(client: AsyncClient) -> None:
    mock_oidc = MagicMock()
    mock_oidc.is_enabled = False

    with patch("app.api.routes.auth.get_oidc_client", return_value=mock_oidc):
        resp = await client.get("/api/auth/callback?code=abc&state=xyz", follow_redirects=False)

    assert resp.status_code == 307
    assert "error=" in resp.headers["location"]


@pytest.mark.asyncio
async def test_logout(client: AsyncClient) -> None:
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 200
    assert resp.json()["message"] == "Logged out successfully"


@pytest.mark.asyncio
async def test_me_invalid_header(client: AsyncClient) -> None:
    with patch("app.api.dependencies.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(oidc_enabled=True)
        resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": "Token xyz"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_expired_token(client: AsyncClient) -> None:
    with (
        patch("app.api.dependencies.get_settings", return_value=MagicMock(oidc_enabled=True)),
        patch("app.api.dependencies.verify_session_token", return_value=None),
    ):
        resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer expired-token"},
        )
    assert resp.status_code == 401


# --- WebSocket ticket tests ---


@pytest.mark.asyncio
async def test_create_ws_ticket_has_ws_type() -> None:
    """create_ws_ticket produces a JWT with type=ws and short expiry."""
    user_id = uuid4()
    with patch("app.core.auth.get_settings") as mock_settings:
        settings = MagicMock()
        settings.secret_key = "test-secret"  # pragma: allowlist secret
        mock_settings.return_value = settings

        token = create_ws_ticket(user_id)

    payload = jwt.decode(
        token, "test-secret", algorithms=[JWT_ALGORITHM]
    )  # pragma: allowlist secret
    assert payload["type"] == "ws"
    assert payload["sub"] == str(user_id)
    assert "roles" not in payload
    assert payload["exp"] - payload["iat"] == WS_TICKET_EXPIRATION_SECONDS


@pytest.mark.asyncio
async def test_verify_ws_ticket_valid() -> None:
    """verify_ws_ticket accepts a valid ws ticket."""
    user_id = uuid4()
    with patch("app.core.auth.get_settings") as mock_settings:
        settings = MagicMock()
        settings.secret_key = "test-secret"  # pragma: allowlist secret
        mock_settings.return_value = settings

        token = create_ws_ticket(user_id)
        payload = verify_ws_ticket(token)

    assert payload is not None
    assert payload["sub"] == str(user_id)
    assert payload["type"] == "ws"


@pytest.mark.asyncio
async def test_verify_ws_ticket_rejects_session_token() -> None:
    """verify_ws_ticket rejects a session token (type=session)."""
    now = datetime.now(UTC)
    session_payload = {
        "sub": str(uuid4()),
        "roles": ["admin"],
        "iat": now,
        "exp": now + timedelta(hours=24),
        "type": "session",
    }
    token = jwt.encode(
        session_payload, "test-secret", algorithm=JWT_ALGORITHM
    )  # pragma: allowlist secret

    with patch("app.core.auth.get_settings") as mock_settings:
        settings = MagicMock()
        settings.secret_key = "test-secret"  # pragma: allowlist secret
        mock_settings.return_value = settings

        result = verify_ws_ticket(token)

    assert result is None


@pytest.mark.asyncio
async def test_verify_ws_ticket_rejects_expired() -> None:
    """verify_ws_ticket rejects an expired ticket."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid4()),
        "iat": now - timedelta(seconds=30),
        "exp": now - timedelta(seconds=20),
        "type": "ws",
    }
    token = jwt.encode(payload, "test-secret", algorithm=JWT_ALGORITHM)  # pragma: allowlist secret

    with patch("app.core.auth.get_settings") as mock_settings:
        settings = MagicMock()
        settings.secret_key = "test-secret"  # pragma: allowlist secret
        mock_settings.return_value = settings

        result = verify_ws_ticket(token)

    assert result is None


@pytest.mark.asyncio
async def test_ws_ticket_endpoint_no_auth(client: AsyncClient) -> None:
    """POST /auth/ws-ticket without auth returns 401."""
    with patch("app.api.dependencies.get_settings", return_value=MagicMock(oidc_enabled=True)):
        resp = await client.post("/api/auth/ws-ticket")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ws_ticket_endpoint_invalid_session(client: AsyncClient) -> None:
    """POST /auth/ws-ticket with invalid session token returns 401."""
    with (
        patch("app.api.dependencies.get_settings", return_value=MagicMock(oidc_enabled=True)),
        patch("app.api.dependencies.verify_session_token", return_value=None),
    ):
        resp = await client.post(
            "/api/auth/ws-ticket",
            headers={"Authorization": "Bearer bad-token"},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ws_ticket_endpoint_success(client: AsyncClient, mock_db: AsyncMock) -> None:
    """POST /auth/ws-ticket returns a ticket for valid session."""
    from app.api.dependencies import get_current_user

    user_id = uuid4()
    mock_user = MagicMock()
    mock_user.id = user_id
    mock_user.is_active = True

    app.dependency_overrides[get_current_user] = lambda: mock_user

    with patch(
        "app.api.routes.auth.create_ws_ticket", return_value="mock-ws-ticket"
    ) as mock_create:
        resp = await client.post("/api/auth/ws-ticket")

    assert resp.status_code == 200
    assert resp.json()["ticket"] == "mock-ws-ticket"
    mock_create.assert_called_once_with(user_id)


# --- extract_ws_ticket tests ---


@pytest.mark.parametrize(
    "header, expected",
    [
        ("inkwest, ticket|abc123", "abc123"),
        ("ticket|abc123", "abc123"),
        ("inkwest", None),
        ("", None),
        ("inkwest , ticket|abc123 , other", "abc123"),
        ("inkwest, auth|abc123", None),
        ("ticket|", ""),
    ],
    ids=[
        "normal",
        "ticket-only",
        "no-ticket",
        "empty",
        "whitespace-padded",
        "old-auth-prefix",
        "empty-value",
    ],
)
def test_extract_ws_ticket(header: str, expected: str | None) -> None:
    assert extract_ws_ticket(header) == expected


# --- truncate_messages tests ---


def test_truncate_messages_no_op_when_under_limit() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert truncate_messages(msgs, 10) is msgs


def test_truncate_messages_preserves_tool_pairs() -> None:
    """Truncation should not orphan a tool_result at the start."""
    msgs = [
        {"role": "user", "content": "q1"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "kubectl_get", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "answer"},
    ]
    result = truncate_messages(msgs, 3)
    # Should not start with tool_result
    assert result[0]["role"] == "user"
    assert isinstance(result[0]["content"], str)


def test_truncate_messages_drops_leading_tool_results() -> None:
    msgs = [
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}],
        },
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "answer"},
    ]
    result = truncate_messages(msgs, 3)
    assert len(result) == 2
    assert result[0]["content"] == "q2"


# --- pubsub notify test ---


@pytest.mark.asyncio
async def test_pubsub_notify_calls_pg_notify() -> None:
    """Verify notify() uses parameterized pg_notify instead of string interpolation."""
    mock_driver = AsyncMock()
    mock_raw = MagicMock()
    mock_raw.driver_connection = mock_driver

    mock_conn = AsyncMock()
    mock_conn.get_raw_connection = AsyncMock(return_value=mock_raw)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)

    with patch("app.services.pubsub.engine") as mock_engine:
        mock_engine.connect = MagicMock(return_value=mock_conn)
        from app.services.pubsub import notify

        payload = {"title": "it's a test"}
        await notify(payload, user_id="u1")

    mock_driver.execute.assert_called_once()
    call_args = mock_driver.execute.call_args
    assert call_args[0][0] == "SELECT pg_notify($1, $2)"
    assert call_args[0][1] == "session_changes"
    import json

    # notify() merges the scoping user_id into the published payload.
    assert json.loads(call_args[0][2]) == {**payload, "user_id": "u1"}
