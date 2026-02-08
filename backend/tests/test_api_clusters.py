"""Tests for cluster API routes."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_clusters_empty(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await authenticated_client.get("/api/clusters")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_clusters_returns_items(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_cluster: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [mock_cluster]
    mock_db.execute = AsyncMock(return_value=result_mock)

    with patch("app.api.routes.clusters.get_user_allowed_cluster_ids", return_value=None):
        resp = await authenticated_client.get("/api/clusters")

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["name"] == "test-cluster"


@pytest.mark.asyncio
async def test_create_cluster_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    resp = await authenticated_client.post(
        "/api/clusters",
        json={
            "name": "new-cluster",
            "display_name": "New Cluster",
            "kubeconfig_path": "/tmp/kc",
        },
    )
    # Regular user -> 403 (admin required via dependency)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_cluster_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_cluster: MagicMock
) -> None:
    # No existing cluster
    no_result = MagicMock()
    no_result.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=no_result)
    mock_db.flush = AsyncMock()
    mock_db.refresh = AsyncMock()

    # After refresh, db.add was called with a Cluster object
    # We simulate refresh populating the object
    def fake_refresh(obj: MagicMock) -> None:
        obj.id = mock_cluster.id
        obj.name = "new-cluster"
        obj.display_name = "New Cluster"
        obj.kubeconfig_path = "/tmp/kc"
        obj.context_name = None
        obj.description = None
        obj.is_active = True
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)

    mock_db.refresh = AsyncMock(side_effect=fake_refresh)

    with patch("app.api.routes.clusters.Path.is_file", return_value=True):
        resp = await admin_client.post(
            "/api/clusters",
            json={
                "name": "new-cluster",
                "display_name": "New Cluster",
                "kubeconfig_path": "/tmp/kc",
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "new-cluster"


@pytest.mark.asyncio
async def test_create_cluster_conflict(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_cluster: MagicMock
) -> None:
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=existing_result)

    with patch("app.api.routes.clusters.Path.is_file", return_value=True):
        resp = await admin_client.post(
            "/api/clusters",
            json={
                "name": "test-cluster",
                "display_name": "Test Cluster",
                "kubeconfig_path": "/tmp/kc",
            },
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_cluster_kubeconfig_not_found(
    admin_client: AsyncClient, mock_db: AsyncMock
) -> None:
    resp = await admin_client.post(
        "/api/clusters",
        json={
            "name": "bad-cluster",
            "display_name": "Bad Cluster",
            "kubeconfig_path": "/nonexistent/kubeconfig.yaml",
        },
    )
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_cluster_kubeconfig_not_found(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_cluster: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=result_mock)

    resp = await admin_client.patch(
        f"/api/clusters/{mock_cluster.id}",
        json={"kubeconfig_path": "/nonexistent/kubeconfig.yaml"},
    )
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_cluster_not_found(authenticated_client: AsyncClient, mock_db: AsyncMock) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(return_value=result_mock)

    cid = uuid.uuid4()
    resp = await authenticated_client.get(f"/api/clusters/{cid}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_cluster_access_denied(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_cluster: MagicMock
) -> None:
    """Security: non-admin user without cluster access gets 403."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=result_mock)

    with patch(
        "app.api.routes.clusters.check_cluster_access",
        side_effect=HTTPException(status_code=403, detail="Forbidden"),
    ):
        resp = await authenticated_client.get(f"/api/clusters/{mock_cluster.id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_cluster_success(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_cluster: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=result_mock)

    with patch("app.api.routes.clusters.check_cluster_access", return_value=None):
        resp = await authenticated_client.get(f"/api/clusters/{mock_cluster.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "test-cluster"


@pytest.mark.asyncio
async def test_update_cluster_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    cid = uuid.uuid4()
    resp = await authenticated_client.patch(
        f"/api/clusters/{cid}",
        json={"display_name": "Updated"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_cluster_requires_admin(
    authenticated_client: AsyncClient,
) -> None:
    cid = uuid.uuid4()
    resp = await authenticated_client.delete(f"/api/clusters/{cid}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_cluster_as_admin(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_cluster: MagicMock
) -> None:
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = mock_cluster
    mock_db.execute = AsyncMock(return_value=result_mock)
    mock_db.flush = AsyncMock()

    resp = await admin_client.delete(f"/api/clusters/{mock_cluster.id}")
    assert resp.status_code == 204
