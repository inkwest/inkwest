"""Web Push phase 2 (spec 003): the share-invite sender, its host allowlist and its crypto.

Subscriptions live in a real in-memory SQLite engine patched in as the sender's own session
maker, so the 404/410 prune runs as SQL. Every key below is generated, never hand-written.
"""

import asyncio
import base64
import gc
import json
import os
import uuid
import warnings
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import http_ece
import jwt
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from prometheus_client import REGISTRY
from py_vapid import Vapid
from pydantic import SecretStr
from pywebpush import WebPushException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

from app.core.config import get_settings, validate_settings
from app.models.base import Base
from app.models.push_subscription import PushSubscription
from app.models.session import Session
from app.models.user import User
from app.services import web_push

_SUBJECT = "mailto:ops@example.com"
_INVITEE = uuid.UUID("00000000-0000-4000-8000-0000000000c3")
_SHARE_ID = uuid.UUID("12345678-1234-4234-8234-123456789abc")
_SESSION_ID = uuid.UUID("00000000-0000-4000-8000-0000000000d4")
_TITLE = "Investigating crashes"
_INVITER = "Owner Person"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _vapid_pair() -> tuple[str, str]:
    """(raw private, uncompressed public) base64url, as `web-push generate-vapid-keys` prints."""
    key = ec.generate_private_key(ec.SECP256R1())
    return _b64url(key.private_numbers().private_value.to_bytes(32, "big")), _b64url(_point(key))


_VAPID_PRIV, _VAPID_PUB = _vapid_pair()


def _endpoint(host: str = "fcm.googleapis.com") -> str:
    return f"https://{host}/fcm/send/{uuid.uuid4().hex}"


class _Browser:
    """A subscriber with a real RFC 8291 keypair and auth secret."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)

    def row(self) -> PushSubscription:
        return PushSubscription(
            user_id=_INVITEE,
            endpoint=self.endpoint,
            p256dh=_b64url(_point(self.key)),
            auth=_b64url(self.auth),
        )


@pytest.fixture
def push_on(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", _VAPID_PUB)
    monkeypatch.setattr(settings, "vapid_private_key", SecretStr(_VAPID_PRIV))
    monkeypatch.setattr(settings, "vapid_subject", _SUBJECT)


@pytest.fixture
def push_off(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "")
    monkeypatch.setattr(settings, "vapid_private_key", SecretStr(""))
    monkeypatch.setattr(settings, "vapid_subject", "")


@pytest_asyncio.fixture(autouse=True)
async def _close_shared_client() -> AsyncIterator[None]:
    """The process-wide push client binds to this test's loop: close it with that loop."""
    web_push.open_http_session(create_client=False)
    yield
    await web_push.close_http_session()


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[PushSubscription.__table__])
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    with patch.object(web_push, "async_session_maker", session_maker):
        yield session_maker
    await engine.dispose()


async def _seed(maker: async_sessionmaker[AsyncSession], *browsers: _Browser) -> None:
    async with maker() as db:
        db.add_all([b.row() for b in browsers])
        await db.commit()


async def _endpoints(maker: async_sessionmaker[AsyncSession]) -> set[str]:
    async with maker() as db:
        return set((await db.scalars(select(PushSubscription.endpoint))).all())


async def _send() -> None:
    await web_push.send_share_invite(
        user_id=_INVITEE,
        share_id=_SHARE_ID,
        session_id=_SESSION_ID,
        session_title=_TITLE,
        invited_by=_INVITER,
    )


def _count(result: str) -> float:
    return REGISTRY.get_sample_value("inkwest_push_sent_total", {"result": result}) or 0.0


def _snapshot() -> dict[str, float]:
    return {r: _count(r) for r in ("ok", "gone", "failed", "rejected")}


def _deltas(before: dict[str, float]) -> dict[str, float]:
    return {r: _count(r) - v for r, v in before.items()}


def _path(endpoint: str) -> str:
    """The capability part of an endpoint (everything after the host)."""
    return endpoint.split("/", 3)[3]


