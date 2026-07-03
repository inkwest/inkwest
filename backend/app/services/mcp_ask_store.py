"""
In-process pending-ask registry for the MCP runtime permission gate (P6).

When the proxy resolves a tool call to the ``ask`` verdict, it parks the request on an
:class:`asyncio.Future` registered here, pushes a WS prompt to the asking user's sockets,
and waits (bounded) for the decision endpoint to resolve it. Same single-replica model as
``llm_proxy_token``: the proxy hold and the decision POST land in the SAME process, so a
plain dict touched only from the event loop suffices — no lock, no shared store.

The proxy (the waiter) owns each entry's lifecycle: it creates the ask and discards it in
its ``finally``. ``resolve`` only completes the Future — a decision arriving after the
waiter gave up (timeout cancels the Future) is reported as unresolvable, never an error.
"""

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# How long the proxy holds an ``ask`` tool call before denying. Locked decision: 60s → deny.
ASK_TIMEOUT_SECONDS = 60

# Memory backstop only. The proxy discards each ask in its ``finally``, so the store holds
# ~one entry per concurrently-held tool call; this cap bounds it if that invariant breaks.
_MAX_PENDING = 1_000


@dataclass(frozen=True)
class PendingAsk:
    """One held tool call awaiting a user decision."""

    ask_id: str
    user_id: str
    session_id: str
    server_key: str
    tool: str
    created_at: float  # time.monotonic()
    future: asyncio.Future[str]  # resolves to "allow" | "deny"


# ask_id -> pending ask. Event-loop-confined; see module docstring.
_asks: dict[str, PendingAsk] = {}


def _sweep(now: float) -> None:
    """Drop asks whose waiter is gone (Future done/cancelled) or that outlived the hold
    window twice over — backstop for a proxy task killed before its ``finally`` ran."""
    stale = [
        ask_id
        for ask_id, ask in _asks.items()
        if ask.future.done() or now - ask.created_at > 2 * ASK_TIMEOUT_SECONDS
    ]
    for ask_id in stale:
        del _asks[ask_id]


def create(*, user_id: str, session_id: str, server_key: str, tool: str) -> PendingAsk:
    """Register a new pending ask and return it (the caller awaits ``.future``)."""
    now = time.monotonic()
    _sweep(now)
    if len(_asks) >= _MAX_PENDING:
        # Evict the oldest so the store can never grow unbounded. Logged: at this size
        # something is leaking holds, and evicted waiters resolve to deny (fail-closed).
        overflow = len(_asks) - _MAX_PENDING + 1
        for ask_id in sorted(_asks, key=lambda a: _asks[a].created_at)[:overflow]:
            evicted = _asks.pop(ask_id)
            if not evicted.future.done():
                evicted.future.set_result("deny")
        logger.warning("mcp_ask_store_capped", extra={"evicted": overflow, "cap": _MAX_PENDING})
    ask = PendingAsk(
        ask_id=secrets.token_urlsafe(16),
        user_id=user_id,
        session_id=session_id,
        server_key=server_key,
        tool=tool,
        created_at=now,
        future=asyncio.get_running_loop().create_future(),
    )
    _asks[ask.ask_id] = ask
    return ask


def resolve(ask_id: str, verdict: str, *, user_id: str) -> bool:
    """Deliver a user decision. False when the ask is unknown, belongs to another user,
    or its waiter already gave up (timeout cancelled the Future) — the caller maps that
    to 404, it is not an error path."""
    ask = _asks.get(ask_id)
    if ask is None or ask.user_id != user_id or ask.future.done():
        return False
    ask.future.set_result(verdict)
    return True


def discard(ask_id: str) -> None:
    """Remove an ask once its waiter is done with it. Idempotent."""
    _asks.pop(ask_id, None)


def pending_for(*, user_id: str, session_id: str) -> list[PendingAsk]:
    """Live asks for one user's conversation — the reload-rehydrate read."""
    now = time.monotonic()
    return [
        ask
        for ask in _asks.values()
        if ask.user_id == user_id
        and ask.session_id == session_id
        and not ask.future.done()
        and now - ask.created_at <= ASK_TIMEOUT_SECONDS
    ]
