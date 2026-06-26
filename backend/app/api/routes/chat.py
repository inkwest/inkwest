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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_or_create_dev_user
from app.core import metrics
from app.core.auth import extract_ws_ticket, verify_ws_ticket
from app.core.config import get_settings
from app.core.database import async_session_maker
from app.models.cluster import Cluster
from app.models.session import Session
from app.models.session_share_link import SessionShareLink
from app.models.user import User
from app.services.ai_client import get_orchestrator
from app.services.budget_service import check_budget, is_budget_enabled
from app.services.mcp_dek_handshake import handle_dek_message, note_connect, note_disconnect
from app.services.messages import clear_retry_state, get_retryable_message
from app.services.session_access import get_session_role
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


@dataclass(frozen=True)
class Presence:
    user_id: str
    display_name: str


# Per-session presence roster: who is currently viewing, keyed by their socket. A
# user with two tabs maps to two entries but appears once in the broadcast roster.
# Drives the "who's here" avatars + typing indicators, fanned to all of a session's
# sockets independently of any active turn. Same single-replica scope as
# active_connections / turn_runner.
session_presence: dict[str, dict[WebSocket, Presence]] = defaultdict(dict)

# Anonymous share-link sockets, keyed by token, so revoking a link can kick its
# live guests (they also live in active_connections / session_presence by session).
# Single-replica scope (like active_connections / turn_runner): a revoke can only
# kick guests on THIS replica, so on a multi-replica deployment a guest pinned to
# another replica would keep streaming until they disconnect. Inkwest runs
# single-replica, so this holds today; revisit alongside the other in-memory state.
shared_link_connections: dict[str, set[WebSocket]] = defaultdict(set)

# Flat set of every anonymous share-link socket, so a session-wide broadcast can tell
# guests apart and project each frame down to the public-safe subset before sending.
# Mutated under _connections_lock alongside shared_link_connections.
guest_connections: set[WebSocket] = set()


def _sanitize_guest_broadcast(payload: dict) -> dict | None:
    """Project a session broadcast down to what an anonymous share-link guest may see.

    Turn events go through ``_sanitize_shared_event``; this guards the *broadcast* path
    (presence, typing, a peer's message echo), which otherwise leaks the real identities
    of authenticated participants to the public viewer. Returns the frame to forward, or
    ``None`` to drop it. The only broadcast a guest legitimately needs is the live
    question text — stripped of who authored it, mirroring the REST projection.
    """
    if payload.get("type") == "user_message":
        # Guest sees the question live, never the author (identity omitted by SharedMessage).
        return {
            "type": "user_message",
            "content": payload.get("content", ""),
            "attachments": payload.get("attachments", []),
        }
    # presence (roster names/ids), typing (who's typing), and anything else: withhold.
    return None


async def broadcast_to_session(
    session_id: str, payload: dict, *, exclude: WebSocket | None = None
) -> None:
    """Send a frame to every socket on a session (presence, typing, a peer's new
    message) — turn-independent. Best-effort per socket; a dead socket is skipped.

    Anonymous share-link guests receive a sanitized projection (``_sanitize_guest_broadcast``)
    so a session broadcast never leaks authenticated participants' identities to a public link."""
    async with _connections_lock:
        targets = [ws for ws in active_connections.get(session_id, set()) if ws is not exclude]
        guest_targets = [ws for ws in targets if ws in guest_connections]
    guest_payload = _sanitize_guest_broadcast(payload) if guest_targets else None
    for ws in targets:
        out = guest_payload if ws in guest_connections else payload
        if out is None:
            continue
        try:
            await ws.send_json(out)
        except Exception:
            pass


async def _broadcast_presence(session_id: str) -> None:
    """Fan the current (deduped) presence roster to all of a session's sockets."""
    async with _connections_lock:
        seen: dict[str, str] = {}
        for pres in session_presence.get(session_id, {}).values():
            seen[pres.user_id] = pres.display_name
        roster = [{"user_id": uid, "display_name": dn} for uid, dn in seen.items()]
    await broadcast_to_session(session_id, {"type": "presence", "users": roster})


async def _register_presence_raw(
    session_id: str, websocket: WebSocket, user_id: str, display_name: str
) -> None:
    async with _connections_lock:
        session_presence[session_id][websocket] = Presence(str(user_id), display_name)
    await _broadcast_presence(session_id)