def _push_error(status: int, body: str = "") -> WebPushException:
    """The exception webpush_async raises on a >202 answer (aiohttp response: ``.status``)."""
    return WebPushException(
        f"Push failed: {status}\nResponse body:{body}", SimpleNamespace(status=status)
    )


# ---- AC2: VAPID private key must derive the public key ------------------------ //


def _validate(private: str, public: str = _VAPID_PUB, subject: str = _SUBJECT) -> None:
    settings = MagicMock(
        app_env="development",
        connectors_enabled=False,
        aws_sso_role_filter=None,
        aws_region="us-east-1",
        llm_broker_enabled=False,
        vapid_public_key=public,
        vapid_private_key=SecretStr(private),
        vapid_subject=subject,
    )
    with patch("app.core.config.get_settings", return_value=settings):
        validate_settings()


def _der_b64url(key: ec.EllipticCurvePrivateKey) -> str:
    der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return _b64url(der)


def test_validate_accepts_matching_pair_raw_and_der():
    """003-AC2: both formats py_vapid reads (raw 32-byte and DER) boot when they derive the pair."""
    _validate(_VAPID_PRIV)
    key = ec.generate_private_key(ec.SECP256R1())
    _validate(_der_b64url(key), public=_b64url(_point(key)))


@pytest.mark.parametrize(
    "private",
    [
        _vapid_pair()[0],  # valid raw key of another pair
        _der_b64url(ec.generate_private_key(ec.SECP256R1())),  # valid DER key of another pair
    ],
)
def test_validate_rejects_mismatched_private_key(private: str):
    """003-AC2: a private key that does not derive VAPID_PUBLIC_KEY refuses to boot."""
    with pytest.raises(ValueError, match="VAPID_PRIVATE_KEY does not derive VAPID_PUBLIC_KEY") as e:
        _validate(private)
    assert private not in str(e.value)


@pytest.mark.parametrize(
    "private",
    [
        "not-a-key",
        _b64url(b"\x00" * 32),  # raw length, scalar 0 is not a valid P-256 key
        _b64url(b"\x30\x03\x02\x01\x01"),  # truncated DER
        _der_b64url(ec.generate_private_key(ec.SECP384R1())),  # wrong curve
        "!!!!",
    ],
)
def test_validate_rejects_unparsable_private_key(private: str):
    """003-AC2: an unparsable VAPID_PRIVATE_KEY refuses to boot, without echoing the key."""
    with pytest.raises(ValueError, match="^VAPID_PRIVATE_KEY ") as e:
        _validate(private)
    assert private not in str(e.value)
    # No chained parser error either: its message could quote the input.
    assert e.value.__cause__ is None
    assert e.value.__context__ is None or e.value.__suppress_context__


@pytest.mark.parametrize(
    "subject",
    ["https://example.com/contact", "mailto:ops", "mailto:@example.com", "https://intranet"],
)
def test_validate_rejects_subject_outside_ac2_shape(subject: str):
    """003-AC2: path, missing user/domain or single-label host refuses to boot."""
    with pytest.raises(ValueError, match="VAPID_SUBJECT must be"):
        _validate(_VAPID_PRIV, subject=subject)


# ---- AC6: fan-out with the share_invite payload ------------------------------- //


@pytest.mark.asyncio
async def test_sends_one_push_per_subscription(maker, push_on):
    """003-AC6: two subscriptions -> two sends, each with the share_invite payload + headers."""
    a, b = _Browser(_endpoint()), _Browser(_endpoint("updates.push.services.mozilla.com"))
    await _seed(maker, a, b)
    before = _snapshot()
    with patch.object(web_push, "webpush_async", new=AsyncMock()) as send:
        await _send()

    assert send.await_count == 2
    assert {c.args[0]["endpoint"] for c in send.await_args_list} == {a.endpoint, b.endpoint}
    for c in send.await_args_list:
        kw = c.kwargs
        assert json.loads(kw["data"]) == {
            "type": "share_invite",
            "share_id": str(_SHARE_ID),
            "session_id": str(_SESSION_ID),
            "session_title": _TITLE,
            "invited_by": _INVITER,
        }
        assert kw["headers"] == {"Urgency": "normal", "Topic": _SHARE_ID.hex}
        assert len(kw["headers"]["Topic"]) == 32
        assert kw["ttl"] == 24 * 3600
        assert kw["timeout"] == 10.0
        assert kw["vapid_claims"] == {"sub": _SUBJECT}
        assert isinstance(kw["vapid_private_key"], Vapid)
        assert kw["aiohttp_session"] is web_push.http_session()
    # pywebpush writes aud into the claims dict: each send must get its own.
    claims = [c.kwargs["vapid_claims"] for c in send.await_args_list]
    assert claims[0] is not claims[1]
    assert _deltas(before)["ok"] == 2


