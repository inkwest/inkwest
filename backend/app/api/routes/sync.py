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


async def _authenticate_sync_ws(websocket: WebSocket, db: AsyncSession) -> tuple[bool, str | None]:
    """Authenticate the sync WebSocket. Returns (ok, user_id), fail-closed.

    ``user_id`` scopes which events this connection receives — always the concrete
    user's id (the dev user has one too), never None. Scoping is now load-bearing:
    sharing emits events addressed to a *specific* recipient (e.g. a share_invite to
    the invitee), so a None-scoped "receive all" socket would wrongly surface another
    user's invite to the inviter.
    """
    try:
        settings = get_settings()

        if not settings.oidc_enabled:
            from app.api.dependencies import get_or_create_dev_user

            # Honor the ws-ticket (always sent) so dev-mode user switching scopes
            # this socket to the impersonated user; else the default dev user.
            header = websocket.headers.get("sec-websocket-protocol", "")
            token = extract_ws_ticket(header)
            if token:
                payload = verify_ws_ticket(token)
                sub = payload.get("sub") if payload else None
                if sub:
                    result = await db.execute(select(User).where(User.id == sub))
                    user = result.scalar_one_or_none()
                    if user is not None and user.is_active:
                        return True, str(user.id)
            dev_user = await get_or_create_dev_user(db)
            return True, str(dev_user.id)

        header = websocket.headers.get("sec-websocket-protocol", "")
        token = extract_ws_ticket(header)
        if not token:
            return False, None

        payload = verify_ws_ticket(token)
        if not payload:
            return False, None

        user_id = payload.get("sub")
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            return False, None
        return True, str(user.id)
    except Exception:
        logger.exception("sync_ws_auth_error")
        return False, None


@router.websocket("/ws/sync")
async def websocket_sync(websocket: WebSocket) -> None:
    async with async_session_maker() as db:
        ok, user_id = await _authenticate_sync_ws(websocket, db)
        if not ok:
            await websocket.close(code=4001)
            logger.warning("sync_ws_auth_rejected")
            return

    await websocket.accept(subprotocol="inkwest")
    logger.info("sync_ws_connected")

    async with subscribe(user_id) as queue:
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
