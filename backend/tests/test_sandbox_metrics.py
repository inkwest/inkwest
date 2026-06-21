"""Tests for the sandbox resource-metrics collector + its REST snapshot route."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import sandbox_metrics
from app.services.sandbox import _names

SID = "11111111-1111-1111-1111-111111111111"
# Matches conftest's _SESSION_ID, which mock_session/owner checks key off of.
ROUTE = "/api/sessions/00000000-0000-4000-8000-000000000020/metrics"


@pytest.fixture(autouse=True)
def _clear_caches():
    sandbox_metrics._cache.clear()
    sandbox_metrics._disk_cache.clear()
    yield
    sandbox_metrics._cache.clear()
    sandbox_metrics._disk_cache.clear()


def _enable(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    monkeypatch.setattr(
        sandbox_metrics, "get_settings", lambda: SimpleNamespace(sandbox_enabled=enabled)
    )


# --------------------------------------------------------------------------- #
# Pure parsers
# --------------------------------------------------------------------------- #
def test_parse_size_handles_binary_and_decimal_units():
    assert sandbox_metrics._parse_size("1GiB") == 2**30  # memory: binary
    assert sandbox_metrics._parse_size("256MiB") == 256 * 2**20
    assert sandbox_metrics._parse_size("1.5kB") == 1500  # net/block IO: decimal
    assert sandbox_metrics._parse_size("4MB") == 4_000_000
    assert sandbox_metrics._parse_size("512B") == 512


def test_parse_size_tolerates_blanks_and_dashes():
    assert sandbox_metrics._parse_size("--") == 0.0
    assert sandbox_metrics._parse_size("") == 0.0
    assert sandbox_metrics._parse_size("garbage") == 0.0


def test_parse_pair_splits_on_slash():
    assert sandbox_metrics._parse_pair("1.5kB / 2.0kB") == (1500, 2000)


def test_parse_pct():
    assert sandbox_metrics._parse_pct("12.50%") == 12.5
    assert sandbox_metrics._parse_pct("--") == 0.0


def test_container_stats_maps_every_field():
    row = {
        "CPUPerc": "12.50%",
        "MemUsage": "256MiB / 1GiB",
        "NetIO": "1.5kB / 2.0kB",
        "BlockIO": "4MB / 0B",
    }
    s = sandbox_metrics._container_stats(row)
    assert s == {
        "cpu_pct": 12.5,
        "mem_bytes": 256 * 2**20,
        "mem_limit_bytes": 2**30,
        "net_rx_bytes": 1500,
        "net_tx_bytes": 2000,
        "blk_read_bytes": 4_000_000,
        "blk_write_bytes": 0,
    }


def test_dir_size_bytes_sums_recursively(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x" * 10)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_bytes(b"y" * 5)
    assert sandbox_metrics._dir_size_bytes(str(tmp_path)) == 15


def test_dir_size_bytes_missing_dir_is_zero():
    assert sandbox_metrics._dir_size_bytes("/no/such/path") == 0


# --------------------------------------------------------------------------- #
# sample()
# --------------------------------------------------------------------------- #
async def test_sample_disabled_never_touches_docker(monkeypatch):
    _enable(monkeypatch, enabled=False)
    docker = AsyncMock()
    monkeypatch.setattr(sandbox_metrics, "_docker", docker)
    snap = await sandbox_metrics.sample(SID)
    assert snap["status"] == "disabled"
    assert snap["conv"] is None and snap["proxy"] is None
    docker.assert_not_called()


async def test_sample_running_parses_both_containers(monkeypatch, tmp_path):
    _enable(monkeypatch)
    names = _names(SID)
    out = "\n".join(
        [
            json.dumps(
                {
                    "Name": names.conv,
                    "CPUPerc": "12.50%",
                    "MemUsage": "256MiB / 1GiB",
                    "NetIO": "1.5kB / 2.0kB",
                    "BlockIO": "4MB / 0B",
                }
            ),
            json.dumps(
                {
                    "Name": names.proxy,
                    "CPUPerc": "0.30%",
                    "MemUsage": "32MiB / 1GiB",
                    "NetIO": "10kB / 5kB",
                    "BlockIO": "0B / 0B",
                }
            ),
        ]
    )
    monkeypatch.setattr(sandbox_metrics, "_docker", AsyncMock(return_value=(0, out, "")))
    (tmp_path / "f").write_bytes(b"z" * 42)
    monkeypatch.setattr(sandbox_metrics, "session_dir", lambda _sid: str(tmp_path))

    snap = await sandbox_metrics.sample(SID)
    assert snap["status"] == "running"
    assert snap["conv"]["cpu_pct"] == 12.5
    assert snap["conv"]["mem_bytes"] == 256 * 2**20
    assert snap["proxy"]["cpu_pct"] == 0.3
    assert snap["disk_bytes"] == 42
    assert snap["disk_total_bytes"] > 0  # tmp_path's filesystem capacity


async def test_sample_only_proxy_running_still_reports(monkeypatch, tmp_path):
    # A missing conv prints to stderr (rc=1); the proxy's stats still land on stdout.
    _enable(monkeypatch)
    names = _names(SID)
    out = json.dumps(
        {
            "Name": names.proxy,
            "CPUPerc": "1%",
            "MemUsage": "8MiB / 1GiB",
            "NetIO": "0B / 0B",
            "BlockIO": "0B / 0B",
        }
    )
    monkeypatch.setattr(
        sandbox_metrics, "_docker", AsyncMock(return_value=(1, out, "No such container: conv"))
    )
    monkeypatch.setattr(sandbox_metrics, "session_dir", lambda _sid: str(tmp_path))

    snap = await sandbox_metrics.sample(SID)
    assert snap["status"] == "running"
    assert snap["conv"] is None
    assert snap["proxy"]["mem_bytes"] == 8 * 2**20


async def test_sample_not_running_when_no_stats(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        sandbox_metrics, "_docker", AsyncMock(return_value=(1, "", "No such container"))
    )
    snap = await sandbox_metrics.sample(SID)
    assert snap["status"] == "not_running"
    assert snap["conv"] is None and snap["proxy"] is None
    assert snap["disk_bytes"] is None


async def test_sample_caches_within_ttl(monkeypatch, tmp_path):
    _enable(monkeypatch)
    names = _names(SID)
    out = json.dumps(
        {
            "Name": names.conv,
            "CPUPerc": "1%",
            "MemUsage": "1MiB / 1GiB",
            "NetIO": "0B / 0B",
            "BlockIO": "0B / 0B",
        }
    )
    docker = AsyncMock(return_value=(0, out, ""))
    monkeypatch.setattr(sandbox_metrics, "_docker", docker)
    monkeypatch.setattr(sandbox_metrics, "session_dir", lambda _sid: str(tmp_path))

    await sandbox_metrics.sample(SID)
    await sandbox_metrics.sample(SID)
    assert docker.call_count == 1  # second poll served from cache


async def test_sample_degrades_on_collect_error(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        sandbox_metrics, "_docker", AsyncMock(side_effect=RuntimeError("daemon down"))
    )
    snap = await sandbox_metrics.sample(SID)
    assert snap["status"] == "not_running"


# --------------------------------------------------------------------------- #
# REST route
# --------------------------------------------------------------------------- #
async def test_metrics_route_returns_snapshot(
    authenticated_client, mock_db, mock_user, monkeypatch
):
    from app.services import sandbox_metrics as sm

    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(id=SID, user_id=mock_user.id)
    mock_db.execute.return_value = result
    monkeypatch.setattr(
        sm,
        "sample",
        AsyncMock(
            return_value={
                "ts": 1.0,
                "status": "running",
                "conv": {
                    "cpu_pct": 5.0,
                    "mem_bytes": 1.0,
                    "mem_limit_bytes": 2.0,
                    "net_rx_bytes": 0.0,
                    "net_tx_bytes": 0.0,
                    "blk_read_bytes": 0.0,
                    "blk_write_bytes": 0.0,
                },
                "proxy": None,
                "disk_bytes": 10.0,
            }
        ),
    )

    resp = await authenticated_client.get(ROUTE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["conv"]["cpu_pct"] == 5.0
    assert body["proxy"] is None
    assert body["disk_bytes"] == 10.0


async def test_metrics_route_404_when_session_missing(authenticated_client, mock_db):
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = result
    resp = await authenticated_client.get(ROUTE)
    assert resp.status_code == 404


async def test_metrics_route_400_on_bad_session_id(authenticated_client):
    resp = await authenticated_client.get("/api/sessions/not-a-uuid/metrics")
    assert resp.status_code == 400