@pytest.mark.asyncio
async def test_no_subscription_sends_nothing(maker, push_on):
    """003-AC6: an invitee with no subscription costs one query and no send."""
    with patch.object(web_push, "webpush_async", new=AsyncMock()) as send:
        await _send()
    send.assert_not_awaited()


# ---- AC7: 404/410 prune ------------------------------------------------------- //


@pytest.mark.asyncio
async def test_gone_subscriptions_pruned_others_kept(maker, push_on):
    """003-AC7: 410 and 404 delete their rows; the third subscription is kept and sent."""
    gone410, gone404, alive = (_Browser(_endpoint()) for _ in range(3))
    await _seed(maker, gone410, gone404, alive)
    answers = {gone410.endpoint: _push_error(410), gone404.endpoint: _push_error(404)}

    async def fake(info: dict[str, Any], **_: Any) -> None:
        if info["endpoint"] in answers:
            raise answers[info["endpoint"]]

    before = _snapshot()
    with patch.object(web_push, "webpush_async", new=AsyncMock(side_effect=fake)) as send:
        await _send()

    assert send.await_count == 3
    assert await _endpoints(maker) == {alive.endpoint}
    assert _deltas(before) == {"ok": 1, "gone": 2, "failed": 0, "rejected": 0}


# ---- AC8: failures are logged without secrets, counted, rows kept ------------- //


@pytest.mark.asyncio
async def test_timeout_and_5xx_logged_counted_rows_kept(maker, push_on):
    """003-AC8: timeout + 5xx -> rows kept, metric counts, logs carry host + status/class only."""
    slow, broken = _Browser(_endpoint()), _Browser(_endpoint("web.push.apple.com"))
    await _seed(maker, slow, broken)
    # A push service may echo the request in its error body; it must not reach the log.
    answers = {
        slow.endpoint: aiohttp.ServerTimeoutError("Timeout on reading data from socket"),
        broken.endpoint: _push_error(503, body=f"{broken.endpoint} {_TITLE}"),
    }

    async def fake(info: dict[str, Any], **_: Any) -> None:
        raise answers[info["endpoint"]]

    before = _snapshot()
    with (
        patch.object(web_push, "webpush_async", new=AsyncMock(side_effect=fake)),
        capture_logs() as logs,
    ):
        await _send()  # never raises

    assert await _endpoints(maker) == {slow.endpoint, broken.endpoint}
    assert _deltas(before) == {"ok": 0, "gone": 0, "failed": 2, "rejected": 0}
    failed = sorted((e for e in logs if e["event"] == "push_send_failed"), key=lambda e: e["host"])
    assert [(e["host"], e["status"], e["error"]) for e in failed] == [
        ("fcm.googleapis.com", None, "ServerTimeoutError"),
        ("web.push.apple.com", 503, "WebPushException"),
    ]
    dumped = json.dumps(logs, default=str)
    for secret in (
        _path(slow.endpoint),
        _path(broken.endpoint),
        slow.row().p256dh,
        slow.row().auth,
        _TITLE,
        _INVITER,
        str(_SHARE_ID),
    ):
        assert secret not in dumped


@pytest.mark.asyncio
async def test_unexpected_error_never_raises(maker, push_on):
    """003-AC8: even a failure outside the per-send handling stays inside the sender."""
    await _seed(maker, _Browser(_endpoint()))
    with (
        patch.object(web_push, "load_vapid", side_effect=RuntimeError("boom")),
        capture_logs() as logs,
    ):
        await _send()
    assert [e["event"] for e in logs] == ["push_share_invite_failed"]


