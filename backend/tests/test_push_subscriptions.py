"""Web Push phase 1 (spec 003): VAPID config, /api/config exposure, subscription API.

The subscription routes run against a real in-memory SQLite engine so the ON CONFLICT
upsert and the ownership move are exercised as SQL, not as mock calls.
"""

import asyncio
import base64
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.routes.me import _MAX_PUSH_SUBSCRIPTIONS_PER_USER as CAP
from app.api.routes.me import put_my_push_subscription
from app.core.config import Settings, get_settings, validate_settings
from app.models.base import Base
from app.models.push_subscription import PushSubscription
from app.models.schemas import PushSubscriptionUpsert
from app.models.user import User

_USER_A = uuid.UUID("00000000-0000-4000-8000-0000000000a1")
_USER_B = uuid.UUID("00000000-0000-4000-8000-0000000000b2")
_ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
_URL = "/api/me/push-subscriptions"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _new_public_key() -> ec.EllipticCurvePublicKey:
    return ec.generate_private_key(ec.SECP256R1()).public_key()


def _encoded(key: ec.EllipticCurvePublicKey, fmt: serialization.PublicFormat) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, fmt)


def _real_point() -> str:
    return _b64url(_encoded(_new_public_key(), serialization.PublicFormat.UncompressedPoint))


_P256DH = _real_point()
_AUTH = _b64url(b"\x22" * 16)
_VAPID_KEY = ec.generate_private_key(ec.SECP256R1())
# Raw 32-byte scalar: the `web-push generate-vapid-keys` format py_vapid reads.
_VAPID_PRIV = _b64url(_VAPID_KEY.private_numbers().private_value.to_bytes(32, "big"))
_VAPID_PUB = _b64url(
    _encoded(_VAPID_KEY.public_key(), serialization.PublicFormat.UncompressedPoint)
)
# Right length and 0x04 prefix, but not a point on P-256.
_OFF_CURVE = _b64url(b"\x04" + b"\x55" * 64)
# A valid point, but 33 bytes: browsers only send the uncompressed form.
_COMPRESSED = _b64url(_encoded(_new_public_key(), serialization.PublicFormat.CompressedPoint))


def _body(endpoint: str = _ENDPOINT, p256dh: str = _P256DH, auth: str = _AUTH) -> dict[str, Any]:
    # Shape of the browser's PushSubscription.toJSON(), extra field included.
    return {"endpoint": endpoint, "expirationTime": None, "keys": {"p256dh": p256dh, "auth": auth}}


@pytest.fixture
def push_on(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", _VAPID_PUB)
    monkeypatch.setattr(settings, "vapid_private_key", SecretStr(_VAPID_PRIV))
    monkeypatch.setattr(settings, "vapid_subject", "mailto:ops@example.com")


@pytest.fixture
def push_off(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "vapid_public_key", "")
    monkeypatch.setattr(settings, "vapid_private_key", SecretStr(""))
    monkeypatch.setattr(settings, "vapid_subject", "")


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[PushSubscription.__table__])
        # The PUT locks the caller's users row; the real model's ARRAY column has no SQLite DDL.
        await conn.exec_driver_sql("CREATE TABLE users (id CHAR(32) PRIMARY KEY)")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def caller() -> SimpleNamespace:
    """The authenticated user; tests switch identity by reassigning ``id``."""
    return SimpleNamespace(id=_USER_A)


@pytest_asyncio.fixture
async def client(
    maker: async_sessionmaker[AsyncSession], caller: SimpleNamespace
) -> AsyncIterator[AsyncClient]:
    from app.api.dependencies import get_current_user, get_db
    from app.main import app

    async def _db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session
            await session.commit()

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = lambda: caller
    app.dependency_overrides[get_db] = _db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:  # type: ignore[arg-type]
        yield c
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


async def _rows(maker: async_sessionmaker[AsyncSession]) -> list[PushSubscription]:
    async with maker() as session:
        return list((await session.execute(select(PushSubscription))).scalars().all())


# ---- AC1: /api/config exposure ------------------------------------------------ //


@pytest.mark.asyncio
async def test_config_exposes_public_key_when_on(authenticated_client: AsyncClient, push_on):
    """003-AC1: all three VAPID vars set -> push_public_key is the public key."""
    resp = await authenticated_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["push_public_key"] == _VAPID_PUB


