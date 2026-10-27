"""
Web Push of share invites to the invitee's browsers (spec 003).

Best effort: never retried, never raised to the caller. A missed push leaves the invite
pending, and the in-app popup recovers it on the next page load.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp
from py_vapid import Vapid
from pywebpush import WebPushException, webpush_async
from sqlalchemy import delete, select

from app.core import metrics
from app.core.config import get_settings
from app.core.database import async_session_maker
from app.core.logging import get_logger
from app.core.push_keys import is_push_service_url, load_vapid
from app.models.push_subscription import PushSubscription

logger = get_logger(__name__)

_TTL_SECONDS = 24 * 3600
_SEND_TIMEOUT_SECONDS = 10.0
# FCM answers 410 to some brand-new subscriptions for ~10 s: pruning those drops a live browser.
_FRESH_SUBSCRIPTION_GRACE = timedelta(seconds=60)
# Bounds sends and DB checkouts: 4 invites x 20 subscriptions per user stays under the
# connector's default 100 connections. The pending cap bounds task pileup behind it.
_INVITE_CONCURRENCY = asyncio.Semaphore(4)
_MAX_PENDING_INVITES = 256
# Strong refs: the loop holds tasks weakly, so an unreferenced send could be GC'd mid-flight.
_tasks: set[asyncio.Task[None]] = set()
_http: aiohttp.ClientSession | None = None
# Set at shutdown, cleared at startup: while set, no invite is scheduled, no client created.
_closed = False


class RedirectRefused(aiohttp.ClientError):
    """A push service answered with a redirect."""


async def _refuse_redirect(
    _session: aiohttp.ClientSession,
    _ctx: object,
    params: aiohttp.TraceRequestRedirectParams,
) -> None:
    # The allowlist vets only the first URL; following a redirect would re-POST anywhere.
    params.response.close()
    raise RedirectRefused()


def http_session() -> aiohttp.ClientSession:
    """The process-wide push client; the lifespan creates it when push is on, else first use."""
    global _http
    if _closed:
        raise RuntimeError("push client closed at shutdown")
    if _http is None or _http.closed:
        # pywebpush exposes no allow_redirects, so the refusal lives on the session.
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(_refuse_redirect)
        _http = aiohttp.ClientSession(trace_configs=[trace])
    return _http


def open_http_session(*, create_client: bool) -> None:
    """Startup: accept invites again; create the client now when push is on."""
    global _closed
    _closed = False
    if create_client:
        http_session()


async def close_http_session() -> None:
    """Shutdown: cancel in-flight invites (not counted as failed), then close the client."""
    global _http, _closed
    _closed = True
    if _tasks:
        # A send cut off by close() would read as a push outage (ServerDisconnectedError).
        logger.info("push_invites_cancelled", count=len(_tasks))
        for task in _tasks:
            task.cancel()
        await asyncio.gather(*_tasks, return_exceptions=True)
    if _http is not None:
        await _http.close()
        _http = None


def schedule_share_invite(
    *,
    user_id: UUID,
    share_id: UUID,
    session_id: UUID,
    session_title: str | None,
    invited_by: str | None,
) -> None:
    """Push the invite in the background so the HTTP response never waits on a push service."""
    if _closed or len(_tasks) >= _MAX_PENDING_INVITES:
        reason = "shutdown" if _closed else "backlog"
        logger.warning("push_invite_dropped", reason=reason, pending=len(_tasks))
        metrics.push_sent_total.labels(result="dropped").inc()
        return
    task = asyncio.create_task(
        send_share_invite(
            user_id=user_id,
            share_id=share_id,
            session_id=session_id,
            session_title=session_title,
            invited_by=invited_by,
        )
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def send_share_invite(
    *,
    user_id: UUID,
    share_id: UUID,
    session_id: UUID,
    session_title: str | None,
    invited_by: str | None,
) -> None:
    """Push ``share_invite`` to every subscription of ``user_id``.

    A 404/410 prunes the row unless it is younger than ``_FRESH_SUBSCRIPTION_GRACE``.
    """
    settings = get_settings()
    if not settings.push_enabled:
        return
    async with _INVITE_CONCURRENCY:
        try:
            signer = load_vapid(
                settings.vapid_private_key.get_secret_value(), settings.vapid_public_key
            )
            # Own sessions: the request's is closed by now, and none is held during sends.
            async with async_session_maker() as db:
                subs = (
                    await db.execute(
                        select(
                            PushSubscription.id,
                            PushSubscription.endpoint,
                            PushSubscription.p256dh,
                            PushSubscription.auth,
                            PushSubscription.created_at,
                        ).where(PushSubscription.user_id == user_id)
                    )
                ).all()
            if not subs:
                return
            # Raw UTF-8, compact: with 255-char title and name it stays under the 3993-byte
            # RFC 8291 plaintext cap even when every char is escaped (\u0001 = 6 bytes).
            data = json.dumps(
                {
                    "type": "share_invite",
                    "share_id": str(share_id),
                    "session_id": str(session_id),
                    "session_title": session_title,
                    "invited_by": invited_by,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            # RFC 8030 caps Topic at 32 chars; a re-invite replaces an undelivered push.
            headers = {"Urgency": "normal", "Topic": share_id.hex}
            fresh_since = datetime.now(UTC) - _FRESH_SUBSCRIPTION_GRACE
            results = await asyncio.gather(
                *(
                    _send(
                        signer,
                        settings.vapid_subject,
                        endpoint,
                        p256dh,
                        auth,
                        data,
                        headers,
                        # SQLite (tests) returns naive UTC; Postgres timestamptz is aware.
                        fresh=created.replace(tzinfo=created.tzinfo or UTC) > fresh_since,
                    )
                    for _, endpoint, p256dh, auth, created in subs
                )
            )
            gone = [sub.id for sub, res in zip(subs, results, strict=True) if res == "gone"]
            if gone:
                async with async_session_maker() as db:
                    await db.execute(delete(PushSubscription).where(PushSubscription.id.in_(gone)))
                    await db.commit()
        except Exception:
            logger.exception("push_share_invite_failed")


async def _send(
    signer: Vapid,
    subject: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    data: str,
    headers: dict[str, str],
    *,
    fresh: bool,
) -> str:
    """One push; returns the metric result. Logs carry the host only: the URL is a capability."""
    host = urlsplit(endpoint).hostname
    if not is_push_service_url(endpoint):
        logger.warning("push_endpoint_rejected", host=host)
        result = "rejected"
    else:
        try:
            await webpush_async(
                {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                data=data,
                vapid_private_key=signer,
                aiohttp_session=http_session(),
                # Fresh dict per send: pywebpush writes the endpoint's aud and exp into it.
                vapid_claims={"sub": subject},
                timeout=_SEND_TIMEOUT_SECONDS,
                ttl=_TTL_SECONDS,
                headers=headers,
            )
            result = "ok"
        except Exception as e:
            status = e.status_code if isinstance(e, WebPushException) else None
            if status in (404, 410) and not fresh:
                result = "gone"
            else:
                # Never str(e): a WebPushException embeds the push service's response body.
                logger.warning("push_send_failed", host=host, status=status, error=type(e).__name__)
                result = "failed"
    metrics.push_sent_total.labels(result=result).inc()
    return result
