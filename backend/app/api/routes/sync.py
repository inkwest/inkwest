"""
WebSocket endpoint for real-time session list synchronization.

Clients connect here to receive session change events (create, update, delete)
so multiple browser tabs stay in sync.
"""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import extract_ws_ticket, verify_ws_ticket
from app.core.config import get_settings
from app.core.database import async_session_maker
from app.models.user import User
from app.services.mcp_dek_handshake import handle_dek_message, note_connect, note_disconnect
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


async def _handle_inbound(websocket: WebSocket, uid: UUID, data: object) -> None:
    """Route one client→server frame on the sync socket: heartbeat or DEK handshake.

    Extracted from the loop so the dispatch is unit-testable without a live WebSocket.
    """
    # receive_json() yields any JSON value; a non-object frame (array/scalar) is simply not a
    # frame we speak — ignore it rather than letting `.get` raise into the 1011 teardown.
    if not isinstance(data, dict):
        return
    msg_type = data.get("type")
    if msg_type == "ping":
        await websocket.send_json({"type": "pong"})
    elif isinstance(msg_type, str) and msg_type.startswith("mcp_dek_"):
        reply = await handle_dek_message(uid, data)
        if reply is not None:
            await websocket.send_json(reply)


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

    # The DEK handshake (browser-fractioned MCP key custody) rides this app-wide socket so
    # the user's key is resident whenever a tab is open — not only inside a chat. Without it,
    # connecting an OAuth2 MCP server from the settings panel (no chat socket) leaves the DEK
    # store empty and the OAuth callback fails with "no encryption key available". The chat
    # socket runs the same handshake; the refcount (note_connect/disconnect) is additive, so
    # the DEK survives as long as ANY of the user's sockets is live.
    uid = UUID(user_id)
    await note_connect(uid)
    # Pair note_connect/note_disconnect in a try/finally that wraps the subscribe block too:
    # if `subscribe()` setup raises, the finally still decrements the refcount. Otherwise a
    # leaked count would keep the DEK resident forever (the orphan sweep only evicts DEKs with
    # NO refcount entry) and stop real disconnects from ever reaching zero.
    try:
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
                            await _handle_inbound(websocket, uid, task.result())

            except (WebSocketDisconnect, asyncio.CancelledError):
                pass
            except Exception:
                logger.exception("sync_ws_error")
                try:
                    await websocket.close(code=1011)
                except Exception:
                    pass
    finally:
        # Drop the in-RAM DEK once the user's last socket closes (connected-only custody).
        await note_disconnect(uid)

    logger.info("sync_ws_disconnected")