@pytest.mark.asyncio
async def test_config_push_key_null_when_off(authenticated_client: AsyncClient, push_off):
    """003-AC1: no VAPID vars -> push_public_key is null."""
    resp = await authenticated_client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["push_public_key"] is None


# ---- AC2: boot validation ----------------------------------------------------- //


def _validate(public: str = "", private: str = "", subject: str = "") -> None:
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


def test_validate_accepts_all_or_none():
    """003-AC1: all set boots, none set boots."""
    _validate()
    _validate(_VAPID_PUB, _VAPID_PRIV, "mailto:ops@example.com")
    _validate(_VAPID_PUB, _VAPID_PRIV, "https://example.com")
    _validate(_VAPID_PUB + "=", _VAPID_PRIV, "mailto:ops@example.com")  # padded base64url


@pytest.mark.parametrize(
    ("kwargs", "missing"),
    [
        ({"public": "pub"}, "VAPID_PRIVATE_KEY, VAPID_SUBJECT"),
        ({"public": "pub", "private": "priv"}, "VAPID_SUBJECT"),
        ({"private": "priv", "subject": "mailto:a@b"}, "VAPID_PUBLIC_KEY"),
    ],
)
def test_validate_rejects_partial_naming_missing(kwargs: dict[str, str], missing: str):
    """003-AC2: one or two VAPID vars set -> refuses to boot, naming the missing ones."""
    with pytest.raises(ValueError, match=f"missing: {missing}$"):
        _validate(**kwargs)


@pytest.mark.parametrize(
    "subject",
    [
        "ops@example.com",
        "http://example.com",
        "mailto",
        "mailto:",
        "https:",
        "https://",
        "https:///path",
        "mailto:ops@example.com\n",
        " https://example.com",
        "https://exa\nmple.com",
        "mailto:ops@\x00example.com",
        "https://user:pw@example.com",  # pragma: allowlist secret
        "https://@example.com",
        "https://[::1",  # urlsplit raises
        "https://exämple.com",
        "https://example.com:99999",
        "https://example.com:abc",
        "mailto:ops@example.com/path",
        "mailto:ops@example.com,evil@x.org",
        "mailto:ops@example.com?subject=x",
        "https://dead:1234",  # py_vapid reads a hex host:port as IPv6
        "https://cafe:",
    ],
)
def test_validate_rejects_bad_subject(subject: str):
    """003-AC2: VAPID_SUBJECT is 'mailto:<user>@<domain>' or 'https://<domain>', nothing after."""
    with pytest.raises(ValueError, match="VAPID_SUBJECT must be"):
        _validate(_VAPID_PUB, _VAPID_PRIV, subject)


_PEM = (
    _new_public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)


@pytest.mark.parametrize(
    "public",
    [
        _PEM,
        _VAPID_PUB + "\n",  # file-mounted secret
        f"  {_VAPID_PUB}  ",
        _b64url(b"\x04" + b"\x55" * 63),  # 64 bytes
        _b64url(b"\x02" + b"\x55" * 64),  # compressed-point prefix
        _COMPRESSED,
        _OFF_CURVE,
        base64.b64encode(b"\x04" + b"\xff" * 64).decode(),  # standard alphabet (+, /)
        "pub",
    ],
)
def test_validate_rejects_malformed_public_key(public: str):
    """003-AC2: a VAPID_PUBLIC_KEY browsers would refuse fails at boot, not at subscribe."""
    with pytest.raises(ValueError, match="VAPID_PUBLIC_KEY must be"):
        _validate(public, "priv", "mailto:ops@example.com")


def test_private_key_is_secret():
    # A plain str would leak the key through repr()/model_dump() into logs.
    assert Settings.model_fields["vapid_private_key"].annotation is SecretStr


# ---- AC3: upsert + ownership move --------------------------------------------- //


