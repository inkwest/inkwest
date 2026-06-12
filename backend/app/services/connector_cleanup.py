"""
Background task: sweep expired connector credentials and pending OAuth states.

Mirrors :mod:`app.services.auth_cleanup`. Credentials also expire lazily on read,
so this is a backstop to keep the in-RAM store and the pending-state table tidy.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.core.constants import CONNECTOR_STATE_TTL_SECONDS
from app.core.database import async_session_maker
from app.core.logging import get_logger
from app.models.connector_pending_state import ConnectorPendingState
from app.services.connectors.store import get_credential_store
from app.services.connectors.warm import get_warm_store

logger = get_logger(__name__)

_CLEANUP_INTERVAL_SECONDS = 60

_cleanup_task: asyncio.Task[None] | None = None


async def _cleanup_loop() -> None:
    store = get_credential_store()
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            removed = await store.sweep()
            if removed:
                logger.info("connector_cred_sweep", removed=removed)
            removed_warm = await get_warm_store().sweep()
            if removed_warm:
                logger.info("connector_warm_sweep", removed=removed_warm)
            cutoff = datetime.now(UTC) - timedelta(seconds=CONNECTOR_STATE_TTL_SECONDS)
            async with async_session_maker() as db:
                result = await db.execute(
                    delete(ConnectorPendingState).where(ConnectorPendingState.created_at < cutoff)
                )
                await db.commit()
                deleted = result.rowcount  # type: ignore[attr-defined]
                if deleted:
                    logger.info("connector_state_cleanup", deleted=deleted)
        except asyncio.CancelledError:
            logger.info("connector_cleanup_cancelled")
            return
        except Exception:
            logger.exception("connector_cleanup_error")


async def start_connector_cleanup() -> None:
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info("connector_cleanup_started")


async def stop_connector_cleanup() -> None:
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
    _cleanup_task = None
    logger.info("connector_cleanup_stopped")
