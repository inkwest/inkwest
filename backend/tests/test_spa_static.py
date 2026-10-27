"""Tests for the SPA static mount used by the combined production image."""

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import mount_spa


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<!DOCTYPE html><title>spa</title>")
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "app-abc123.js").write_text("console.log('spa')")
    return tmp_path


@pytest.fixture
async def client(static_dir: Path) -> AsyncGenerator[AsyncClient, None]:
    spa_app = FastAPI()
    assert mount_spa(spa_app, static_dir) is True
    transport = ASGITransport(app=spa_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_mount_spa_skips_missing_dir(tmp_path: Path) -> None:
    assert mount_spa(FastAPI(), tmp_path / "does-not-exist") is False


@pytest.mark.asyncio
async def test_root_serves_index_with_no_cache(client: AsyncClient) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "spa" in resp.text
    assert resp.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_client_route_falls_back_to_index(client: AsyncClient) -> None:
    resp = await client.get("/sessions/some-id")
    assert resp.status_code == 200
    assert "spa" in resp.text
    assert resp.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_existing_asset_served_without_no_cache(client: AsyncClient) -> None:
    resp = await client.get("/assets/app-abc123.js")
    assert resp.status_code == 200
    assert "cache-control" not in resp.headers


@pytest.mark.asyncio
async def test_unknown_api_path_stays_404(client: AsyncClient) -> None:
    # Version skew (new frontend / old backend) must surface as a 404, not as
    # index.html that the client would try to JSON-parse.
    resp = await client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert "text/html" not in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_missing_asset_stays_404(client: AsyncClient) -> None:
    # A purged hashed bundle must not come back as text/html (module MIME error).
    resp = await client.get("/assets/app-gone.js")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_ws_path_stays_404(client: AsyncClient) -> None:
    resp = await client.get("/ws/nope")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_service_worker_served_as_js_with_no_cache(
    client: AsyncClient, static_dir: Path
) -> None:
    # 003-AC12: an unhashed worker must revalidate, or browsers keep running a stale one.
    (static_dir / "sw.js").write_text("self.addEventListener('push', () => {});")
    resp = await client.get("/sw.js")
    assert resp.status_code == 200
    assert "addEventListener" in resp.text
    assert "javascript" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_missing_service_worker_stays_404(client: AsyncClient) -> None:
    # 003-AC12: index.html in its place would fail registration with a MIME error.
    resp = await client.get("/sw.js")
    assert resp.status_code == 404
    assert "text/html" not in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_nested_sw_js_route_still_falls_back(client: AsyncClient) -> None:
    # Only the root worker path is exempt; a client route ending in sw.js keeps the SPA fallback.
    resp = await client.get("/sessions/sw.js")
    assert resp.status_code == 200
    assert "spa" in resp.text
