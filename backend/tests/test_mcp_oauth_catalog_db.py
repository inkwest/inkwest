"""Real-engine tests for the DB-backed OAuth2 MCP catalog (app.services.mcp_oauth_catalog).

The pure parser is covered in test_mcp_oauth_catalog.py. Here we exercise the actual SQL
against an in-memory SQLite engine: env seeding, the enabled-only in-memory snapshot, the
secret-free admin listing, and the upsert/delete writes (incl. the secret-preserving
ON CONFLICT path).
"""

import base64
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

import app.models  # noqa: F401  -- registers all tables on Base.metadata
from app.core.secretbox import KEY_SIZE, is_sealed
from app.models.base import Base
from app.models.mcp_oauth_catalog import McpOauthCatalog
from app.services import mcp_oauth_catalog


@compiles(JSONB, "sqlite")
def _render_jsonb_on_sqlite(type_: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


def _b64_catalog(entries: dict[str, dict]) -> str:
    return json.dumps(
        {k: base64.b64encode(json.dumps(v).encode()).decode() for k, v in entries.items()}
    )


# 32 zero bytes — a valid (test-only) SecretBox key under version 1.
TEST_KEYRING = json.dumps({"1": base64.b64encode(b"\x00" * KEY_SIZE).decode()})


@pytest.fixture(autouse=True)
def _reset_snapshot():
    mcp_oauth_catalog._servers = {}
    yield
    mcp_oauth_catalog._servers = {}


@pytest.fixture(autouse=True)
def notify_mock(monkeypatch):
    """Capture (and neutralize) the catalog-change broadcast: upsert/delete call
    ``pubsub.notify``, which otherwise opens a real DB connection via the app engine —
    unwanted under the in-memory SQLite test engine. Yields the mock for assertions."""
    mock = AsyncMock()
    monkeypatch.setattr(mcp_oauth_catalog.pubsub, "notify", mock)
    return mock


@pytest.fixture(autouse=True)
def settings_stub(monkeypatch):
    """Stub the module's get_settings with a mutable object carrying both the env catalog
    and a valid secretbox keyring (so sealing works in tests by default)."""
    stub = SimpleNamespace(mcp_catalog_oauth2="", secretbox_keys=TEST_KEYRING)
    monkeypatch.setattr(mcp_oauth_catalog, "get_settings", lambda: stub)
    return stub


@pytest.fixture
def _env(settings_stub):
    """Set the seeded MCP_CATALOG_OAUTH2 env value for a test."""

    def _set(raw: str) -> None:
        settings_stub.mcp_catalog_oauth2 = raw

    return _set


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = cast("list[Table]", [McpOauthCatalog.__table__])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_seed_from_env_on_first_load(db: AsyncSession, _env) -> None:
    _env(_b64_catalog({"notion": {"type": "http", "url": "https://m.example.com/mcp"}}))
    await mcp_oauth_catalog.load_oauth_catalog(db)

    # Seeded into the table AND mirrored into the enabled snapshot.
    rows = (await db.execute(McpOauthCatalog.__table__.select())).mappings().all()
    assert [r["key"] for r in rows] == ["notion"]
    assert mcp_oauth_catalog.oauth_catalog_keys() == ["notion"]
    assert mcp_oauth_catalog.get_oauth_server("notion").url == "https://m.example.com/mcp"


@pytest.mark.asyncio
async def test_seed_runs_once_not_on_every_load(db: AsyncSession, _env) -> None:
    _env(_b64_catalog({"notion": {"type": "http", "url": "https://m.example.com/mcp"}}))
    await mcp_oauth_catalog.load_oauth_catalog(db)
    # Second load with a different env must NOT re-seed (table is non-empty → authoritative).
    _env(_b64_catalog({"slack": {"type": "http", "url": "https://s.example.com/mcp"}}))
    await mcp_oauth_catalog.load_oauth_catalog(db)
    assert mcp_oauth_catalog.oauth_catalog_keys() == ["notion"]


@pytest.mark.asyncio
async def test_snapshot_is_enabled_only(db: AsyncSession, _env) -> None:
    _env("")  # no seeding
    db.add(McpOauthCatalog(key="on", url="https://a.example.com/mcp", enabled=True))
    db.add(McpOauthCatalog(key="off", url="https://b.example.com/mcp", enabled=False))
    await db.commit()
    await mcp_oauth_catalog.load_oauth_catalog(db)

    assert mcp_oauth_catalog.oauth_catalog_keys() == ["on"]
    assert mcp_oauth_catalog.get_oauth_server("off") is None


@pytest.mark.asyncio
async def test_list_entries_is_secret_free_and_includes_disabled(db: AsyncSession) -> None:
    db.add(
        McpOauthCatalog(
            key="srv",
            url="https://a.example.com/mcp",
            client_id="cid",
            client_secret="shh",  # noqa: S106  # pragma: allowlist secret — test fixture
            enabled=False,
        )
    )
    await db.commit()

    entries = await mcp_oauth_catalog.list_oauth_entries(db)
    assert len(entries) == 1
    e = entries[0]
    assert e["key"] == "srv" and e["enabled"] is False  # disabled row still listed
    assert e["has_secret"] is True
    assert "client_secret" not in e  # the raw secret never leaves the server


@pytest.mark.asyncio
async def test_upsert_creates_and_refreshes_snapshot(db: AsyncSession) -> None:
    await mcp_oauth_catalog.upsert_oauth_server(
        db, key="gh", url="https://gh.example.com/mcp", scopes=["repo"], enabled=True
    )
    # Snapshot refreshed in-place (no reload call needed by the caller).
    assert mcp_oauth_catalog.get_oauth_server("gh").scopes == ["repo"]
    # Persisted (fresh load proves DB, not just the mirror).
    await mcp_oauth_catalog.load_oauth_catalog(db)
    assert "gh" in mcp_oauth_catalog.get_oauth_catalog()


@pytest.mark.asyncio
async def test_upsert_without_secret_preserves_stored_one(db: AsyncSession) -> None:
    await mcp_oauth_catalog.upsert_oauth_server(
        db,
        key="gh",
        url="https://gh.example.com/mcp",
        client_secret="topsecret",  # noqa: S106  # pragma: allowlist secret — test fixture
    )
    # An edit that doesn't resend the secret (panel never receives it back) keeps it.
    await mcp_oauth_catalog.upsert_oauth_server(
        db, key="gh", url="https://gh2.example.com/mcp", client_secret=None
    )
    entries = await mcp_oauth_catalog.list_oauth_entries(db)
    assert entries[0]["url"] == "https://gh2.example.com/mcp"
    assert entries[0]["has_secret"] is True


@pytest.mark.asyncio
async def test_upsert_rejects_non_https_url(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="https"):
        await mcp_oauth_catalog.upsert_oauth_server(db, key="x", url="http://insecure/mcp")


@pytest.mark.asyncio
async def test_upsert_seals_secret_at_rest_and_unseals_in_snapshot(db: AsyncSession) -> None:
    await mcp_oauth_catalog.upsert_oauth_server(
        db,
        key="gh",
        url="https://gh.example.com/mcp",
        client_id="cid",
        client_secret="topsecret",  # noqa: S106  # pragma: allowlist secret — test fixture
    )
    # At rest: the column holds a sealed token, never the plaintext.
    row = (await db.execute(McpOauthCatalog.__table__.select())).mappings().one()
    assert is_sealed(row["client_secret"])
    assert "topsecret" not in row["client_secret"]
    # In RAM: the snapshot exposes the decrypted secret for the broker to use.
    assert (
        mcp_oauth_catalog.get_oauth_server("gh").client_secret
        == "topsecret"  # pragma: allowlist secret — test fixture
    )


@pytest.mark.asyncio
async def test_upsert_secret_without_keyring_raises(db: AsyncSession, settings_stub) -> None:
    settings_stub.secretbox_keys = ""  # no encryption key configured
    with pytest.raises(ValueError, match="SECRETBOX_KEYS"):
        await mcp_oauth_catalog.upsert_oauth_server(
            db,
            key="gh",
            url="https://gh.example.com/mcp",
            client_secret="x",  # noqa: S106  # pragma: allowlist secret — test fixture
        )


@pytest.mark.asyncio
async def test_load_drops_unsealable_row_keeps_rest(db: AsyncSession) -> None:
    # A row whose secret can't be unsealed (here: looks sealed but isn't valid) must be
    # dropped from the snapshot, not crash the whole load (rotation/config resilience).
    db.add(McpOauthCatalog(key="ok", url="https://a.example.com/mcp", enabled=True))
    db.add(
        McpOauthCatalog(
            key="broken",
            url="https://b.example.com/mcp",
            client_secret="AAABnot-real-sealed-data",  # noqa: S106  # pragma: allowlist secret
            enabled=True,
        )
    )
    await db.commit()
    await mcp_oauth_catalog.load_oauth_catalog(db)
    assert mcp_oauth_catalog.oauth_catalog_keys() == ["ok"]  # "broken" dropped, no crash


@pytest.mark.asyncio
async def test_list_marks_unreadable_secret_not_editable(db: AsyncSession) -> None:
    db.add(McpOauthCatalog(key="ok", url="https://a.example.com/mcp", enabled=True))
    db.add(
        McpOauthCatalog(
            key="locked",
            url="https://b.example.com/mcp",
            client_secret="AAABnot-real-sealed-data",  # noqa: S106  # pragma: allowlist secret
            enabled=True,
        )
    )
    await db.commit()
    entries = {e["key"]: e for e in await mcp_oauth_catalog.list_oauth_entries(db)}
    assert entries["ok"]["editable"] is True  # no/plaintext secret → editable
    assert entries["locked"]["editable"] is False  # sealed, can't decrypt → blocked


@pytest.mark.asyncio
async def test_seed_drops_secret_without_keyring(db: AsyncSession, _env, settings_stub) -> None:
    # No keyring + a seeded secret → store the row WITHOUT the secret (never clear text).
    settings_stub.secretbox_keys = ""
    _env(
        _b64_catalog(
            {
                "srv": {
                    "type": "http",
                    "url": "https://m.example.com/mcp",
                    "client_secret": "plain",  # pragma: allowlist secret
                }
            }
        )
    )
    await mcp_oauth_catalog.load_oauth_catalog(db)
    row = (await db.execute(McpOauthCatalog.__table__.select())).mappings().one()
    assert row["key"] == "srv"
    assert row["client_secret"] is None  # secret dropped, not stored in clear text


@pytest.mark.asyncio
async def test_load_drops_cleartext_secret_row(db: AsyncSession) -> None:
    # A clear-text (non-sealed) secret is invalid — the server is dropped from the snapshot.
    db.add(McpOauthCatalog(key="ok", url="https://a.example.com/mcp", enabled=True))
    db.add(
        McpOauthCatalog(
            key="plain",
            url="https://b.example.com/mcp",
            client_secret="cleartext",  # noqa: S106  # pragma: allowlist secret
            enabled=True,
        )
    )
    await db.commit()
    await mcp_oauth_catalog.load_oauth_catalog(db)
    assert mcp_oauth_catalog.oauth_catalog_keys() == ["ok"]


@pytest.mark.asyncio
async def test_secret_storage_enabled_reflects_keyring(settings_stub) -> None:
    assert mcp_oauth_catalog.secret_storage_enabled() is True  # TEST_KEYRING
    settings_stub.secretbox_keys = ""
    assert mcp_oauth_catalog.secret_storage_enabled() is False
    settings_stub.secretbox_keys = "{not valid json"  # malformed → degraded → disabled
    assert mcp_oauth_catalog.secret_storage_enabled() is False


@pytest.mark.asyncio
async def test_create_only_rejects_duplicate_key(db: AsyncSession) -> None:
    await mcp_oauth_catalog.upsert_oauth_server(db, key="gh", url="https://gh.example.com/mcp")
    with pytest.raises(mcp_oauth_catalog.DuplicateKeyError):
        await mcp_oauth_catalog.upsert_oauth_server(
            db, key="gh", url="https://other.example.com/mcp", create_only=True
        )


@pytest.mark.asyncio
async def test_upsert_rejects_invalid_key(db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="key must"):
        await mcp_oauth_catalog.upsert_oauth_server(db, key="bad/key", url="https://x.example/mcp")


@pytest.mark.asyncio
async def test_seed_seals_secret_when_keyring_present(db: AsyncSession, _env) -> None:
    _env(
        _b64_catalog(
            {
                "withsecret": {
                    "type": "http",
                    "url": "https://m.example.com/mcp",
                    "client_secret": "seedsecret",  # pragma: allowlist secret
                }
            }
        )
    )
    await mcp_oauth_catalog.load_oauth_catalog(db)  # seeds from env
    row = (await db.execute(McpOauthCatalog.__table__.select())).mappings().one()
    assert is_sealed(row["client_secret"])  # sealed at seed, not plaintext at rest
    assert (
        mcp_oauth_catalog.get_oauth_server("withsecret").client_secret
        == "seedsecret"  # pragma: allowlist secret — test fixture
    )


@pytest.mark.asyncio
async def test_delete_removes_and_unknown_raises(db: AsyncSession) -> None:
    await mcp_oauth_catalog.upsert_oauth_server(db, key="gh", url="https://gh.example.com/mcp")
    await mcp_oauth_catalog.delete_oauth_server(db, "gh")
    assert mcp_oauth_catalog.get_oauth_server("gh") is None

    with pytest.raises(ValueError, match="Unknown MCP server key"):
        await mcp_oauth_catalog.delete_oauth_server(db, "nope")


@pytest.mark.asyncio
async def test_upsert_broadcasts_catalog_change(db: AsyncSession, notify_mock) -> None:
    await mcp_oauth_catalog.upsert_oauth_server(db, key="gh", url="https://gh.example.com/mcp")
    notify_mock.assert_awaited_once_with({"type": "mcp_catalog_changed"}, user_id=None)


@pytest.mark.asyncio
async def test_delete_broadcasts_catalog_change(db: AsyncSession, notify_mock) -> None:
    await mcp_oauth_catalog.upsert_oauth_server(db, key="gh", url="https://gh.example.com/mcp")
    notify_mock.reset_mock()  # ignore the create's broadcast; assert the delete's alone
    await mcp_oauth_catalog.delete_oauth_server(db, "gh")
    notify_mock.assert_awaited_once_with({"type": "mcp_catalog_changed"}, user_id=None)


@pytest.mark.asyncio
async def test_delete_unknown_key_does_not_broadcast(db: AsyncSession, notify_mock) -> None:
    # A no-op delete raises before the snapshot refresh — nothing changed, so no broadcast.
    with pytest.raises(ValueError, match="Unknown MCP server key"):
        await mcp_oauth_catalog.delete_oauth_server(db, "nope")
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_upsert_does_not_broadcast(db: AsyncSession, notify_mock) -> None:
    # A rejected write (duplicate under create_only, invalid url) must not signal a change.
    await mcp_oauth_catalog.upsert_oauth_server(db, key="gh", url="https://gh.example.com/mcp")
    notify_mock.reset_mock()
    with pytest.raises(mcp_oauth_catalog.DuplicateKeyError):
        await mcp_oauth_catalog.upsert_oauth_server(
            db, key="gh", url="https://gh.example.com/mcp", create_only=True
        )
    with pytest.raises(ValueError, match="https"):
        await mcp_oauth_catalog.upsert_oauth_server(db, key="bad", url="http://insecure/mcp")
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_fail_the_write(db: AsyncSession, notify_mock) -> None:
    # A transient broadcast failure must not surface as a failed admin write — the row is
    # already committed and clients also revalidate on mount/popup-open (SWR safety net).
    notify_mock.side_effect = RuntimeError("pg down")
    await mcp_oauth_catalog.upsert_oauth_server(db, key="gh", url="https://gh.example.com/mcp")
    assert mcp_oauth_catalog.get_oauth_server("gh") is not None
    notify_mock.assert_awaited_once()