@pytest.mark.asyncio
async def test_put_inserts_row_for_caller(client: AsyncClient, maker, push_on):
    """003-AC3: a PUT creates exactly one row owned by the caller."""
    resp = await client.put(_URL, json=_body(), headers={"User-Agent": "UA-1"})
    assert resp.status_code == 204
    rows = await _rows(maker)
    assert [(r.user_id, r.endpoint, r.p256dh, r.auth, r.user_agent) for r in rows] == [
        (_USER_A, _ENDPOINT, _P256DH, _AUTH, "UA-1")
    ]


@pytest.mark.asyncio
async def test_second_put_updates_same_row(client: AsyncClient, maker, push_on):
    """003-AC3: re-PUT of the same endpoint updates keys + user agent, still one row."""
    await client.put(_URL, json=_body(), headers={"User-Agent": "UA-1"})
    first = (await _rows(maker))[0]
    new_p256dh = _real_point()
    new_auth = _b64url(b"\x44" * 16)
    resp = await client.put(
        _URL, json=_body(p256dh=new_p256dh, auth=new_auth), headers={"User-Agent": "U" * 600}
    )
    assert resp.status_code == 204
    rows = await _rows(maker)
    assert len(rows) == 1
    assert rows[0].id == first.id
    assert (rows[0].p256dh, rows[0].auth) == (new_p256dh, new_auth)
    assert rows[0].user_agent == "U" * 512


@pytest.mark.asyncio
async def test_put_by_other_user_moves_row(client: AsyncClient, maker, caller, push_on):
    """003-AC3: the same endpoint PUT by another user moves the row to that user."""
    await client.put(_URL, json=_body())
    caller.id = _USER_B
    resp = await client.put(_URL, json=_body())
    assert resp.status_code == 204
    rows = await _rows(maker)
    assert [(r.user_id, r.endpoint) for r in rows] == [(_USER_B, _ENDPOINT)]


async def _seed(
    maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID, n: int, *, stagger: str
) -> list[str]:
    """Insert ``n`` rows for ``user_id``; index 0 is the oldest by ``stagger``, the other tied."""
    base = datetime(2020, 1, 1)
    endpoints = [f"{_ENDPOINT}/{user_id}/{i}" for i in range(n)]
    async with maker() as session:
        for i, endpoint in enumerate(endpoints):
            stamps = {
                "last_seen_at": base,
                "created_at": base,
                stagger: base + timedelta(minutes=i),
            }
            session.add(
                PushSubscription(
                    user_id=user_id, endpoint=endpoint, p256dh=_P256DH, auth=_AUTH, **stamps
                )
            )
        await session.commit()
    return endpoints


