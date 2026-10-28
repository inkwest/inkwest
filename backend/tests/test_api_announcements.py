"""Tests for the announcement banner routes (spec 004, Phase 1)."""

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.sql.functions import now as sql_now

from app.api.routes.announcements import active_announcements_query
from app.models import schemas
from app.models.announcement import Announcement
from app.services import pubsub

VALID_BODY: dict[str, Any] = {
    "message": "Bedrock is throttled, expect slow answers",
    "level": "warning",
    "dismissible": True,
    "enabled": True,
}
CHANGED = {"type": "announcements_changed"}
# What the API strips (str.strip()) and the DB CHECK refuses at either end of a message.
ISSPACE = {c for c in range(0x110000) if chr(c).isspace()}


def _row(**overrides: Any) -> Announcement:
    now = datetime.now(UTC)
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "message": "hello",
        "level": "info",
        "color": None,
        "text_color": None,
        "opacity": 100,
        "dismissible": True,
        "enabled": True,
        "ends_at": None,
        "created_by": uuid.uuid4(),
        "created_at": now,
        "updated_at": now,
    }
    return Announcement(**{**fields, **overrides})


def _scalars(rows: list[Announcement]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _fill_server_defaults(obj: Announcement) -> None:
    obj.id = obj.id or uuid.uuid4()
    obj.created_at = obj.created_at or datetime.now(UTC)
    obj.updated_at = datetime.now(UTC)


@pytest.fixture
def notify() -> Iterator[AsyncMock]:
    with patch.object(pubsub, "notify", new_callable=AsyncMock) as mock:
        yield mock


@pytest.fixture
def write_db(mock_db: AsyncMock) -> AsyncMock:
    mock_db.refresh = AsyncMock(side_effect=_fill_server_defaults)
    return mock_db


# ---- Read route: filter semantics (004-AC2) ------------------------------------------------ //


def test_active_query_filters_disabled_and_expired() -> None:
    """004-AC2: only enabled rows whose ends_at is NULL or in the future, oldest first."""
    sql = _sql(active_announcements_query())
    where = sql.split("WHERE", 1)[1]
    assert where.split("ORDER BY")[0].strip() == (
        "announcements.enabled AND (announcements.ends_at IS NULL OR announcements.ends_at > now())"
    )
    assert sql.rstrip().endswith("ORDER BY announcements.created_at ASC")


@pytest.mark.asyncio
async def test_read_route_runs_the_active_query(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """004-AC2: the route serves exactly the filtered query, not an unfiltered select."""
    mock_db.execute = AsyncMock(return_value=_scalars([]))

    resp = await authenticated_client.get("/api/announcements")

    assert resp.status_code == 200
    assert resp.json() == []
    (stmt,), _ = mock_db.execute.await_args
    assert _sql(stmt) == _sql(active_announcements_query())


@pytest.mark.asyncio
async def test_read_route_public_payload_hides_author_and_enabled(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """004-AC2: the public shape carries no created_by, created_at or enabled."""
    row = _row(ends_at=datetime.now(UTC) + timedelta(hours=1))
    mock_db.execute = AsyncMock(return_value=_scalars([row]))

    resp = await authenticated_client.get("/api/announcements")

    assert resp.status_code == 200
    (item,) = resp.json()
    assert set(item) == {
        "id",
        "message",
        "level",
        "color",
        "text_color",
        "opacity",
        "dismissible",
        "ends_at",
        "updated_at",
    }
    assert item["id"] == str(row.id)


# ---- Auth (004-AC9) ------------------------------------------------------------------------ //


@pytest.mark.asyncio
async def test_read_route_unauthenticated_is_401(mock_db: AsyncMock) -> None:
    """004-AC9: no session cookie in OIDC mode -> 401 (dev mode would auto-login)."""
    from app.api.dependencies import get_db
    from app.main import app

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        transport = ASGITransport(app=app)  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            with patch(
                "app.api.dependencies.get_settings", return_value=MagicMock(oidc_enabled=True)
            ):
                resp = await client.get("/api/announcements")
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/admin/announcements", None),
        ("POST", "/api/admin/announcements", VALID_BODY),
        ("PUT", f"/api/admin/announcements/{uuid.uuid4()}", VALID_BODY),
        ("DELETE", f"/api/admin/announcements/{uuid.uuid4()}", None),
    ],
)
async def test_admin_routes_forbid_non_admin(
    authenticated_client: AsyncClient,
    mock_db: AsyncMock,
    notify: AsyncMock,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """004-AC9: every admin route is 403 for a signed-in non-admin, with no side effect."""
    resp = await authenticated_client.request(method, path, json=body)

    assert resp.status_code == 403
    mock_db.commit.assert_not_awaited()
    notify.assert_not_awaited()


# ---- Validation (004-AC10) ------------------------------------------------------------------ //

_BAD_BODIES: list[tuple[str, dict[str, Any]]] = [
    ("empty message", {"message": ""}),
    ("whitespace-only message", {"message": "   \t "}),
    ("isspace-only message outside Rust trim", {"message": "\x1c\x1d\x1e\x1f"}),
    ("nul", {"message": "before\x00after"}),
    ("501-char message", {"message": "a" * 501}),
    ("unknown level", {"level": "critical"}),
    ("short color", {"color": "#fff"}),
    ("named color", {"color": "red"}),
    ("bad text color", {"text_color": "#12345g"}),
    ("opacity 9", {"opacity": 9}),
    ("opacity 101", {"opacity": 101}),
    ("naive datetime", {"ends_at": (datetime.now() + timedelta(days=1)).isoformat()}),
    ("ends_at past year 9999 in UTC", {"ends_at": "9999-12-31T23:00:00-05:00"}),
    ("missing enabled", {"enabled": None}),
    ("missing dismissible", {"dismissible": None}),
]


# A create already refuses any past ends_at; this shape only reaches the overflow guard on PUT.
_BAD_PUT_ONLY: list[tuple[str, dict[str, Any]]] = [
    ("ends_at before year 1 in UTC", {"ends_at": "0001-01-01T00:30:00+01:00"}),
]
_BAD_CASES = [
    pytest.param(method, overrides, id=f"{method}-{case}")
    for case, overrides in _BAD_BODIES
    for method in ("POST", "PUT")
] + [pytest.param("PUT", overrides, id=f"PUT-{case}") for case, overrides in _BAD_PUT_ONLY]


def _bad(overrides: dict[str, Any]) -> dict[str, Any]:
    body = {**VALID_BODY, **overrides}
    # A None override stands for "field absent".
    return {k: v for k, v in body.items() if v is not None}


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "overrides"), _BAD_CASES)
async def test_write_rejects_invalid_body(
    admin_client: AsyncClient,
    write_db: AsyncMock,
    notify: AsyncMock,
    method: str,
    overrides: dict[str, Any],
) -> None:
    """004-AC10: 422 on each bound, on create and on full-replace update alike."""
    write_db.get = AsyncMock(return_value=_row())
    path = "/api/admin/announcements"
    if method == "PUT":
        path += f"/{uuid.uuid4()}"

    resp = await admin_client.request(method, path, json=_bad(overrides))

    assert resp.status_code == 422
    write_db.commit.assert_not_awaited()
    notify.assert_not_awaited()