async def _register_presence(session_id: str, websocket: WebSocket, user: User) -> None:
    await _register_presence_raw(session_id, websocket, str(user.id), user.display_name)


async def _unregister_presence(session_id: str, websocket: WebSocket) -> None:
    async with _connections_lock:
        present = session_presence.get(session_id)
        if present is not None:
            present.pop(websocket, None)
            if not present:
                del session_presence[session_id]
    await _broadcast_presence(session_id)


async def close_user_session_connections(session_id: str, user_id: str) -> None:
    """Close a specific user's sockets on a session — e.g. their share was revoked."""
    async with _connections_lock:
        present = session_presence.get(session_id, {})
        targets = [ws for ws, pres in present.items() if pres.user_id == str(user_id)]
    for ws in targets:
        try:
            await ws.close(code=4011)  # access revoked
        except Exception:
            pass


async def close_share_link_connections(token: str) -> None:
    """Close all anonymous viewers holding a share link — its token was revoked."""
    async with _connections_lock:
        targets = list(shared_link_connections.get(token, set()))
    for ws in targets:
        try:
            await ws.close(code=4012)  # share link revoked
        except Exception:
            pass


# Live turn events an anonymous share-link viewer may receive. Whitelist (not
# blacklist) so any newly added event type is withheld by default — the public
# viewer must never see the internal detail the unauthenticated REST projection
# (SharedMessage) deliberately omits: model reasoning (`thinking`/`reasoning`),
# per-turn token/cost/iteration metadata, or interactive/connector prompts. Only
# the answer text and the tool I/O the transcript already shows are forwarded.
_SHARED_VIEWER_EVENTS = frozenset({"text_delta", "tool_start", "tool_result", "done"})