def _endpoints_of(rows: list[PushSubscription], user_id: uuid.UUID) -> set[str]:
    return {r.endpoint for r in rows if r.user_id == user_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("stagger", ["last_seen_at", "created_at"])
async def test_put_beyond_cap_evicts_oldest(client: AsyncClient, maker, stagger: str, push_on):
    """003-AC3: a PUT past the per-user cap evicts the least recently seen row, then oldest."""
    seeded = await _seed(maker, _USER_A, CAP, stagger=stagger)
    resp = await client.put(_URL, json=_body())
    assert resp.status_code == 204
    assert _endpoints_of(await _rows(maker), _USER_A) == {*seeded[1:], _ENDPOINT}


@pytest.mark.asyncio
async def test_re_put_at_cap_evicts_nothing(client: AsyncClient, maker, push_on):
    """003-AC3: re-PUT of an existing endpoint at the cap (even the oldest) evicts nothing."""
    seeded = await _seed(maker, _USER_A, CAP, stagger="last_seen_at")
    resp = await client.put(_URL, json=_body(endpoint=seeded[0]))
    assert resp.status_code == 204
    assert _endpoints_of(await _rows(maker), _USER_A) == set(seeded)


@pytest.mark.asyncio
async def test_put_never_evicts_the_row_it_wrote(client: AsyncClient, maker, push_on):
    """003-AC3: even when every other row looks newer, the just-written row survives."""
    async with maker() as session:
        for i in range(CAP):
            future = datetime(2100, 1, 1) + timedelta(minutes=i)
            session.add(
                PushSubscription(
                    user_id=_USER_A,
                    endpoint=f"{_ENDPOINT}/future/{i}",
                    p256dh=_P256DH,
                    auth=_AUTH,
                    last_seen_at=future,
                    created_at=future,
                )
            )
        await session.commit()
    resp = await client.put(_URL, json=_body())
    assert resp.status_code == 204
    endpoints = _endpoints_of(await _rows(maker), _USER_A)
    assert _ENDPOINT in endpoints
    assert len(endpoints) == CAP


@pytest.mark.asyncio
async def test_moved_row_counts_toward_new_owner_cap(client: AsyncClient, maker, caller, push_on):
    """003-AC3: a row moved in from another user counts toward the new owner's cap only."""
    seeded_b = await _seed(maker, _USER_B, CAP, stagger="last_seen_at")
    await client.put(_URL, json=_body())
    await client.put(_URL, json=_body(endpoint=_ENDPOINT + "-other"))
    caller.id = _USER_B
    resp = await client.put(_URL, json=_body())
    assert resp.status_code == 204
    rows = await _rows(maker)
    assert _endpoints_of(rows, _USER_B) == {*seeded_b[1:], _ENDPOINT}
    assert _endpoints_of(rows, _USER_A) == {_ENDPOINT + "-other"}


@pytest.mark.asyncio
async def test_put_rejected_when_push_off(client: AsyncClient, maker, push_off):
    """Feature off (no VAPID keys): PUT is refused and stores nothing."""
    resp = await client.put(_URL, json=_body())
    assert resp.status_code == 404
    assert await _rows(maker) == []


# ---- AC4: auth + ownership on DELETE ------------------------------------------ //


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_unauthenticated_gets_401(authenticated_client: AsyncClient, method: str, push_on):
    """003-AC4: no session cookie with OIDC on -> 401 on PUT and DELETE."""
    from app.api.dependencies import get_current_user
    from app.main import app

    app.dependency_overrides.pop(get_current_user)
    with patch("app.api.dependencies.get_settings", return_value=MagicMock(oidc_enabled=True)):
        resp = await authenticated_client.request(method, _URL, json=_body())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_removes_own_row(client: AsyncClient, maker, push_on):
    """003-AC4: a user deletes their own subscription."""
    await client.put(_URL, json=_body())
    resp = await client.request("DELETE", _URL, json={"endpoint": _ENDPOINT})
    assert resp.status_code == 204
    assert await _rows(maker) == []


@pytest.mark.asyncio
async def test_delete_of_other_users_row_404_and_intact(
    client: AsyncClient, maker, caller, push_on
):
    """003-AC4: DELETE never removes another user's row (404, row intact)."""
    await client.put(_URL, json=_body())
    caller.id = _USER_B
    resp = await client.request("DELETE", _URL, json={"endpoint": _ENDPOINT})
    assert resp.status_code == 404
    assert [(r.user_id, r.endpoint) for r in await _rows(maker)] == [(_USER_A, _ENDPOINT)]


@pytest.mark.asyncio
async def test_delete_allowed_when_push_off(client: AsyncClient, maker, push_on, monkeypatch):
    """Turning the feature off must not strand rows: DELETE still works."""
    await client.put(_URL, json=_body())
    monkeypatch.setattr(get_settings(), "vapid_public_key", "")
    resp = await client.request("DELETE", _URL, json={"endpoint": _ENDPOINT})
    assert resp.status_code == 204
    assert await _rows(maker) == []


# ---- AC5: payload validation -------------------------------------------------- //


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _body(endpoint="http://fcm.googleapis.com/fcm/send/abc123"),
        _body(endpoint="https://"),
        _body(endpoint="ftp://example.com/x"),
        _body(endpoint="https://exa\nmple.com/x\t y"),
        _body(endpoint="https://example.com/x y"),
        _body(endpoint="https://example.com/\x7f"),
        _body(endpoint="https://user:pw@127.0.0.1:5432/x"),  # pragma: allowlist secret
        _body(endpoint="https://@example.com/x"),
        _body(endpoint="https://[::1/x"),  # urlsplit raises
        _body(endpoint="https://example.com/" + "é" * 2000),  # >2704 bytes: btree row limit
        _body(endpoint="https://example.com:99999/x"),
        _body(endpoint="https://example.com:abc/x"),
        _body(endpoint="https://example.com/" + "a" * 2048),
        _body(p256dh=_b64url(b"\x04" + b"\x11" * 63)),  # 64 bytes
        _body(p256dh=_b64url(b"\x02" + b"\x11" * 64)),  # 65 bytes, not uncompressed
        _body(p256dh=_OFF_CURVE),
        _body(p256dh=_COMPRESSED),
        _body(auth=_b64url(b"\x22" * 15)),
        _body(auth=_b64url(b"\x22" * 17)),
        _body(p256dh="not base64url!"),
        _body(auth="+/+/+/+/+/+/+/+/+/+/+w"),  # standard base64 alphabet, not url-safe
        _body(auth="a"),  # impossible base64 length
        _body(p256dh="A" * 129),
        _body(auth="A" * 129),
        {"endpoint": _ENDPOINT, "keys": {"p256dh": _P256DH}},
    ],
)
async def test_invalid_payload_422_stores_nothing(client: AsyncClient, maker, body, push_on):
    """003-AC5: non-https endpoint or bad base64url/key length -> 422, nothing stored."""
    resp = await client.put(_URL, json=body)
    assert resp.status_code == 422
    assert await _rows(maker) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["p256dh", "auth"])
