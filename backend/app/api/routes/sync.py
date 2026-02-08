"""
WebSocket endpoint for real-time session list synchronization.

Clients connect here to receive session change events (create, update, delete)
so multiple browser tabs stay in sync.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import extract_ws_ticket, verify_ws_ticket
from app.core.config import get_settings
from app.core.database import async_session_maker
from app.models.user import User
from app.services.pubsub import subscribe

logger = logging.getLogger(__name__)

router = APIRouter()


async def _authenticate_sync_ws(websocket: WebSocket, db: AsyncSession) -> bool:
    """Authenticate sync WebSocket. Returns False on any failure (fail-closed)."""
    try:
        settings = get_settings()

        if not settings.oidc_enabled:
            return True

        header = websocket.headers.get("sec-websocket-protocol", "")
        token = extract_ws_ticket(header)
        if not token:
            return False

        payload = verify_ws_ticket(token)
        if not payload:
            return False

        user_id = payload.get("sub")
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        return user is not None and user.is_active
    except Exception:
        logger.exception("sync_ws_auth_error")
        return False


@router.websocket("/ws/sync")
async def websocket_sync(websocket: WebSocket) -> None:
    async with async_session_maker() as db:
        if not await _authenticate_sync_ws(websocket, db):
            await websocket.close(code=4001)
            logger.warning("sync_ws_auth_rejected")
            return

    await websocket.accept(subprotocol="inkwest")
    logger.info("sync_ws_connected")

    async with subscribe() as queue:
        try:
            while True:
                recv_task = asyncio.create_task(websocket.receive_json())
                event_task = asyncio.create_task(queue.get())

                done, pending = await asyncio.wait(
                    {recv_task, event_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                for task in done:
                    if task is event_task:
                        event = task.result()
                        await websocket.send_json(event)
                    elif task is recv_task:
                        data = task.result()
                        if data.get("type") == "ping":
                            await websocket.send_json({"type": "pong"})

        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception("sync_ws_error")
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    logger.info("sync_ws_disconnected")
