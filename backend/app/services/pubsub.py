"""
Postgres LISTEN/NOTIFY pub/sub with in-process fan-out.

Single listener task receives NOTIFY events and distributes them
to per-WebSocket asyncio.Queues.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.database import engine
from app.core.logging import get_logger

logger = get_logger(__name__)

CHANNEL = "session_changes"

_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
_listener_task: asyncio.Task[None] | None = None


async def notify(payload: dict[str, Any]) -> None:
    """Send a NOTIFY on the session_changes channel."""
    raw = json.dumps(payload)
    async with engine.connect() as conn:
        raw_conn = await conn.get_raw_connection()
        await raw_conn.driver_connection.execute(  # type: ignore[union-attr]
            "SELECT pg_notify($1, $2)", CHANNEL, raw
        )


@asynccontextmanager
async def subscribe() -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
    """Subscribe to session change events. Yields an asyncio.Queue."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    _subscribers.add(queue)
    try:
        yield queue
    finally:
        _subscribers.discard(queue)


def _fan_out(payload: dict[str, Any]) -> None:
    """Distribute a payload to all subscriber queues (non-blocking)."""
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("pubsub_queue_full, dropping event")


async def _listen_loop() -> None:
    """Background task: LISTEN on Postgres and fan-out to subscribers."""
    while True:
        try:
            async with engine.connect() as conn:
                raw = await conn.get_raw_connection()
                pg = raw.driver_connection
                assert pg is not None, "driver_connection unavailable"
                await pg.add_listener(CHANNEL, _on_notification)  # type: ignore[union-attr]
                logger.info("pubsub_listener_started", channel=CHANNEL)
                # Keep connection alive — asyncpg listener needs the connection open
                while True:
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("pubsub_listener_cancelled")
            return
        except Exception:
            logger.exception("pubsub_listener_error, reconnecting in 5s")
            await asyncio.sleep(5)


def _on_notification(conn: Any, pid: int, channel: str, payload: str) -> None:
    """asyncpg notification callback — runs in the event loop."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        data = {"raw": payload}
    _fan_out(data)


async def start_listener() -> None:
    """Start the background LISTEN task."""
    global _listener_task
    if _listener_task is None or _listener_task.done():
        _listener_task = asyncio.create_task(_listen_loop())
        logger.info("pubsub_listener_task_created")


async def stop_listener() -> None:
    """Cancel the background LISTEN task."""
    global _listener_task
    if _listener_task and not _listener_task.done():
        _listener_task.cancel()
        try:
            await _listener_task
        except asyncio.CancelledError:
            pass
    _listener_task = None
    logger.info("pubsub_listener_task_stopped")
