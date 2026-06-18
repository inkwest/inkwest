"""
WebSocket endpoint for real-time chat with AI assistant.

This module implements the WebSocket endpoint that:
1. Accepts WebSocket connections for chat sessions
2. Validates session and cluster information
3. Handles client messages (chat messages and pings)
4. Streams AI responses and tool execution events
5. Manages connection lifecycle and health
"""

import asyncio
import json
import logging
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_or_create_dev_user
from app.core.auth import extract_ws_ticket, verify_ws_ticket
from app.core.config import get_settings
from app.core.database import async_session_maker
from app.models.cluster import Cluster
from app.models.session import Session
from app.models.user import User
from app.services.ai_client import get_orchestrator
from app.services.turn_runner import TURN_END, turn_runner

logger = logging.getLogger(__name__)

router = APIRouter()

active_connections: dict[str, set[WebSocket]] = defaultdict(set)
_connections_lock = asyncio.Lock()


@dataclass(frozen=True)
class ClusterContext:
    display_name: str
    kubeconfig_path: str
    context_name: str | None = None


async def connect(session_id: str, websocket: WebSocket) -> None:
    """Register a WebSocket connection for a session."""
    await websocket.accept(subprotocol="inkwest")
    async with _connections_lock:
        active_connections[session_id].add(websocket)
        count = len(active_connections[session_id])
    logger.info(
        "WebSocket connected",
        extra={
            "session_id": session_id,
            "client": websocket.client.host if websocket.client else "unknown",
            "active_connections": count,
        },
    )


async def disconnect(session_id: str, websocket: WebSocket) -> None:
    """Unregister a WebSocket connection for a session."""
    async with _connections_lock:
        active_connections[session_id].discard(websocket)
        if not active_connections[session_id]:
            del active_connections[session_id]
        remaining = len(active_connections.get(session_id, set()))
    logger.info(
        "WebSocket disconnected",
        extra={
            "session_id": session_id,
            "remaining_connections": remaining,
        },
    )


async def get_active_connection_count(session_id: str) -> int:
    """
    Get the number of active connections for a session.

    Args:
        session_id: The session ID

    Returns:
        Number of active connections
    """
    async with _connections_lock:
        return len(active_connections.get(session_id, set()))


async def send_error(websocket: WebSocket, message: str, close_code: int | None = None) -> None:
    """
    Send an error message to the client and optionally close the connection.

    Args:
        websocket: The WebSocket connection
        message: Error message to send
        close_code: Optional WebSocket close code
    """
    try:
        await websocket.send_json({"type": "error", "content": message})
        if close_code is not None:
            await websocket.close(code=close_code)
    except Exception:
        pass


async def validate_websocket_auth(websocket: WebSocket, db: AsyncSession) -> User | None:
    """Authenticate WebSocket connection. Returns user or None on failure."""
    settings = get_settings()

    if not settings.oidc_enabled:
        return await get_or_create_dev_user(db)

    header = websocket.headers.get("sec-websocket-protocol", "")
    token = extract_ws_ticket(header)
    if not token:
        return None

    payload = verify_ws_ticket(token)
    if not payload:
        return None

    user_id = payload.get("sub")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    return user


async def validate_session(
    db: AsyncSession, session_id: str, user: User | None = None
) -> tuple[Session | None, Cluster | None, str | None]:
    """
    Validate that a session exists, has an active cluster,
    and optionally check user ownership.
    """
    try:
        session = await db.get(Session, session_id)
        if not session:
            return None, None, "Session not found"

        if user and session.user_id and not user.is_admin():
            if str(session.user_id) != str(user.id):
                return None, None, "Access denied"

        # A session may have no cluster: start without a Kube context.
        if session.cluster_id is None:
            return session, None, None

        cluster = await db.get(Cluster, session.cluster_id)
        if not cluster:
            return None, None, "Cluster not found"

        if not cluster.is_active:
            return None, None, "Cluster is not active"

        return session, cluster, None

    except Exception as e:
        logger.exception(f"Error validating session {session_id}")
        return None, None, f"Error validating session: {str(e)}"


async def close_session_connections(session_id: str) -> None:
    """Close all active WS connections for a session, triggering orchestrator shutdown."""
    async with _connections_lock:
        connections = list(active_connections.get(session_id, set()))
    for ws in connections:
        try:
            await ws.close(code=4010)
        except Exception:
            pass