# ---- AC9: feature off --------------------------------------------------------- //


@pytest.mark.asyncio
async def test_push_off_sends_nothing(push_off):
    """003-AC9: no VAPID keys -> no DB read, no send, no error (not even a swallowed one)."""
    with (
        patch.object(web_push, "async_session_maker") as db,
        patch.object(web_push, "webpush_async", new=AsyncMock()) as send,
        capture_logs() as logs,
    ):
        await _send()
    db.assert_not_called()
    send.assert_not_awaited()
    assert logs == []


# ---- AC15: a stored row outside the allowlist is never sent to ---------------- //


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    ["example.com", "push.apple.com.evil.com", "xfcm.googleapis.com", "169.254.169.254"],
)
async def test_stored_row_with_foreign_host_skipped(maker, push_on, host: str):
    """003-AC15: a pre-allowlist row is skipped at send time, logged by host, counted."""
    bad, good = _Browser(f"https://{host}/x/{uuid.uuid4().hex}"), _Browser(_endpoint())
    await _seed(maker, bad, good)
    before = _snapshot()
    with (
        patch.object(web_push, "webpush_async", new=AsyncMock()) as send,
        capture_logs() as logs,
    ):
        await _send()
    assert [c.args[0]["endpoint"] for c in send.await_args_list] == [good.endpoint]
    assert [(e["event"], e["host"]) for e in logs] == [("push_endpoint_rejected", host)]
    assert _path(bad.endpoint) not in json.dumps(logs)
    assert _deltas(before) == {"ok": 1, "gone": 0, "failed": 0, "rejected": 1}
    assert await _endpoints(maker) == {bad.endpoint, good.endpoint}


# ---- Real pywebpush: RFC 8291 encryption + RFC 8292 VAPID, no network ---------- //


class _Wire:
    """Stands in for aiohttp's POST; records what pywebpush actually put on the wire."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls: list[dict[str, Any]] = []

    # Patched onto the class as a bound method of this object: no ``ClientSession`` self.
    async def post(self, url: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.append({"url": url, **kwargs})

        async def text() -> str:
            return ""

        return SimpleNamespace(status=self.status, reason="", text=text)


@pytest.mark.asyncio
async def test_real_request_is_encrypted_and_vapid_signed(maker, push_on):
    """003-AC6: pywebpush's real request decrypts to the payload and carries a valid VAPID JWT."""
    browser = _Browser(_endpoint())
    await _seed(maker, browser)
    wire = _Wire(status=201)
    with patch.object(aiohttp.ClientSession, "post", new=wire.post):
        await _send()

    [call] = wire.calls
    assert call["url"] == browser.endpoint
    assert call["timeout"] == 10.0
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["ttl"] == str(24 * 3600)
    assert headers["urgency"] == "normal"
    assert headers["topic"] == _SHARE_ID.hex

    scheme, params = headers["authorization"].split(" ", 1)
    assert scheme == "vapid"
    fields = dict(p.strip().split("=", 1) for p in params.split(","))
    assert fields["k"] == _VAPID_PUB
    vapid_public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), base64.urlsafe_b64decode(_VAPID_PUB + "=")
    )
    claims = jwt.decode(
        fields["t"], vapid_public, algorithms=["ES256"], audience="https://fcm.googleapis.com"
    )
    assert claims["sub"] == _SUBJECT

    plaintext = http_ece.decrypt(
        call["data"], private_key=browser.key, auth_secret=browser.auth, version="aes128gcm"
    )
    assert json.loads(plaintext) == {
        "type": "share_invite",
        "share_id": str(_SHARE_ID),
        "session_id": str(_SESSION_ID),
        "session_title": _TITLE,
        "invited_by": _INVITER,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "kept"), [(410, False), (404, False), (500, True), (429, True)])