def _sanitize_shared_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Project a live turn event down to what a public share-link viewer may see.

    Returns the (possibly trimmed) payload to forward, or ``None`` to drop it, keeping
    the live view aligned with the REST projection (see ``SharedMessage``). ``done`` is
    stripped to its lifecycle signal because its metadata carries token counts /
    iterations / latency that the authenticated viewer sees but the public one must not.
    """
    etype = payload.get("type")
    if etype not in _SHARED_VIEWER_EVENTS:
        return None
    if etype == "done":
        # Keep only `stopped` (drives the "stopped" badge); drop usage/cost metadata.
        if payload.get("metadata", {}).get("stopped"):
            return {"type": "done", "metadata": {"stopped": True}}
        return {"type": "done"}
    return payload


async def _pump_turn(
    websocket: WebSocket,
    session_id: str,
    snapshot: list[dict],
    queue: asyncio.Queue,
    sanitize: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> None:
    """Forward an active turn's events (replayed buffer, then live) to a socket.

    ``sanitize`` (set for anonymous share viewers) projects/drops each event before
    it leaves the server; authenticated sockets pass ``None`` and receive everything.
    """
    try:
        for payload in snapshot:
            out = sanitize(payload) if sanitize else payload
            if out is not None:
                await websocket.send_json(out)
        while True:
            payload = await queue.get()
            if payload is TURN_END:
                break
            out = sanitize(payload) if sanitize else payload
            if out is not None:
                await websocket.send_json(out)
    finally:
        await turn_runner.unsubscribe(session_id, queue)


async def _follow_turns(
    websocket: WebSocket,
    session_id: str,
    sanitize: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> None:
    """Stream the session's turns to a socket for its whole lifetime.

    Attaches to whichever turn is active and to each new one as it starts, pumping
    each to completion before waiting for the next. Unlike a one-shot attach, this
    lets a socket that connected BEFORE a turn began still receive it live — the
    fix for passive viewers (a read-only share viewer, or a participant watching
    someone else's turn), who never start a turn themselves. Cancelled on close.
    """
    while True:
        sub = await turn_runner.subscribe(session_id)
        if sub is None:
            # No active turn — block until the next one starts (or the socket closes,
            # which cancels this task). wait_for_turn returns at once if one is live.
            # Accepted edge: a turn that both starts AND finishes within the gap
            # between wait_for_turn returning and this subscribe() (a sub-millisecond
            # synchronous/empty turn) is missed live and only shows on reload. Real AI
            # turns stream for seconds, so the common case always catches the buffer.
            await turn_runner.wait_for_turn(session_id)
            continue
        snapshot, queue = sub
        # A turn is in progress (fresh or mid-flight): show streaming, then replay
        # the buffer and forward live events.
        await websocket.send_json({"type": "stream_resumed"})
        await _pump_turn(websocket, session_id, snapshot, queue, sanitize)


async def connect(session_id: str, websocket: WebSocket) -> None:
    """Register a WebSocket connection for a session."""
    await websocket.accept(subprotocol="inkwest")
    async with _connections_lock:
        active_connections[session_id].add(websocket)
        count = len(active_connections[session_id])
        active_total = sum(len(s) for s in active_connections.values())
    metrics.ws_connections_total.inc()
    # set() to the live total (not inc/dec) so the gauge can't drift on an unpaired path.
    metrics.ws_connections_active.set(active_total)
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
        active_total = sum(len(s) for s in active_connections.values())
    metrics.ws_connections_active.set(active_total)
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
        # Honor the ws-ticket if the client sent one (it always does), so dev-mode
        # user switching carries onto the socket; else the default dev user.
        header = websocket.headers.get("sec-websocket-protocol", "")
        token = extract_ws_ticket(header)
        if token:
            payload = verify_ws_ticket(token)
            sub = payload.get("sub") if payload else None
            if sub:
                result = await db.execute(select(User).where(User.id == sub))
                user = result.scalar_one_or_none()
                if user and user.is_active:
                    return user
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
) -> tuple[Session | None, Cluster | None, str | None, str | None]:
    """
    Validate that a session exists, has an active cluster, and that the user may
    access it. Returns ``(session, cluster, role, error)`` — ``role`` is the
    caller's effective role (owner | editor | viewer) so the socket can gate what
    a viewer may do; it is None when there is an error.
    """
    try:
        session = await db.get(Session, session_id)
        if not session:
            return None, None, None, "Session not found"

        # Access: owner, admin, or an accepted participant (see session_access).
        # No-auth dev mode (user is None) keeps full access as "owner".
        role: str | None = "owner"
        if user is not None:
            role = await get_session_role(session, user, db)
            if role is None:
                return None, None, None, "Access denied"

        # A session may have no cluster: start without a Kube context.
        if session.cluster_id is None:
            return session, None, role, None

        cluster = await db.get(Cluster, session.cluster_id)
        if not cluster:
            return None, None, None, "Cluster not found"

        if not cluster.is_active:
            return None, None, None, "Cluster is not active"

        return session, cluster, role, None

    except Exception as e:
        logger.exception(f"Error validating session {session_id}")
        return None, None, None, f"Error validating session: {str(e)}"


async def close_session_connections(session_id: str) -> None:
    """Close all active WS connections for a session, triggering orchestrator shutdown."""
    async with _connections_lock:
        connections = list(active_connections.get(session_id, set()))
    for ws in connections:
        try:
            await ws.close(code=4010)
        except Exception:
            pass


async def _budget_block_reason(user_id: UUID | None, user_roles: list[str]) -> str | None:
    """User-facing message if the user's spend budget bars a new turn, else None.

    No-op (no DB hit) while enforcement is disabled — the default. Fails open: a
    budget-check fault must never wedge chat, so a transient DB error allows the turn.
    """
    if not is_budget_enabled():
        return None
    try:
        async with async_session_maker() as db:
            status = await check_budget(db, user_id, user_roles)
    except Exception:
        logger.warning("Budget check failed; allowing turn", exc_info=True)
        return None
    if not status.is_allowed and status.hard_limit_action == "block":
        return (
            "You've reached your usage budget for this period. "
            "Please contact an administrator to raise your limit."
        )
    return None


@router.websocket("/ws/chat/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str) -> None:
    """
    WebSocket endpoint for chat.

    Client sends:
        {"type": "message", "content": "..."} - Send a chat message
        {"type": "retry"} - Re-run the latest interrupted turn
        {"type": "stop"} - Cancel the active turn
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
    user_display_name = ""
    user_roles: list[str] = []
    session_role = "owner"  # owner | editor | viewer — gates what this socket may do
    async with async_session_maker() as db:
        user = await validate_websocket_auth(websocket, db)
        if not user:
            await websocket.close(code=4001)
            return

        user_id = user.id
        user_display_name = user.display_name
        # Captured now (used per turn for budget checks): the user object is expired
        # and detached once this session commits/closes.
        user_roles = list(user.roles or [])
        await db.commit()

        session, cluster, role, error = await validate_session(db, session_id, user)

        if error:
            await websocket.close(code=4004)
            return
        session_role = role or "owner"

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
    # survives the client navigating away (and still persists its result). A single
    # background task FOLLOWS the session's turns — attaching to whichever turn is
    # active and to each new one as it begins — so this socket streams turns it did
    # not itself start (another participant's turn, or, for a read-only viewer, the
    # owner's). See _follow_turns.
    base_kwargs = {
        "session_id": session_id,
        "cluster_name": cluster_ctx.display_name if cluster_ctx else None,
        "kubeconfig_path": cluster_ctx.kubeconfig_path if cluster_ctx else None,
        "context_name": cluster_ctx.context_name if cluster_ctx else None,
        "user_id": user_id,
    }

    async def receive_loop() -> None:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            # Viewers watch only: reject anything that drives or interrupts the turn.
            if session_role == "viewer" and msg_type in ("message", "retry", "stop"):
                await websocket.send_json(
                    {"type": "error", "content": "You have view-only access to this conversation"}
                )
                continue

            if msg_type == "stop":
                await turn_runner.cancel(session_id)
                logger.info("Stop requested", extra={"session_id": session_id})
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "typing":
                # Ephemeral "X is typing…" to the other participants (not the sender).
                await broadcast_to_session(
                    session_id,
                    {
                        "type": "typing",
                        "user_id": str(user_id),
                        "display_name": user_display_name,
                        "state": bool(data.get("state")),
                    },
                    exclude=websocket,
                )
            elif msg_type is not None and msg_type.startswith("mcp_dek_"):
                # Browser-fractioned DEK handshake (see app.services.mcp_dek_handshake):
                # supplies/derives the per-user key that seals MCP OAuth token blobs.
                reply = await handle_dek_message(user_id, data)
                if reply is not None:
                    await websocket.send_json(reply)
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
                # Attachments need the per-conversation sandbox /workdir, which only exists
                # under the claude-cli provider. Both sandbox engines consume them there:
                # claude-cli via @/workdir/<file> refs, inkwest-agent via the TurnSpec
                # (image files → Converse image blocks, P1.8). The pure API/Bedrock
                # orchestrators have no /workdir and no `attachments` parameter.
                if attachments and get_settings().ai_provider == "claude-cli":
                    turn_kwargs["attachments"] = attachments
                block_reason = await _budget_block_reason(user_id, user_roles)
                if block_reason:
                    await websocket.send_json({"type": "error", "content": block_reason})
                    continue
                started = await turn_runner.start(session_id, get_orchestrator(), turn_kwargs)
                if not started:
                    await websocket.send_json(
                        {"type": "error", "content": "A response is already in progress."}
                    )
                    continue
                # Show the question to the other participants in real time (the sender
                # already rendered it optimistically). On reload it comes from history
                # with the same author_user_id, so the two stay consistent.
                await broadcast_to_session(
                    session_id,
                    {
                        "type": "user_message",
                        "content": content,
                        "attachments": attachments,
                        "author_user_id": str(user_id),
                        "author_display_name": user_display_name,
                    },
                    exclude=websocket,
                )
            elif msg_type == "retry":
                # Re-run the latest interrupted turn. The errored user message is
                # already in history, so we replay it in place
                # (persist_user_message=False) rather than appending a duplicate.
                # The DB read runs inline here (not in the turn task); guard it so a
                # transient DB error surfaces as an error event instead of tearing
                # down the socket.
                try:
                    async with async_session_maker() as db:
                        content = await get_retryable_message(db, session_id)
                except Exception:
                    logger.exception("Retry lookup failed", extra={"session_id": session_id})
                    await websocket.send_json(
                        {"type": "error", "content": "Could not start retry, please try again."}
                    )
                    continue
                if content is None:
                    await websocket.send_json({"type": "error", "content": "Nothing to retry"})
                    continue

                logger.info("Retry requested", extra={"session_id": session_id})

                block_reason = await _budget_block_reason(user_id, user_roles)
                if block_reason:
                    await websocket.send_json({"type": "error", "content": block_reason})
                    continue

                started = await turn_runner.start(
                    session_id,
                    get_orchestrator(),
                    {**base_kwargs, "user_message": content, "persist_user_message": False},
                )
                if not started:
                    # Turn already running: leave the message retryable so the user
                    # can retry once it frees up — do NOT clear the flag here.
                    await websocket.send_json(
                        {"type": "error", "content": "A response is already in progress."}
                    )
                    continue
                # Turn started: now safe to clear the error flag (best-effort — a
                # failure here only leaves a stale error bubble on reload, not lost
                # work, since the turn is already running).
                try:
                    async with async_session_maker() as db:
                        await clear_retry_state(db, session_id)
                except Exception:
                    logger.exception("Clear retry state failed", extra={"session_id": session_id})
            else:
                await websocket.send_json(
                    {"type": "error", "content": f"Unknown message type: {msg_type}"}
                )

    # Register presence + start the turn-follow loop INSIDE the try, so any failure
    # here still reaches the finally and releases the connection/presence/DEK we took.
    # Follow streams the active turn plus every later one (see _follow_turns), so a
    # passive viewer sees turns it did not start; it runs alongside the receive loop.
    follow_task: asyncio.Task[None] | None = None
    dek_held = False
    try:
        await note_connect(user_id)
        dek_held = True
        # Announce arrival to everyone already in this conversation (and vice versa).
        await _register_presence(session_id, websocket, user)
        follow_task = asyncio.create_task(_follow_turns(websocket, session_id))
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
        # Stop following turns for this socket — the turns keep running detached and
        # still persist their result even with no client attached.
        if follow_task is not None:
            follow_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await follow_task
        await disconnect(session_id, websocket)
        # Tell the remaining participants this user's tab left (updates the roster).
        await _unregister_presence(session_id, websocket)
        # Drop the in-RAM DEK once the user's last socket closes (connected-only
        # custody) — only if we actually took it (note_connect ran).
        if dek_held:
            await note_disconnect(user_id)


@router.websocket("/ws/shared/{token}")
async def websocket_shared(websocket: WebSocket, token: str) -> None:
    """Anonymous, read-only live view of a conversation via a share-link token.

    No OIDC — the token is the credential. The guest joins the session's live event
    stream (turn deltas, a peer 'user_message' echo, presence) but can only ping;
    every other inbound frame is ignored. Revoking the link closes this socket.
    """
    async with async_session_maker() as db:
        link = await db.scalar(
            select(SessionShareLink).where(
                SessionShareLink.token == token,
                SessionShareLink.is_active == True,  # noqa: E712
            )
        )
        if link is None:
            await websocket.close(code=4004)  # invalid / revoked
            return
        session_id = str(link.session_id)
        session = await db.get(Session, session_id)
        if session is None:
            await websocket.close(code=4004)
            return

    await connect(session_id, websocket)
    # Anonymous guests show in the roster as "Guest"; a per-socket id keeps each
    # guest a distinct chip (and is never tied to a real account).
    guest_id = f"guest:{id(websocket)}"

    # Register the token/presence entries + start the follow loop INSIDE the try, so a
    # failure still reaches the finally and frees them. Follow streams the active turn
    # plus every later one so a guest sees the AI answer live; the loop honors pings only.
    follow_task: asyncio.Task[None] | None = None
    try:
        # Flag as a guest BEFORE registering presence: _register_presence_raw broadcasts
        # the roster to every socket incl. this one, and the guest flag is what makes that
        # frame get dropped for the guest. Set it first or the first presence leaks.
        async with _connections_lock:
            shared_link_connections[token].add(websocket)
            guest_connections.add(websocket)
        await _register_presence_raw(session_id, websocket, guest_id, "Guest")
        # Tell the guest its own presence id so the client can filter itself out of the
        # "who's viewing" roster (it has no authenticated user id to match on).
        await websocket.send_json({"type": "presence_self", "user_id": guest_id})
        # Anonymous viewer: sanitize live events to the public-safe subset (see
        # _sanitize_shared_event) so the stream never leaks what the REST view omits.
        follow_task = asyncio.create_task(
            _follow_turns(websocket, session_id, _sanitize_shared_event)
        )
        while True:
            data = await websocket.receive_json()
            # Read-only: keepalive only; ignore anything a malicious client might send.
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info("Shared viewer disconnected", extra={"session_id": session_id})
    except (json.JSONDecodeError, ValidationError):
        pass
    except Exception:
        logger.exception(f"Shared WebSocket error in session {session_id}")
    finally:
        if follow_task is not None:
            follow_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await follow_task
        async with _connections_lock:
            guest_connections.discard(websocket)
            conns = shared_link_connections.get(token)
            if conns is not None:
                conns.discard(websocket)
                if not conns:
                    del shared_link_connections[token]
        await disconnect(session_id, websocket)
        await _unregister_presence(session_id, websocket)