def test_message_at_cap_is_accepted() -> None:
    """004-AC10: 500 chars is the inclusive cap, measured after stripping."""
    from app.models.schemas import AnnouncementCreate

    body = AnnouncementCreate(**{**VALID_BODY, "message": "  " + "a" * 500 + "  "})
    assert body.message == "a" * 500


def test_message_strips_the_exact_isspace_set() -> None:
    """004-AC10: the API strips every char the DB CHECK refuses at either end, nothing else."""
    from app.models.schemas import AnnouncementCreate

    for c in ISSPACE:
        body = AnnouncementCreate(**{**VALID_BODY, "message": f"{chr(c)}a\nb{chr(c)}"})
        assert body.message == "a\nb", hex(c)
    # Every other codepoint is accepted by the DB at either end, so the API must keep it.
    # NUL and lone surrogates are excluded: neither can reach a stored message.
    adapter = TypeAdapter(schemas.AnnouncementMessage)
    for c in range(1, 0x110000):
        if c in ISSPACE or 0xD800 <= c <= 0xDFFF:
            continue
        message = f"{chr(c)}a{chr(c)}"
        assert adapter.validate_python(message) == message, hex(c)


def test_ends_at_is_normalized_to_utc() -> None:
    from app.models.schemas import AnnouncementCreate

    body = AnnouncementCreate(**{**VALID_BODY, "ends_at": "9999-01-01T02:00:00+02:00"})
    assert body.ends_at == datetime(9999, 1, 1, tzinfo=UTC)
    assert body.ends_at is not None and body.ends_at.tzinfo is UTC


