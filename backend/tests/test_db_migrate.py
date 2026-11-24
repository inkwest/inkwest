"""Tests for the migration runner's connection handling (no database needed)."""

import ssl
import sys
from unittest.mock import AsyncMock, MagicMock, call

import asyncpg
import pytest

import db_migrate


def _fake_conn(applied: list[str] | None = None, *, lock_free: bool = True) -> AsyncMock:
    conn = AsyncMock()
    conn.fetch.return_value = [{"version": v} for v in applied or []]
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = lock_free  # pg_try_advisory_lock
    return conn


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_migrate, "DB_WAIT_RETRIES", 3)
    monkeypatch.setattr(db_migrate, "DB_WAIT_DELAY_SECONDS", 0)


def _patch_connect(monkeypatch: pytest.MonkeyPatch, *outcomes: object) -> AsyncMock:
    connect = AsyncMock(side_effect=list(outcomes))
    monkeypatch.setattr(db_migrate.asyncpg, "connect", connect)
    return connect


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(),
        TimeoutError(),
        asyncpg.CannotConnectNowError("the database system is starting up"),
        asyncpg.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
        asyncpg.TooManyConnectionsError("too many clients"),
    ],
    ids=type,
)
async def test_connect_retries_transient_errors_then_succeeds(monkeypatch, error):
    conn = _fake_conn()
    connect = _patch_connect(monkeypatch, error, conn)
    assert await db_migrate.connect_when_ready("postgresql://x") is conn
    assert connect.await_count == 2
    timeouts = {c.kwargs["timeout"] for c in connect.await_args_list}
    assert timeouts == {db_migrate.DB_CONNECT_TIMEOUT_SECONDS}


async def test_connect_gives_up_after_retry_budget(monkeypatch):
    last = ConnectionRefusedError("last")
    connect = _patch_connect(monkeypatch, ConnectionRefusedError(), ConnectionRefusedError(), last)
    with pytest.raises(RuntimeError, match="not reachable after 3 attempts") as exc:
        await db_migrate.connect_when_ready("postgresql://x")
    assert connect.await_count == 3
    assert exc.value.__cause__ is last  # the cause reaches the failure record


async def test_connect_names_the_session(monkeypatch):
    connect = _patch_connect(monkeypatch, _fake_conn())
    await db_migrate.connect_when_ready("postgresql://x")
    settings = connect.await_args.kwargs["server_settings"]
    assert settings == {"application_name": "inkwest-db-migrate"}


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.InvalidPasswordError("bad password"),
        ssl.SSLCertVerificationError("certificate verify failed"),
    ],
    ids=type,
)
async def test_permanent_errors_are_not_retried(monkeypatch, error):
    connect = _patch_connect(monkeypatch, error)
    with pytest.raises(type(error)):
        await db_migrate.connect_when_ready("postgresql://x")
    assert connect.await_count == 1


@pytest.mark.parametrize("argv", [[], ["--rollback"]])
def test_main_exits_1_when_database_never_comes_up(monkeypatch, argv):
    _patch_connect(monkeypatch, *[ConnectionRefusedError()] * 3)
    monkeypatch.setattr(db_migrate, "setup_logging", lambda: None)  # adds root handlers per call
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x")
    monkeypatch.setattr(sys, "argv", ["db_migrate.py", *argv])
    with pytest.raises(SystemExit) as exc:
        db_migrate.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("run", [db_migrate.migrate, db_migrate.rollback])
async def test_schema_changes_take_the_lock_first_and_close(monkeypatch, run):
    conn = _fake_conn()
    _patch_connect(monkeypatch, conn)
    monkeypatch.setattr(db_migrate, "get_migration_files", lambda: [])
    await run("postgresql://x")
    assert conn.mock_calls[0] == call.fetchval(
        "SELECT pg_try_advisory_lock($1)", db_migrate._MIGRATION_LOCK_ID
    )
    conn.close.assert_awaited_once()


async def test_busy_lock_is_awaited_under_a_bounded_lock_timeout(monkeypatch):
    conn = _fake_conn(lock_free=False)
    conn.fetchrow.return_value = {"pid": 42, "application_name": "psql", "client_addr": None}
    _patch_connect(monkeypatch, conn)
    monkeypatch.setattr(db_migrate, "get_migration_files", lambda: [])
    log = MagicMock()
    monkeypatch.setattr(db_migrate, "logger", log)
    await db_migrate.migrate("postgresql://x")
    log.info.assert_any_call(
        "migration_lock_wait",
        timeout_s=db_migrate.LOCK_TIMEOUT_SECONDS,
        holder_pid="42",
        holder_application_name="psql",
    )
    assert [c.args[0] for c in conn.execute.await_args_list[:3]] == [
        f"SET lock_timeout = '{db_migrate.LOCK_TIMEOUT_SECONDS}s'",
        "SELECT pg_advisory_lock($1)",
        "RESET lock_timeout",
    ]


async def test_holder_lookup_failure_still_waits_for_the_lock(monkeypatch):
    conn = _fake_conn(lock_free=False)
    conn.fetchrow.side_effect = asyncpg.InsufficientPrivilegeError("permission denied for pg_locks")
    _patch_connect(monkeypatch, conn)
    monkeypatch.setattr(db_migrate, "get_migration_files", lambda: [])
    assert await db_migrate.migrate("postgresql://x") == 0
    executed = [c.args[0] for c in conn.execute.await_args_list]
    assert "SELECT pg_advisory_lock($1)" in executed


def test_main_exits_1_when_the_lock_wait_times_out(monkeypatch):
    conn = _fake_conn(lock_free=False)

    async def execute(sql, *args):
        if sql.startswith("SELECT pg_advisory_lock"):
            raise asyncpg.LockNotAvailableError("canceling statement due to lock timeout")

    conn.execute.side_effect = execute
    _patch_connect(monkeypatch, conn)
    monkeypatch.setattr(db_migrate, "setup_logging", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setattr(sys, "argv", ["db_migrate.py"])
    with pytest.raises(SystemExit) as exc:
        db_migrate.main()
    assert exc.value.code == 1
    # No RESET on the failed session: it is closed, and a RESET there would mask the error.
    assert conn.execute.await_args_list[-1].args[0] == "SELECT pg_advisory_lock($1)"
    conn.close.assert_awaited_once()


async def test_lock_connection_closed_when_migration_fails(monkeypatch):
    conn = _fake_conn()
    conn.fetch.side_effect = RuntimeError("boom")
    _patch_connect(monkeypatch, conn)
    with pytest.raises(RuntimeError, match="boom"):
        await db_migrate.migrate("postgresql://x")
    conn.close.assert_awaited_once()