async def test_oversized_key_rejected_by_length(client: AsyncClient, maker, field: str, push_on):
    """003-AC5: an oversized key is cut off by max_length, before the regex/decode runs."""
    resp = await client.put(_URL, json=_body(**{field: "A" * 100_000}))
    assert resp.status_code == 422
    assert [e["type"] for e in resp.json()["detail"]] == ["string_too_long"]
    assert await _rows(maker) == []


@pytest.mark.asyncio
async def test_padded_base64url_tolerated(client: AsyncClient, maker, push_on):
    """003-AC5: padded base64url keys are accepted (stored verbatim)."""
    padded_auth = base64.urlsafe_b64encode(b"\x22" * 16).decode()
    assert padded_auth.endswith("==")
    resp = await client.put(_URL, json=_body(auth=padded_auth))
    assert resp.status_code == 204
    assert (await _rows(maker))[0].auth == padded_auth


# ---- AC15: push-service host allowlist on PUT ---------------------------------- //


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com/x",
        "https://push.apple.com.evil.com/x",  # allowed suffix, but not at the end
        "https://xfcm.googleapis.com/x",  # exact host, no dot boundary
        "https://evilpush.apple.com/x",  # suffix without the leading dot
        "https://fcm.googleapis.com.evil.com/fcm/send/abc",
        "https://push.apple.com/x",  # the bare suffix is not a subdomain of itself
        "https://169.254.169.254/latest/meta-data",
        "https://127.0.0.1/x",
    ],
)
async def test_put_foreign_push_host_422(client: AsyncClient, maker, endpoint: str, push_on):
    """003-AC15: an endpoint outside the four browser push services gets 422, nothing stored."""
    resp = await client.put(_URL, json=_body(endpoint=endpoint))
    assert resp.status_code == 422
    assert "push service" in resp.text
    assert await _rows(maker) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/abc",
        "https://FCM.googleapis.com/fcm/send/abc",  # hosts compare case-insensitively
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://wns2-par02p.notify.windows.com/w/?token=abc",
        "https://web.push.apple.com/abc",
    ],
)
async def test_put_browser_push_hosts_accepted(client: AsyncClient, maker, endpoint: str, push_on):
    """003-AC15: Chrome, Firefox, Edge and Safari endpoint shapes are accepted."""
    resp = await client.put(_URL, json=_body(endpoint=endpoint))
    assert resp.status_code == 204
    assert [r.endpoint for r in await _rows(maker)] == [endpoint]


@pytest.mark.asyncio
async def test_delete_foreign_host_row_still_allowed(client: AsyncClient, maker, push_on):
    """003-AC15: the allowlist is PUT-only, so a row stored before it can still be removed."""
    async with maker() as session:
        session.add(
            PushSubscription(
                user_id=_USER_A, endpoint="https://example.com/x", p256dh=_P256DH, auth=_AUTH
            )
        )
        await session.commit()
    resp = await client.request("DELETE", _URL, json={"endpoint": "https://example.com/x"})
    assert resp.status_code == 204
    assert await _rows(maker) == []