async def test_real_status_mapping(maker, push_on, status: int, kept: bool):
    """003-AC7/AC8: the status the real pywebpush surfaces prunes on 404/410 only."""
    browser = _Browser(_endpoint())
    await _seed(maker, browser)
    with patch.object(aiohttp.ClientSession, "post", new=_Wire(status=status).post):
        await _send()
    assert (await _endpoints(maker) == {browser.endpoint}) is kept


# ---- Redirects: the allowlist vets the first URL only, so none is followed ---- //


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [307, 308, 302])
async def test_redirect_refused_not_followed(maker, push_on, monkeypatch, status: int):
    """003-AC15: a real push service answering a redirect gets no follow-up request; send fails."""
    hits: dict[str, list[str]] = {"push": [], "target": []}

    async def target(request: web.Request) -> web.Response:
        hits["target"].append(request.method)
        return web.Response(status=201)

    target_app = web.Application()
    target_app.router.add_route("*", "/{tail:.*}", target)
    async with TestServer(target_app) as target_server:

        async def push_service(request: web.Request) -> web.Response:
            hits["push"].append(request.method)
            location = str(target_server.make_url("/internal"))
            return web.Response(status=status, headers={"Location": location})

        push_app = web.Application()
        push_app.router.add_route("*", "/{tail:.*}", push_service)
        async with TestServer(push_app) as push_server:
            # Seam: a local plain-http server stands in for an allowlisted push service.
            monkeypatch.setattr(web_push, "is_push_service_url", lambda _url: True)
            browser = _Browser(str(push_server.make_url(f"/push/{uuid.uuid4().hex}")))
            await _seed(maker, browser)
            before = _snapshot()
            with capture_logs() as logs:
                await _send()

    assert hits == {"push": ["POST"], "target": []}
    assert _deltas(before) == {"ok": 0, "gone": 0, "failed": 1, "rejected": 0}
    assert [(e["event"], e["error"]) for e in logs] == [("push_send_failed", "RedirectRefused")]
    assert await _endpoints(maker) == {browser.endpoint}


# ---- Load bounds: concurrent invites, pending backlog ------------------------- //


_INVITE = {
    "user_id": _INVITEE,
    "share_id": _SHARE_ID,
    "session_id": _SESSION_ID,
    "session_title": _TITLE,
    "invited_by": _INVITER,
}


@pytest.mark.asyncio
async def test_burst_bounds_concurrent_sends_and_db_sessions(maker, push_on):
    """A burst of invites never runs more sends or DB sessions than the invite bound."""
    bound = 2
    live = {"send": 0, "db": 0}
    peak = {"send": 0, "db": 0}

    def enter(kind: str) -> None:
        live[kind] += 1
        peak[kind] = max(peak[kind], live[kind])

    @asynccontextmanager
    async def counting_maker() -> AsyncIterator[AsyncSession]:
        enter("db")
        try:
            async with maker() as db:
                await asyncio.sleep(0.01)
                yield db
        finally:
            live["db"] -= 1

    async def slow_send(*_: Any, **__: Any) -> None:
        enter("send")
        try:
            await asyncio.sleep(0.02)
        finally:
            live["send"] -= 1

    # One subscription per invite, so concurrent sends == concurrent invites.
    await _seed(maker, _Browser(_endpoint()))
    before = _snapshot()
    with (
        patch.object(web_push, "_INVITE_CONCURRENCY", asyncio.Semaphore(bound)),
        patch.object(web_push, "async_session_maker", counting_maker),
        patch.object(web_push, "webpush_async", new=slow_send),
    ):
        for _ in range(10):
            web_push.schedule_share_invite(**_INVITE)
        await asyncio.gather(*web_push._tasks)

    assert peak == {"send": bound, "db": bound}
    assert _deltas(before)["ok"] == 10