MIGRATION = Path(__file__).parents[1] / "migrations" / "20261028000000_announcements.sql"


def _migration_checks() -> dict[str, str]:
    """Named CHECK constraint -> its whitespace-normalized expression, from the up section."""
    up = MIGRATION.read_text().split("-- migrate:down")[0]
    found = re.finditer(r"CONSTRAINT (\w+)\s+CHECK \((.+?)\)(?=,\n|\n\);)", up, re.S)
    return {m[1]: " ".join(m[2].split()) for m in found}


def _expand_class(cls: str) -> set[int]:
    """Codepoints of a bracket body written only as \\uXXXX and \\uXXXX-\\uXXXX items."""
    item = r"\\u([0-9A-F]{4})(?:-\\u([0-9A-F]{4}))?"
    assert re.fullmatch(f"(?:{item})+", cls), cls
    out: set[int] = set()
    for lo, hi in re.findall(item, cls):
        out.update(range(int(lo, 16), int(hi or lo, 16) + 1))
    return out


def test_db_checks_mirror_api_bounds() -> None:
    """004-AC10: each table CHECK carries the same bounds as the API schema constants."""
    checks = _migration_checks()
    pattern = schemas.ANNOUNCEMENT_COLOR_PATTERN

    assert set(checks) == {
        "announcements_message_check",
        "announcements_level_check",
        "announcements_color_check",
        "announcements_text_color_check",
        "announcements_opacity_check",
    }
    message = re.fullmatch(
        r"length\(message\) BETWEEN 1 AND (\d+) AND message !~ '\^\[([^\]]+)\]\|\[([^\]]+)\]\$'",
        checks["announcements_message_check"],
    )
    assert message is not None
    assert int(message[1]) == schemas.ANNOUNCEMENT_MESSAGE_MAX_LENGTH
    assert _expand_class(message[2]) == ISSPACE  # leading
    assert _expand_class(message[3]) == ISSPACE  # trailing
    assert checks["announcements_opacity_check"] == (
        f"opacity BETWEEN {schemas.ANNOUNCEMENT_OPACITY_MIN} AND {schemas.ANNOUNCEMENT_OPACITY_MAX}"
    )
    assert checks["announcements_color_check"] == f"color IS NULL OR color ~ '{pattern}'"
    assert checks["announcements_text_color_check"] == (
        f"text_color IS NULL OR text_color ~ '{pattern}'"
    )
    level = re.fullmatch(r"level IN \((.+)\)", checks["announcements_level_check"])
    assert level is not None
    assert set(re.findall(r"'(\w+)'", level[1])) == set(get_args(schemas.AnnouncementLevel))


@pytest.mark.asyncio
async def test_create_rejects_past_ends_at(
    admin_client: AsyncClient, write_db: AsyncMock, notify: AsyncMock
) -> None:
    """004-AC10: a create whose ends_at is already past would be born invisible."""
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    resp = await admin_client.post("/api/admin/announcements", json={**VALID_BODY, "ends_at": past})

    assert resp.status_code == 422
    write_db.add.assert_not_called()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_accepts_past_ends_at(
    admin_client: AsyncClient, write_db: AsyncMock, notify: AsyncMock
) -> None:
    """004-AC10: an update may set a past ends_at, it is how an admin retires a banner."""
    row = _row()
    write_db.get = AsyncMock(return_value=row)
    past = datetime.now(UTC) - timedelta(minutes=1)

    resp = await admin_client.put(
        f"/api/admin/announcements/{row.id}", json={**VALID_BODY, "ends_at": past.isoformat()}
    )

    assert resp.status_code == 200
    assert row.ends_at == past
    notify.assert_awaited_once_with(CHANGED, user_id=None)


# ---- Admin CRUD ----------------------------------------------------------------------------- //


@pytest.mark.asyncio
async def test_admin_list_returns_all_rows_newest_first(
    admin_client: AsyncClient, mock_db: AsyncMock, mock_admin_user: MagicMock
) -> None:
    row = _row(enabled=False, created_by=mock_admin_user.id)
    mock_db.execute = AsyncMock(return_value=_scalars([row]))

    resp = await admin_client.get("/api/admin/announcements")

    assert resp.status_code == 200
    (item,) = resp.json()
    assert item["enabled"] is False
    assert item["created_by"] == str(mock_admin_user.id)
    (stmt,), _ = mock_db.execute.await_args
    sql = _sql(stmt)
    assert "WHERE" not in sql
    assert sql.rstrip().endswith("ORDER BY announcements.created_at DESC")


