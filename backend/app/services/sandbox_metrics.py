"""
Read-only per-conversation resource telemetry (the session "Monitoring" tab).

Samples live ``docker stats`` for a conversation's container pair
(``inkwest_conv_<tag>`` + ``inkwest_proxy_<tag>``) plus the on-disk footprint of its
durable session dir, so the frontend can chart CPU / memory / network / block-IO over
a rolling window. Purely observational — it never mutates the sandbox, and reuses
:mod:`app.services.sandbox`'s docker-CLI plumbing and naming so the two never drift.

There is no server-side history: the frontend polls a snapshot and keeps its own
rolling window. That matches the ephemeral, idle-reaped sandbox lifecycle (a reaped
conversation simply reports ``not_running`` until its next turn re-provisions it).

A short in-process TTL cache collapses concurrent/rapid polls (several viewers, a few
seconds apart) into at most one ``docker stats`` invocation per session per window.
"""

import asyncio
import json
import os
import re
import shutil
import time
from collections import defaultdict
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger

# Reuse the sandbox's docker primitives + naming/paths so telemetry can't drift from
# how the containers are actually named or driven (these are the module's own internals).
from app.services.sandbox import _docker, _names, session_dir

logger = get_logger(__name__)

# A snapshot is reused for this long: caps docker calls at ~1 per session per window even
# under several viewers polling every few seconds. Short enough to feel live.
_CACHE_TTL_SECONDS = 2.0
# Disk usage moves slowly and a recursive walk is the priciest part of a sample — cache it
# far longer than the stats snapshot so the frequent CPU/mem/net polls stay cheap.
_DISK_TTL_SECONDS = 15.0
_STATS_TIMEOUT_SECONDS = 10.0

# docker formats memory in binary units (MiB/GiB) but net/block IO in decimal (kB/MB) —
# cover both so every field parses to bytes.
_SIZE_UNITS: dict[str, int] = {
    "b": 1,
    "kb": 10**3,
    "mb": 10**6,
    "gb": 10**9,
    "tb": 10**12,
    "pb": 10**15,
    "kib": 2**10,
    "mib": 2**20,
    "gib": 2**30,
    "tib": 2**40,
    "pib": 2**50,
}
_SIZE_RE = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Z]*)\s*$")

# session_id -> (loop-clock stamp, snapshot) / (loop-clock stamp, (used, fs total) bytes)
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_disk_cache: dict[str, tuple[float, tuple[int, int]]] = {}
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def _now() -> float:
    return asyncio.get_running_loop().time()


def _idle_snapshot(status: str) -> dict[str, Any]:
    return {
        "ts": time.time(),
        "status": status,
        "conv": None,
        "proxy": None,
        "disk_bytes": None,
        "disk_total_bytes": None,
    }


def _parse_size(token: str) -> float:
    """Bytes from a docker size token (e.g. ``50MiB``, ``1.2kB``); 0 for ``--``/blank."""
    m = _SIZE_RE.match(token)
    if not m:
        return 0.0
    return float(m.group(1)) * _SIZE_UNITS.get(m.group(2).lower(), 1)


def _parse_pair(token: str) -> tuple[float, float]:
    """Split a docker ``X / Y`` field (MemUsage, NetIO, BlockIO) into (X, Y) bytes."""
    left, _, right = token.partition("/")
    return _parse_size(left), _parse_size(right)


def _parse_pct(token: str) -> float:
    try:
        return float(token.strip().rstrip("%"))
    except ValueError:
        return 0.0


def _container_stats(row: dict[str, str]) -> dict[str, float]:
    """One ``docker stats --format '{{json .}}'`` row → numeric, byte-normalized fields."""
    mem_used, mem_limit = _parse_pair(row.get("MemUsage", ""))
    net_rx, net_tx = _parse_pair(row.get("NetIO", ""))
    blk_read, blk_write = _parse_pair(row.get("BlockIO", ""))
    return {
        "cpu_pct": _parse_pct(row.get("CPUPerc", "")),
        "mem_bytes": mem_used,
        "mem_limit_bytes": mem_limit,
        "net_rx_bytes": net_rx,
        "net_tx_bytes": net_tx,
        "blk_read_bytes": blk_read,
        "blk_write_bytes": blk_write,
    }


def _dir_size_bytes(path: str) -> int:
    """Total size of files under ``path`` (best-effort; skips unreadable/vanished entries)."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue  # raced removal / broken symlink — ignore
    return total


def _disk_stats(path: str) -> tuple[int, int]:
    """(bytes used under ``path``, total capacity of its filesystem); best-effort, 0 on error.

    The session dir has no Docker disk quota, so the filesystem capacity is the only real
    ceiling — surfaced as ``disk_total_bytes`` for the chart's "full disk" reference line.
    """
    used = _dir_size_bytes(path)
    try:
        total = shutil.disk_usage(path).total
    except OSError:
        total = 0  # path gone / unmounted between provisioning and the sample
    return used, total


async def _disk_usage(session_id: str) -> tuple[int, int]:
    """Disk (used, fs total) for the durable session dir, behind a longer TTL than the snapshot.

    Runs the walk off the event loop (``to_thread``) so a large ``/workdir`` (e.g. a git
    clone) can't stall request handling.
    """
    now = _now()
    cached = _disk_cache.get(session_id)
    if cached and now - cached[0] < _DISK_TTL_SECONDS:
        return cached[1]
    stats = await asyncio.to_thread(_disk_stats, session_dir(session_id))
    _disk_cache[session_id] = (now, stats)
    return stats


async def _collect(session_id: str) -> dict[str, Any]:
    names = _names(session_id)
    # One docker call for both containers. check=False: a missing/reaped peer prints to
    # stderr with rc=1, but the running one's stats still land on stdout — index by Name.
    _rc, out, _err = await _docker(
        "stats",
        "--no-stream",
        "--no-trunc",
        "--format",
        "{{json .}}",
        names.conv,
        names.proxy,
        check=False,
        timeout=_STATS_TIMEOUT_SECONDS,
    )
    by_name: dict[str, dict[str, str]] = {}
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("Name"):
            by_name[row["Name"]] = row

    conv = _container_stats(by_name[names.conv]) if names.conv in by_name else None
    proxy = _container_stats(by_name[names.proxy]) if names.proxy in by_name else None
    if conv is None and proxy is None:
        return _idle_snapshot("not_running")  # reaped / not yet provisioned

    disk_used, disk_total = await _disk_usage(session_id)
    return {
        "ts": time.time(),
        "status": "running",
        "conv": conv,
        "proxy": proxy,
        "disk_bytes": disk_used,
        "disk_total_bytes": disk_total,
    }


async def sample(session_id: str) -> dict[str, Any]:
    """Current resource snapshot for a conversation's sandbox (cached for the TTL window).

    Returns a dict with ``status`` in ``{disabled, not_running, running}``; ``conv`` and
    ``proxy`` carry per-container byte/percent fields only when ``running``.
    """
    if not get_settings().sandbox_enabled:
        return _idle_snapshot("disabled")

    cached = _cache.get(session_id)
    if cached and _now() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    async with _locks[session_id]:
        cached = _cache.get(session_id)  # another waiter may have just refreshed it
        if cached and _now() - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        try:
            snapshot = await _collect(session_id)
        except Exception:
            # docker daemon unreachable, timeout, … — degrade to "nothing to show" rather
            # than failing the poll; the frontend keeps its existing rolling window.
            logger.exception("sandbox_metrics_collect_failed", session_id=session_id)
            snapshot = _idle_snapshot("not_running")
        _cache[session_id] = (_now(), snapshot)
        return snapshot