@pytest.mark.asyncio
async def test_pending_cap_drops_and_counts(monkeypatch):
    """Invites beyond the pending cap are dropped, logged without PII, and counted."""
    monkeypatch.setattr(web_push, "_MAX_PENDING_INVITES", 3)
    release = asyncio.Event()

    async def blocked(**_: Any) -> None:
        await release.wait()

    before = _count("dropped")
    with patch.object(web_push, "send_share_invite", new=blocked), capture_logs() as logs:
        for _ in range(5):
            web_push.schedule_share_invite(**_INVITE)
        assert len(web_push._tasks) == 3
        release.set()
        await asyncio.gather(*web_push._tasks)

    assert _count("dropped") - before == 2
    dropped = {"event": "push_invite_dropped", "reason": "backlog", "pending": 3}
    assert logs == [{**dropped, "log_level": "warning"}] * 2


# ---- Payload size: one RFC 8291 record ---------------------------------------- //


@pytest.mark.asyncio
@pytest.mark.parametrize("char", ["\U0001f600", "\x01", '"', "\u00e9"])
async def test_longest_payload_fits_one_record(maker, push_on, char: str):
    """Longest title + name the columns hold, in costly chars, encrypts to one 4096-byte record."""
    title = char * Session.__table__.c.title.type.length
    name = char * User.__table__.c.display_name.type.length
    browser = _Browser(_endpoint())
    await _seed(maker, browser)
    wire = _Wire(status=201)
    with patch.object(aiohttp.ClientSession, "post", new=wire.post):
        await web_push.send_share_invite(**{**_INVITE, "session_title": title, "invited_by": name})

    [call] = wire.calls
    plaintext = http_ece.decrypt(
        call["data"], private_key=browser.key, auth_secret=browser.auth, version="aes128gcm"
    )
    assert len(plaintext) <= 3993  # RFC 8291 section 4: 4096 minus header, tag and padding
    assert len(call["data"]) <= 4096
    payload = json.loads(plaintext)
    assert (payload["session_title"], payload["invited_by"]) == (title, name)


# ---- Shutdown: in-flight sends cancelled, client never re-created -------------- //


@pytest.mark.asyncio
async def test_shutdown_cancels_in_flight_send_uncounted(maker, push_on, monkeypatch):
    """A send in flight at shutdown is cancelled, not counted failed; later invites are dropped."""
    arrived, release = asyncio.Event(), asyncio.Event()

    async def hanging_push_service(request: web.Request) -> web.Response:
        arrived.set()
        await release.wait()
        return web.Response(status=201)

    push_app = web.Application()
    push_app.router.add_route("*", "/{tail:.*}", hanging_push_service)
    before, dropped_before = _snapshot(), _count("dropped")
    async with TestServer(push_app) as server:
        monkeypatch.setattr(web_push, "is_push_service_url", lambda _url: True)
        await _seed(maker, _Browser(str(server.make_url(f"/push/{uuid.uuid4().hex}"))))
        with capture_logs() as logs:
            web_push.schedule_share_invite(**_INVITE)
            await asyncio.wait_for(arrived.wait(), timeout=5)
            client = web_push._http
            # Bounded: a regressed cancel loop must fail here, not hang the suite.
            await asyncio.wait_for(web_push.close_http_session(), timeout=5)
            web_push.schedule_share_invite(**_INVITE)
        assert not web_push._tasks
        assert web_push._http is None and client is not None and client.closed
        with pytest.raises(RuntimeError):
            web_push.http_session()
        release.set()

    assert _deltas(before) == {"ok": 0, "gone": 0, "failed": 0, "rejected": 0}
    assert _count("dropped") - dropped_before == 1
    assert [(e["event"], e.get("reason")) for e in logs] == [
        ("push_invites_cancelled", None),
        ("push_invite_dropped", "shutdown"),
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


@pytest.mark.asyncio
async def test_reopen_after_close_accepts_invites():
    """Startup after a shutdown in the same process re-enables scheduling and the client."""
    release = asyncio.Event()

    async def blocked(**_: Any) -> None:
        await release.wait()

    await web_push.close_http_session()
    web_push.open_http_session(create_client=True)
    client = web_push.http_session()
    assert web_push._http is client and not client.closed
    with patch.object(web_push, "send_share_invite", new=blocked):
        web_push.schedule_share_invite(**_INVITE)
        assert len(web_push._tasks) == 1
        release.set()
        await asyncio.wait_for(asyncio.gather(*web_push._tasks), timeout=5)
