"""
WebSocket endpoint streaming a conversation's squid egress log (live audit).

Each conversation's proxy writes its access log to the conversation's durable dir
(:func:`app.services.sandbox.squid_log_path`). This endpoint tails that file and
streams parsed entries, so the frontend can show — in real time — every host the
sandboxed agent reached, and every blocked attempt. This is the transparency that
makes running ``claude --dangerously-skip-permissions`` defensible.
"""

import asyncio
import contextlib
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.routes.chat import validate_session, validate_websocket_auth
from app.core.config import get_settings
from app.core.database import async_session_maker
from app.core.logging import get_logger
from app.services import sandbox

logger = get_logger(__name__)

router = APIRouter()

# How often the tailer wakes to read newly-appended lines (and to notice the log
# file once the sandbox has provisioned it on the conversation's first turn).
_POLL_INTERVAL_SECONDS = 0.5


def parse_squid_line(line: str) -> dict[str, Any] | None:
    """Parse one squid access.log line (default ``squid`` logformat) into an event.

    Layout: ``<ts> <dur> <client> <code>/<status> <bytes> <method> <url> …``. CONNECT
    (HTTPS) logs ``<url>`` as ``host:port``; plain HTTP (GET/POST) logs the full
    ``scheme://host[:port]/path`` — both are handled. Returns None for lines that don't
    match the expected shape.
    """
    parts = line.split()
    if len(parts) < 7:
        return None
    ts, _duration, client, code, size, method, url = parts[:7]
    decision, _, status = code.partition("/")  # e.g. TCP_TUNNEL/200 -> (TCP_TUNNEL, 200)
    if "://" in url:
        # Full URL (plain HTTP): take the host/port from the authority, not the scheme.
        split = urlsplit(url)
        host = split.hostname or url
        port = str(split.port) if split.port else ""
    else:
        # CONNECT form: host:port (port optional).
        host, sep, port = url.rpartition(":")
        if not sep:
            host, port = url, ""
    try:
        ts_value = float(ts)
    except ValueError:
        ts_value = 0.0
    return {
        "ts": ts_value,
        "decision": decision,
        "status": status,
        "method": method,
        "host": host,
        "port": port,
        "bytes": int(size) if size.isdigit() else 0,
        "client": client,
    }


async def _stream_log(websocket: WebSocket, path: str) -> None:
    """Tail ``path`` from the start, sending each parsed line until cancelled."""
    handle = None
    try:
        # The log appears once the conversation's proxy starts (first turn) — wait for it.
        while handle is None:
            try:
                handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
            except (FileNotFoundError, PermissionError):
                # Not provisioned yet, or the access log isn't readable for an instant
                # while the proxy starts — poll until it appears.
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        while True:
            line = handle.readline()
            if line:
                event = parse_squid_line(line.strip())
                if event:
                    await websocket.send_json({"type": "egress", **event})
                continue
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        if handle is not None:
            handle.close()


@router.websocket("/ws/egress/{session_id}")
async def websocket_egress(websocket: WebSocket, session_id: str) -> None:
    """Stream the conversation's live squid egress log to an authenticated owner."""
    settings = get_settings()
    if not settings.sandbox_enabled:
        await websocket.close(code=4003)  # feature disabled
        return

    async with async_session_maker() as db:
        user = await validate_websocket_auth(websocket, db)
        if user is None:
            await websocket.close(code=4001)  # unauthenticated
            return
        session, _cluster, _role, error = await validate_session(db, session_id, user)
        if error or session is None:
            await websocket.close(code=4004)  # not found / access denied
            return

    await websocket.accept(subprotocol="inkwest")

    stream_task = asyncio.create_task(_stream_log(websocket, sandbox.squid_log_path(session_id)))
    try:
        # Drain inbound frames (pings / close) so a client disconnect is noticed promptly
        # — the tailer would otherwise keep running until its next send fails.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("egress_ws_recv_ended", session_id=session_id)
    finally:
        stream_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream_task
