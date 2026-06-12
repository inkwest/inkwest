"""Tests for health check API routes."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c  # type: ignore[misc]


@pytest.mark.asyncio
async def test_health_returns_200(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "healthy"}


@pytest.mark.asyncio
async def test_root_returns_api_info(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Inkwest API"
    assert "version" in data


@pytest.mark.asyncio
async def test_version_reflects_build_settings(client: AsyncClient) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    resp = await client.get("/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": settings.version, "commit": settings.commit_hash}


@pytest.mark.asyncio
async def test_version_is_unauthenticated(client: AsyncClient) -> None:
    # No Authorization header: the SPA must reach it during a reconnect even if
    # the session token is momentarily unavailable.
    resp = await client.get("/version")
    assert resp.status_code == 200


def test_dev_version_is_startup_stamped() -> None:
    # Without a build-time VERSION, the "dev" sentinel becomes a startup-stamped
    # version so each restart trips the update banner locally.
    import re as _re

    from app.core.config import Settings

    assert _re.fullmatch(r"0\.0\.0-dev-\d{8}-\d{6}", Settings(version="dev").version)


def test_injected_build_version_is_preserved() -> None:
    # A real build-time VERSION must pass through untouched.
    from app.core.config import Settings

    assert Settings(version="v1.2.3").version == "v1.2.3"


def test_dev_stamping_is_development_only() -> None:
    # A prod image that forgot to inject VERSION must NOT churn a per-restart
    # version: the "dev" sentinel stays static outside development.
    from app.core.config import Settings

    assert Settings(version="dev", app_env="production").version == "dev"