# ---- AC3 on real Postgres: the cap under concurrency -------------------------- //

_PG_URL = os.environ.get("INKWEST_TEST_DATABASE_URL")


@pytest_asyncio.fixture
async def pg_user() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], uuid.UUID]]:
    """A throwaway user on the migrated Postgres; deleting it cascades its subscriptions."""
    if not _PG_URL:
        pytest.skip("INKWEST_TEST_DATABASE_URL unset — needs a migrated Postgres")
    engine = create_async_engine(
        _PG_URL,
        pool_size=12,
        connect_args={"server_settings": {"search_path": "inkwest,public"}},
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    try:
        async with maker() as db:
            db.add(
                User(
                    id=uid,
                    oidc_subject=f"s-{uid}",
                    oidc_issuer="local",
                    email=f"{uid}@x",
                    display_name="X",
                    roles=[],
                )
            )
            await db.commit()
        yield maker, uid
    finally:
        async with maker() as db:
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()
        await engine.dispose()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_concurrent_puts_at_cap_keep_exactly_cap_live(pg_user, push_on):
    """003-AC3: parallel PUTs at the cap leave exactly cap rows (per-user row lock)."""
    maker, uid = pg_user
    await _seed(maker, uid, CAP, stagger="last_seen_at")

    async def _put(i: int) -> None:
        body = PushSubscriptionUpsert.model_validate(_body(endpoint=f"{_ENDPOINT}/{uid}/{i}"))
        async with maker() as db:
            await put_my_push_subscription(body, SimpleNamespace(id=uid), None, db)  # type: ignore[arg-type]
            await db.commit()

    await asyncio.gather(*(_put(i) for i in range(10)))
    async with maker() as db:
        count = await db.scalar(
            select(func.count())
            .select_from(PushSubscription)
            .where(PushSubscription.user_id == uid)
        )
    assert count == CAP


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_put_lock_does_not_block_fk_inserts_live(pg_user, push_on):
    """003-AC3: the per-user lock is FOR NO KEY UPDATE, so other FK inserts on the user proceed."""
    maker, uid = pg_user
    body = PushSubscriptionUpsert.model_validate(_body(endpoint=f"{_ENDPOINT}/{uid}/held"))
    async with maker() as holder:
        await put_my_push_subscription(body, SimpleNamespace(id=uid), None, holder)  # type: ignore[arg-type]
        try:
            async with maker() as other:
                await other.execute(text("SET LOCAL lock_timeout = '1s'"))
                other.add(
                    PushSubscription(
                        user_id=uid, endpoint=f"{_ENDPOINT}/{uid}/other", p256dh=_P256DH, auth=_AUTH
                    )
                )
                await other.commit()
        finally:
            await holder.rollback()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_fresh_subscription_grace_on_postgres_live(pg_user, push_on):
    """003-AC7: on timestamptz (aware) rows, a just-created row survives a 410, an old one not."""
    from unittest.mock import AsyncMock

    from pywebpush import WebPushException

    from app.services import web_push

    maker, uid = pg_user
    endpoint = f"{_ENDPOINT}/{uid}/grace"
    async with maker() as db:  # created_at = server-side now()
        db.add(PushSubscription(user_id=uid, endpoint=endpoint, p256dh=_P256DH, auth=_AUTH))
        await db.commit()
    gone = AsyncMock(side_effect=WebPushException("410", SimpleNamespace(status=410)))
    kwargs = {
        "user_id": uid,
        "share_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "session_title": "t",
        "invited_by": "n",
    }
    web_push.open_http_session(create_client=False)
    try:
        with (
            patch.object(web_push, "async_session_maker", maker),
            patch.object(web_push, "webpush_async", new=gone),
        ):
            await web_push.send_share_invite(**kwargs)
            assert [r.endpoint for r in await _rows(maker)] == [endpoint]
            async with maker() as db:
                await db.execute(
                    text(
                        "UPDATE push_subscriptions SET created_at = now() - interval '2 minutes' "
                        "WHERE endpoint = :e"
                    ),
                    {"e": endpoint},
                )
                await db.commit()
            await web_push.send_share_invite(**kwargs)
            assert await _rows(maker) == []
    finally:
        await web_push.close_http_session()
