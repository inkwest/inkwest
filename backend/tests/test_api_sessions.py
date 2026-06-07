"""Tests for session API routes."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_sessions_empty(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    resp = await authenticated_client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_sessions_returns_items(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    count_result = MagicMock()
    count_result.scalar.return_value = 1
    list_result = MagicMock()
    list_result.scalars.return_value.all.return_value = [mock_session_obj]
    mock_db.execute = AsyncMock(side_effect=[count_result, list_result])

    resp = await authenticated_client.get("/api/sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["title"] == "Test Session"


@pytest.mark.asyncio
async def test_create_session_cluster_not_found(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.post(
        "/api/sessions",
        json={"cluster_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_session_cluster_access_denied(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_cluster: MagicMock,
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=result_mock)

    with patch(
        "app.api.routes.sessions.check_cluster_access",
        side_effect=HTTPException(status_code=403, detail="Forbidden"),
    ):
        resp = await authenticated_client.post(
            "/api/sessions",
            json={"cluster_id": str(mock_cluster.id)},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_session_success(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_cluster: MagicMock,
    mock_session_obj: MagicMock,
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()

    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_session_obj.id
        obj.cluster_id = mock_cluster.id
        obj.user_id = mock_session_obj.user_id
        obj.title = None
        obj.summary = None
        obj.is_archived = False
        obj.total_input_tokens = 0
        obj.total_output_tokens = 0
        obj.total_tool_calls = 0
        obj.total_iterations = 0
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)
        obj.cluster = mock_cluster

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    with (
        patch("app.api.routes.sessions.check_cluster_access", return_value=None),
        patch("app.api.routes.sessions.notify", return_value=None),
    ):
        resp = await authenticated_client.post(
            "/api/sessions",
            json={"cluster_id": str(mock_cluster.id)},
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_session_without_cluster(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """a session can be created with no cluster configured."""
    mock_db.flush = AsyncMock()

    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_session_obj.id
        obj.cluster_id = None
        obj.user_id = mock_session_obj.user_id
        obj.title = None
        obj.summary = None
        obj.is_archived = False
        obj.total_input_tokens = 0
        obj.total_output_tokens = 0
        obj.total_tool_calls = 0
        obj.total_iterations = 0
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)
        obj.cluster = None

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    # No cluster lookup nor access check should run when cluster_id is absent.
    with (
        patch(
            "app.api.routes.sessions.check_cluster_access",
            side_effect=AssertionError("cluster access must not be checked"),
        ),
        patch("app.api.routes.sessions.notify", return_value=None),
    ):
        resp = await authenticated_client.post("/api/sessions", json={})
    assert resp.status_code == 201
    assert resp.json()["cluster_id"] is None
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_session_not_found(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    sid = uuid.uuid4()
    resp = await authenticated_client.get(f"/api/sessions/{sid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_session_access_denied(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
) -> None:
    """User cannot access another user's session."""
    mock_session_obj.user_id = str(uuid.uuid4())  # different user
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_update_session(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.patch(
            f"/api/sessions/{mock_session_obj.id}",
            json={"title": "Updated Title"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_session(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()

    with patch("app.api.routes.sessions.notify", return_value=None):
        resp = await authenticated_client.delete(f"/api/sessions/{mock_session_obj.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_get_session_stats(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_input_tokens"] == 100
    assert data["total_output_tokens"] == 50


@pytest.mark.asyncio
async def test_get_failed_messages_empty(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    msg_result = MagicMock()
    msg_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(side_effect=[session_result, msg_result])

    resp = await authenticated_client.get(f"/api/sessions/{mock_session_obj.id}/failed-messages")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_retry_message_not_found(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    msg_result = MagicMock()
    msg_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[session_result, msg_result])

    mid = uuid.uuid4()
    resp = await authenticated_client.post(
        f"/api/sessions/{mock_session_obj.id}/messages/{mid}/retry"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retry_message_not_retryable(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    mock_session_obj: MagicMock,
    mock_user: MagicMock,
) -> None:
    mock_session_obj.user_id = str(mock_user.id)
    msg = MagicMock()
    msg.is_retryable = False

    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = mock_session_obj
    msg_result = MagicMock()
    msg_result.scalar_one_or_none.return_value = msg
    mock_db.execute = AsyncMock(side_effect=[session_result, msg_result])

    mid = uuid.uuid4()
    resp = await authenticated_client.post(
        f"/api/sessions/{mock_session_obj.id}/messages/{mid}/retry"
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_search_requires_min_length(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.get("/api/search?q=x")
    assert resp.status_code == 422