@router.websocket("/ws/chat/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str) -> None:
    """
    WebSocket endpoint for chat.

    Client sends:
        {"type": "message", "content": "..."} - Send a chat message
        {"type": "ping"} - Keepalive ping

    Server sends:
        {"type": "text_delta", "content": "..."} - AI text response chunk
        {"type": "tool_start", "tool_name": "...", "tool_input": {...}} - Tool execution starting
        {"type": "tool_result", "tool_name": "...", "content": "..."} - Tool result
        {"type": "done", "metadata": {...}} - Response complete
        {"type": "error", "content": "..."} - Error occurred
        {"type": "pong"} - Pong response to ping

    Args:
        websocket: The WebSocket connection
        session_id: The chat session ID
    """
    # Validate auth and session before accepting the connection
    cluster_ctx: ClusterContext | None = None
    user_id = None
    async with async_session_maker() as db:
        user = await validate_websocket_auth(websocket, db)
        if not user:
            await websocket.close(code=4001)
            return

        user_id = user.id
        await db.commit()

        session, cluster, error = await validate_session(db, session_id, user)

        if error:
            await websocket.close(code=4004)
            return

        # cluster may be None: session runs without a Kube context.
        if cluster is not None:
            cluster_ctx = ClusterContext(
                display_name=cluster.display_name,
                kubeconfig_path=cluster.kubeconfig_path,
                context_name=cluster.context_name,
            )

    await connect(session_id, websocket)

    logger.info(
        "Session validated",
        extra={
            "session_id": session_id,
            "cluster_name": cluster_ctx.display_name if cluster_ctx else None,
        },
    )

    # Orchestration runs detached from this socket via the turn_runner, so a turn
    # survives the client navigating away (and still persists its result). This
    # socket only (a) starts/stops turns and (b) streams a turn's events. A single
    # pump task forwards the active turn's buffered + live events to this client;
    # it's recreated whenever we (re)attach to a turn.
    base_kwargs = {
        "session_id": session_id,
        "cluster_name": cluster_ctx.display_name if cluster_ctx else None,
        "kubeconfig_path": cluster_ctx.kubeconfig_path if cluster_ctx else None,
        "context_name": cluster_ctx.context_name if cluster_ctx else None,
        "user_id": user_id,
    }
    stream_task: asyncio.Task[None] | None = None

    async def pump(snapshot: list[dict], queue: asyncio.Queue) -> None:
        """Forward an active turn's events (replayed buffer, then live) to this socket."""
        try:
            for payload in snapshot:
                await websocket.send_json(payload)
            while True:
                payload = await queue.get()
                if payload is TURN_END:
                    break
                await websocket.send_json(payload)
        finally:
            await turn_runner.unsubscribe(session_id, queue)

    async def attach(send_resumed: bool) -> None:
        """Subscribe this socket to the session's active turn, if any."""
        nonlocal stream_task
        sub = await turn_runner.subscribe(session_id)
        if sub is None:
            return
        snapshot, queue = sub
        # On reconnect (client navigated back mid-turn) tell the UI to show the
        # streaming state before we replay the in-progress events.
        if send_resumed:
            await websocket.send_json({"type": "stream_resumed"})
        stream_task = asyncio.create_task(pump(snapshot, queue))

    async def receive_loop() -> None:
        nonlocal stream_task
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "stop":
                await turn_runner.cancel(session_id)
                logger.info("Stop requested", extra={"session_id": session_id})
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "message":
                content = data.get("content", "").strip()
                # Descriptors echoed from a prior /attachments upload (claude-cli only).
                raw_attachments = data.get("attachments")
                attachments = raw_attachments if isinstance(raw_attachments, list) else []
                # A turn may carry only files (no text) — accept it when attachments exist.
                if not content and not attachments:
                    await websocket.send_json({"type": "error", "content": "Empty message content"})
                    continue

                logger.info(
                    "Received message",
                    extra={
                        "session_id": session_id,
                        "message_length": len(content),
                        "attachments": len(attachments),
                    },
                )

                turn_kwargs: dict = {**base_kwargs, "user_message": content}
                # The @-file mechanism is claude-cli only; the API/Bedrock orchestrators
                # have no `attachments` parameter, so only pass it to the CLI one.
                if attachments and get_settings().ai_provider == "claude-cli":
                    turn_kwargs["attachments"] = attachments
                started = await turn_runner.start(session_id, get_orchestrator(), turn_kwargs)
                if not started:
                    await websocket.send_json(
                        {"type": "error", "content": "A response is already in progress."}
                    )
                    continue
                await attach(send_resumed=False)
            else:
                await websocket.send_json(
                    {"type": "error", "content": f"Unknown message type: {msg_type}"}
                )

    try:
        # If a turn is already running for this session, the client is reconnecting
        # (e.g. navigated back) — attach so it sees the in-progress work live.
        await attach(send_resumed=True)
        await receive_loop()

    except WebSocketDisconnect:
        logger.info(
            "Client disconnected",
            extra={"session_id": session_id},
        )

    except json.JSONDecodeError as e:
        logger.warning(f"JSON decode error in session {session_id}: {e}")
        try:
            await websocket.send_json({"type": "error", "content": "Invalid JSON format"})
        except Exception:
            pass

    except ValidationError as e:
        logger.warning(f"Validation error in session {session_id}: {e}")
        try:
            await websocket.send_json(
                {"type": "error", "content": f"Invalid request format: {str(e)}"}
            )
        except Exception:
            pass

    except Exception:
        logger.exception(f"WebSocket error in session {session_id}")
        try:
            await websocket.send_json(
                {"type": "error", "content": "An unexpected server error occurred"}
            )
            await websocket.close(code=1011)
        except Exception:
            pass

    finally:
        # Tear down only this socket's pump — NOT the turn, which keeps running
        # detached and will persist its result even with no client attached.
        if stream_task is not None:
            if not stream_task.done():
                stream_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await stream_task
        await disconnect(session_id, websocket)
