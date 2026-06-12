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

# Each subscriber queue maps to the connection's user_id; None = receive every
# event (dev single-user). Events are fanned out only to the matching user so one
# user's activity never reaches another user's tabs.
_subscribers: dict[asyncio.Queue[dict[str, Any]], str | None] = {}
_listener_task: asyncio.Task[None] | None = None


async def notify(payload: dict[str, Any], *, user_id: str | None) -> None:
    """Publish a session-change event on the channel, scoped to one user.

    ``user_id`` is a REQUIRED keyword so a new call site cannot silently omit it
    and re-open cross-user delivery (see ``_fan_out``). Pass ``None`` only for a
    deliberate broadcast to every connection.
    """
    raw = json.dumps({**payload, "user_id": user_id})
    async with engine.connect() as conn:
        raw_conn = await conn.get_raw_connection()
        await raw_conn.driver_connection.execute(  # type: ignore[union-attr]
            "SELECT pg_notify($1, $2)", CHANNEL, raw
        )


@asynccontextmanager
async def subscribe(user_id: str | None = None) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
    """Subscribe to session change events for one connection.

    ``user_id`` scopes delivery: the queue only receives events whose payload
    ``user_id`` matches (so a user's activity never reaches another user's tabs).
    None receives everything — used in dev where there is a single user.
    """
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
    _subscribers[queue] = user_id
    try:
        yield queue
    finally:
        _subscribers.pop(queue, None)


def _fan_out(payload: dict[str, Any]) -> None:
    """Distribute a payload to the matching subscriber queues (non-blocking).

    A queue receives the event when it is unscoped (None) or its user_id equals
    the event's. An event with no ``user_id`` falls back to every subscriber, so a
    missing field degrades to the old broadcast rather than silently dropping.
    """
    target = payload.get("user_id")
    for q, uid in list(_subscribers.items()):
        if target is not None and uid is not None and uid != target:
            continue
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