@pytest.mark.asyncio
async def test_create_commits_then_broadcasts(
    admin_client: AsyncClient,
    write_db: AsyncMock,
    notify: AsyncMock,
    mock_admin_user: MagicMock,
) -> None:
    """Broadcast only once the write is durable, so a refetching tab sees it."""
    order = MagicMock()
    write_db.commit.side_effect = lambda: order.commit()
    notify.side_effect = lambda *a, **k: order.notify()
    body = {**VALID_BODY, "message": "  padded  ", "color": "#AbCdEf", "opacity": 40}

    resp = await admin_client.post("/api/admin/announcements", json=body)

    assert resp.status_code == 201
    data = resp.json()
    assert data["message"] == "padded"
    assert data["created_by"] == str(mock_admin_user.id)
    assert data["color"] == "#AbCdEf"
    assert data["opacity"] == 40
    (added,), _ = write_db.add.call_args
    assert added.created_by == mock_admin_user.id
    notify.assert_awaited_once_with(CHANGED, user_id=None)
    assert [c[0] for c in order.mock_calls] == ["commit", "notify"]


@pytest.mark.asyncio
async def test_update_replaces_fields_and_bumps_updated_at(
    admin_client: AsyncClient, write_db: AsyncMock, notify: AsyncMock
) -> None:
    row = _row(color="#000000", text_color="#ffffff", opacity=50)
    write_db.get = AsyncMock(return_value=row)
    bumped: list[bool] = []

    def refresh(obj: Announcement) -> None:
        # An SQL now() expression, not a stale Python value, is what gets flushed.
        bumped.append(isinstance(obj.updated_at, sql_now))
        obj.updated_at = datetime.now(UTC)

    write_db.refresh = AsyncMock(side_effect=refresh)

    resp = await admin_client.put(f"/api/admin/announcements/{row.id}", json=VALID_BODY)

    assert resp.status_code == 200
    # Full replace: omitted optional fields go back to their defaults.
    assert (row.color, row.text_color, row.opacity) == (None, None, 100)
    assert row.message == VALID_BODY["message"]
    assert bumped == [True]
    write_db.commit.assert_awaited_once()
    notify.assert_awaited_once_with(CHANGED, user_id=None)


@pytest.mark.asyncio
async def test_delete_removes_row_and_broadcasts(
    admin_client: AsyncClient, write_db: AsyncMock, notify: AsyncMock
) -> None:
    row = _row()
    write_db.get = AsyncMock(return_value=row)

    resp = await admin_client.delete(f"/api/admin/announcements/{row.id}")

    assert resp.status_code == 204
    write_db.delete.assert_awaited_once_with(row)
    write_db.commit.assert_awaited_once()
    notify.assert_awaited_once_with(CHANGED, user_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_unknown_id_is_404(
    admin_client: AsyncClient, write_db: AsyncMock, notify: AsyncMock, method: str
) -> None:
    write_db.get = AsyncMock(return_value=None)

    resp = await admin_client.request(
        method, f"/api/admin/announcements/{uuid.uuid4()}", json=VALID_BODY
    )

    assert resp.status_code == 404
    write_db.commit.assert_not_awaited()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_racing_a_delete_is_404(
    admin_client: AsyncClient, write_db: AsyncMock, notify: AsyncMock
) -> None:
    """The row is loaded, a concurrent DELETE commits, the UPDATE then matches 0 rows."""
    write_db.get = AsyncMock(return_value=_row())
    write_db.flush = AsyncMock(side_effect=StaleDataError("0 were matched"))

    resp = await admin_client.put(f"/api/admin/announcements/{uuid.uuid4()}", json=VALID_BODY)

    assert resp.status_code == 404
    write_db.commit.assert_not_awaited()
    notify.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_failed_broadcast_does_not_fail_the_write(
    admin_client: AsyncClient, write_db: AsyncMock, notify: AsyncMock, method: str
) -> None:
    """A committed write stays a success when pg_notify fails; tabs refetch on reconnect."""
    row = _row()
    write_db.get = AsyncMock(return_value=row)
    notify.side_effect = OSError("pg down")
    path = "/api/admin/announcements" + ("" if method == "POST" else f"/{row.id}")

    resp = await admin_client.request(method, path, json=None if method == "DELETE" else VALID_BODY)

    assert resp.status_code in (200, 201, 204)
    write_db.commit.assert_awaited_once()
    notify.assert_awaited_once_with(CHANGED, user_id=None)
