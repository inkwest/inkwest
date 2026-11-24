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
import contextvars
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core import metrics
from app.core.database import async_session_maker
from app.services.events import ChatEvent, error_event
from app.services.messages import save_session_event

logger = logging.getLogger(__name__)

# Pushed to each subscriber queue when the turn ends, so a pump loop knows to stop.
TURN_END = object()

# The abort marker is a one-line transcript summary; full error detail lives in the
# logs. A CLI RuntimeError can embed a stderr tail, so cap what gets persisted.
_ABORT_REASON_MAX = 500

Orchestrator = Callable[..., AsyncIterator[ChatEvent]]

# Per task: each turn runs in its own task, so the waiver cannot leak to the next turn.
_no_abort_marker: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "inkwest_turn_no_abort_marker", default=False
)


def skip_abort_marker() -> None:
    """Waive the abort marker for the running turn: its error left the transcript as it was.

    Called from inside the orchestrator, which the runner drives in its own task context.
    """
    _no_abort_marker.set(True)


@dataclass
class _Turn:
    session_id: str
    cancel_event: asyncio.Event
    task: asyncio.Task[None] | None = None
    # Every event emitted so far (serialized), so a late subscriber sees the whole turn.
    buffer: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[Any]] = field(default_factory=set)
    done: bool = False


async def _persist_abort_marker(session_id: str, reason: str | None) -> None:
    """Leave a visible trace of WHY a turn ended abnormally.

    An errored or guard-killed turn keeps its partial content (the orchestrators
    persist it on their error paths), but the terminal error event itself was never
    stored — after a reload the turn ends on a dangling tool call with no cause, so
    viewers blame the last tool. Persisted as a ``turn aborted`` session event:
    UI-only by construction (never replayed to the model, excluded from message
    counts/search) and best-effort like every session event. Runs here rather than in
    each orchestrator because the runner is the one place every engine's terminal
    error passes through — streamed agent errors included — and only after the
    generator has fully unwound, so the marker row sorts after the partial content.
    """
    reason = (reason or "unknown error").strip()
    if len(reason) > _ABORT_REASON_MAX:
        reason = reason[: _ABORT_REASON_MAX - 1] + "…"
    try:
        async with async_session_maker() as db:
            await save_session_event(db, session_id, "turn", "aborted", [{"reason": reason}])
    except Exception:  # noqa: BLE001 — the marker must never mask the turn's outcome
        logger.warning("abort_marker_persist_failed", extra={"session_id": session_id})


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
        # `abort_reason` doubles as the persisted abort marker's text (see the finally).
        errored = False
        completed = False
        abort_reason: str | None = None
        try:
            # Own DB session: must outlive the request/WS that triggered the turn.
            async with async_session_maker() as db:
                async for event in orchestrate(db=db, cancel_event=turn.cancel_event, **kwargs):
                    if event.type == "error":
                        errored = True
                        abort_reason = event.content or abort_reason
                    elif event.type == "done":
                        completed = True
                    # Free the slot atomically with emitting the terminal `done` event
                    # (see _emit): a client that sends its next turn the instant it
                    # receives `done` — e.g. flushing a client-side message queue — would
                    # otherwise be rejected by the not-yet-run finally below.
                    await self._emit(turn, event.to_dict(), terminal=event.type == "done")
        except Exception:
            errored = True
            abort_reason = "An unexpected server error occurred"
            logger.exception("Turn failed", extra={"session_id": turn.session_id})
            await self._emit(turn, error_event("An unexpected server error occurred").to_dict())
        finally:
            if errored and not completed and not _no_abort_marker.get():
                # `not completed`: the CLI orchestrator can yield an error AFTER done (its
                # post-stream process.wait() cap) — a cleanup failure on a finished answer,
                # not an aborted turn. Placed BEFORE eviction/TURN_END so a client that
                # reloads on turn end already finds the marker row. The orchestrator has
                # fully unwound by here, so the marker sorts after the partial rows.
                await _persist_abort_marker(turn.session_id, abort_reason)
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

    async def _emit(self, turn: _Turn, payload: dict[str, Any], *, terminal: bool = False) -> None:
        async with self._lock:
            turn.buffer.append(payload)
            # Mark the turn done in the SAME lock hold that queues the terminal event, so
            # by the time any client receives `done` the slot is already free and its next
            # turn (e.g. a queued-message flush) won't hit "a response is already in
            # progress". The finally still evicts the turn + sends TURN_END; its `is turn`
            # guard won't clobber a newer turn that started in between.
            if terminal:
                turn.done = True
            subscribers = list(turn.subscribers)
        for queue in subscribers:
            queue.put_nowait(payload)


# Module-level singleton shared by all WS connections.
turn_runner = TurnRunner()
