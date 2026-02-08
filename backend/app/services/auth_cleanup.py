"""
Background task to clean up expired OIDC pending states.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.core.constants import OIDC_STATE_TTL_SECONDS
from app.core.database import async_session_maker
from app.core.logging import get_logger
from app.models.oidc_pending_state import OidcPendingState

logger = get_logger(__name__)

_CLEANUP_INTERVAL_SECONDS = 60

_cleanup_task: asyncio.Task[None] | None = None


async def _cleanup_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            cutoff = datetime.now(UTC) - timedelta(seconds=OIDC_STATE_TTL_SECONDS)
            async with async_session_maker() as db:
                result = await db.execute(
                    delete(OidcPendingState).where(OidcPendingState.created_at < cutoff)
                )
                await db.commit()
                deleted = result.rowcount  # type: ignore[attr-defined]
                if deleted:
                    logger.info("oidc_state_cleanup", deleted=deleted)
        except asyncio.CancelledError:
            logger.info("oidc_state_cleanup_cancelled")
            return
        except Exception:
            logger.exception("oidc_state_cleanup_error")


async def start_state_cleanup() -> None:
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())
        logger.info("oidc_state_cleanup_started")


async def stop_state_cleanup() -> None:
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
    _cleanup_task = None
    logger.info("oidc_state_cleanup_stopped")
