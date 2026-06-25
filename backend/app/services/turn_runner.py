"""
Decouple chat orchestration from the WebSocket connection.

Previously the WS route drove the orchestrator inline (`await ws.send_json(event)`
inside `async for event in orchestrate(...)`), so the generator only advanced
while a client stayed connected. A client navigating to another session closes
the socket, which tore the generator down *before* it persisted the assistant
message — losing the in-flight turn entirely (the user message was already
committed, so you'd come back to a question with no answer).

This runner spawns each turn as a background ``asyncio.Task`` with its OWN DB
session, buffers every emitted event, and fans them out to subscribed sockets.
The task runs to natural completion (and persists) regardless of whether any
client is listening; a client that reconnects mid-turn replays the buffer to
see the in-progress work. Only an explicit user ``stop`` (or session deletion)
cancels it, via ``cancel_event``.

Scope: the registry is in-memory, like ``active_connections`` and the connector
pending-state. That assumes a single replica (Inkwest already runs single-replica
for the same reason). A multi-replica deployment would need to pin a session's
sockets to the replica running its turn, or move the buffer to shared storage.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core import metrics
from app.core.database import async_session_maker
from app.services.events import ChatEvent, error_event

logger = logging.getLogger(__name__)

# Pushed to each subscriber queue when the turn ends, so a pump loop knows to stop.
TURN_END = object()

Orchestrator = Callable[..., AsyncIterator[ChatEvent]]


@dataclass
class _Turn:
    session_id: str
    cancel_event: asyncio.Event
    task: asyncio.Task[None] | None = None
    # Every event emitted so far (serialized), so a late subscriber sees the whole turn.
    buffer: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[Any]] = field(default_factory=set)
    done: bool = False


class TurnRunner:
    """Per-session registry of background orchestration turns."""

    def __init__(self) -> None:
        self._turns: dict[str, _Turn] = {}
        # Guards _turns and each turn's buffer/subscribers so emit and subscribe
        # can't race (a subscriber must atomically get the buffer snapshot + the
        # live queue, with no event slipping through the gap).
        self._lock = asyncio.Lock()
        # Per-session futures resolved when a turn starts, so sockets that connected
        # before a turn began can wait for the next one and subscribe to it.
        self._turn_waiters: dict[str, list[asyncio.Future[None]]] = {}

    async def is_active(self, session_id: str) -> bool:
        async with self._lock:
            turn = self._turns.get(session_id)
            return turn is not None and not turn.done

    async def wait_for_turn(self, session_id: str) -> None:
        """Block until a turn is active for the session.

        Returns immediately when one is already running — this closes the
        lost-wakeup race against ``subscribe`` (a turn that starts between a
        caller's ``subscribe``→None and this call). Otherwise it resolves when the
        next turn starts. Lets a passive viewer follow turns that begin after it
        connected (see chat._follow_turns).
        """
        async with self._lock:
            existing = self._turns.get(session_id)
            if existing is not None and not existing.done:
                return
            fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._turn_waiters.setdefault(session_id, []).append(fut)
        try:
            await fut
        finally:
            # If the socket closed before any turn started (await cancelled), drop our
            # future so it can't accumulate on a session that never starts another turn
            # (a left-open shared link is the worst case). start() also pops the list.
            async with self._lock:
                waiters = self._turn_waiters.get(session_id)
                if waiters is not None:
                    try:
                        waiters.remove(fut)
                    except ValueError:
                        pass
                    if not waiters:
                        del self._turn_waiters[session_id]

    async def start(
        self,
        session_id: str,
        orchestrate: Orchestrator,
        orchestrate_kwargs: dict[str, Any],
    ) -> bool:
        """Start a turn unless one is already active for the session.

        Returns True if started, False if a turn is already in progress (one
        active turn per session — the UI disables sending while streaming).
        """
        async with self._lock:
            existing = self._turns.get(session_id)
            if existing is not None and not existing.done:
                return False
            turn = _Turn(session_id=session_id, cancel_event=asyncio.Event())
            self._turns[session_id] = turn
            # Wake sockets waiting for a turn so their follow loop subscribes to it.
            for fut in self._turn_waiters.pop(session_id, []):
                if not fut.done():
                    fut.set_result(None)
        turn.task = asyncio.create_task(self._run(turn, orchestrate, orchestrate_kwargs))
        return True

    async def subscribe(
        self, session_id: str
    ) -> tuple[list[dict[str, Any]], asyncio.Queue[Any]] | None:
        """Attach to an active turn: returns (events so far, live queue) or None."""
        async with self._lock:
            turn = self._turns.get(session_id)
            if turn is None or turn.done:
                return None
            # Accepted trade-off: the queue is unbounded (unlike pubsub's maxsize=64).
            # A turn's event count is bounded by the turn itself, and every event is
            # also kept in turn.buffer for replay, so a slow viewer can be dropped and
            # recover on reconnect rather than us dropping events mid-stream (which would
            # corrupt a live render). Revisit if turns ever stream unbounded output.
            queue: asyncio.Queue[Any] = asyncio.Queue()
            turn.subscribers.add(queue)
            return list(turn.buffer), queue

    async def unsubscribe(self, session_id: str, queue: asyncio.Queue[Any]) -> None:
        async with self._lock:
            turn = self._turns.get(session_id)
            if turn is not None:
                turn.subscribers.discard(queue)

    async def cancel(self, session_id: str) -> None:
        """Request cancellation of the session's active turn (explicit stop / delete)."""
        async with self._lock:
            turn = self._turns.get(session_id)
        if turn is not None:
            turn.cancel_event.set()

    async def _run(self, turn: _Turn, orchestrate: Orchestrator, kwargs: dict[str, Any]) -> None:
        # chat_turns_total is recorded in the dispatcher (labeled engine+model); here we only
        # track in-flight saturation, duration and errors (engine-agnostic).
        metrics.chat_turns_active.inc()
        started_at = time.monotonic()
        # Count both a raised exception and an emitted terminal error event (provider API
        # error, max-iterations) — the orchestrators surface those as events, not raises.
        errored = False
        try:
            # Own DB session: must outlive the request/WS that triggered the turn.
            async with async_session_maker() as db:
                async for event in orchestrate(db=db, cancel_event=turn.cancel_event, **kwargs):
                    if event.type == "error":
                        errored = True
                    await self._emit(turn, event.to_dict())
        except Exception:
            errored = True
            logger.exception("Turn failed", extra={"session_id": turn.session_id})
            await self._emit(turn, error_event("An unexpected server error occurred").to_dict())
        finally:
            # The dispatcher (same task) has set engine+model by now; default 'unknown' if a
            # turn somehow failed before dispatch.
            engine, model = metrics.turn_engine(), metrics.turn_model()
            metrics.chat_turns_active.dec()
            metrics.turn_duration_seconds.labels(engine=engine, model=model).observe(
                time.monotonic() - started_at
            )
            if errored:
                metrics.turn_errors_total.labels(engine=engine, model=model).inc()
            async with self._lock:
                turn.done = True
                subscribers = list(turn.subscribers)
                # Evict only if still the current turn (don't clobber a newer one).
                if self._turns.get(turn.session_id) is turn:
                    del self._turns[turn.session_id]
            for queue in subscribers:
                queue.put_nowait(TURN_END)

    async def _emit(self, turn: _Turn, payload: dict[str, Any]) -> None:
        async with self._lock:
            turn.buffer.append(payload)
            subscribers = list(turn.subscribers)
        for queue in subscribers:
            queue.put_nowait(payload)


# Module-level singleton shared by all WS connections.
turn_runner = TurnRunner()
