#!/usr/bin/env python3
"""
Database migration script for Inkwest.

Uses dbmate-style SQL migrations with -- migrate:up and -- migrate:down markers.
Designed to run as a Kubernetes Job for production deployments.

Usage:
    python db_migrate.py              # Run pending migrations
    python db_migrate.py --status     # Show migration status
    python db_migrate.py --rollback   # Rollback last migration

Environment:
    DATABASE_URL: PostgreSQL connection string (required)
"""

import asyncio
import contextlib
import os
import re
import ssl
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg

from app.core.logging import get_logger, setup_logging

logger = get_logger("db_migrate")  # not __name__: it is "__main__" when run as a script

# Migration directory relative to this script
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
# Arbitrary: every runner instance only has to pick the same number.
_MIGRATION_LOCK_ID = 8_675_309
SCHEMA_NAME = "inkwest"

# K8s Job: the database may still be starting when the migration Job is scheduled.
DB_WAIT_RETRIES = 30
DB_WAIT_DELAY_SECONDS = 2.0
DB_CONNECT_TIMEOUT_SECONDS = 5.0
# Bounds the wait on another runner holding the migration lock, so a stuck one fails the Job.
LOCK_TIMEOUT_SECONDS = 600
# Who holds the lock, logged while waiting so a stuck holder can be found and killed.
# Advisory locks are per database; objsubid = 1 is the single-bigint key form.
_LOCK_HOLDER_SQL = """
    SELECT a.pid, a.application_name, a.client_addr, a.backend_start
    FROM pg_locks l JOIN pg_stat_activity a USING (pid)
    WHERE l.locktype = 'advisory' AND l.granted
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND l.classid = 0 AND l.objid::bigint = $1 AND l.objsubid = 1
    LIMIT 1
"""
# Transient while the server starts, restarts or is saturated; anything else fails at once.
_RETRYABLE_CONNECT_ERRORS = (
    OSError,  # refused, DNS, timeout
    asyncpg.CannotConnectNowError,
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.TooManyConnectionsError,
)


async def connect_when_ready(database_url: str) -> asyncpg.Connection:
    """Connect, retrying while the database is unreachable or still starting up."""
    last_error: Exception | None = None
    for attempt in range(1, DB_WAIT_RETRIES + 1):
        try:
            conn = await asyncpg.connect(
                database_url,
                timeout=DB_CONNECT_TIMEOUT_SECONDS,
                server_settings={"application_name": "inkwest-db-migrate"},
            )
        except ssl.SSLError:
            raise  # an OSError too, but a TLS misconfiguration does not heal on retry
        except _RETRYABLE_CONNECT_ERRORS as e:
            last_error = e
            logger.info(
                "database_wait",
                attempt=attempt,
                retries=DB_WAIT_RETRIES,
                error_type=type(e).__name__,
                error=str(e),
            )
            if attempt < DB_WAIT_RETRIES:
                await asyncio.sleep(DB_WAIT_DELAY_SECONDS)
        else:
            logger.info("database_ready", attempt=attempt)
            return conn
    raise RuntimeError(f"database not reachable after {DB_WAIT_RETRIES} attempts") from last_error


@contextlib.asynccontextmanager
async def locked_connection(database_url: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection holding the migration lock, so two runners never change the schema at once."""
    conn = await connect_when_ready(database_url)
    try:
        # Session-scoped: a second Job for the same deploy waits here, then finds nothing
        # pending, instead of failing on an already-applied DDL. Released on close.
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", _MIGRATION_LOCK_ID):
            try:
                holder = await conn.fetchrow(_LOCK_HOLDER_SQL, _MIGRATION_LOCK_ID)
            except asyncpg.PostgresError:
                holder = None  # diagnostics only (e.g. no read access to pg_locks): still wait
            logger.info(
                "migration_lock_wait",
                timeout_s=LOCK_TIMEOUT_SECONDS,
                **{f"holder_{k}": str(v) for k, v in (holder or {}).items() if v is not None},
            )
            await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'")
            await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
            # The migrations' own DDL keeps the server default. No finally: on failure the
            # connection is closed anyway, and a RESET there would mask the real error.
            await conn.execute("RESET lock_timeout")
        yield conn
    finally:
        await conn.close()


def parse_migration(content: str, direction: str = "up") -> str:
    """
    Extract SQL from migration file for given direction.

    Migrations use dbmate format:
    -- migrate:up
    CREATE TABLE ...

    -- migrate:down
    DROP TABLE ...
    """
    pattern = rf"--\s*migrate:{direction}\s*\n(.*?)(?=--\s*migrate:|$)"
    match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)

    if not match:
        raise ValueError(f"No -- migrate:{direction} section found")

    return match.group(1).strip()


def get_migration_files() -> list[tuple[str, Path]]:
    """
    Get all migration files sorted by version.

    Files should be named: YYYYMMDDHHMMSS_description.sql
    Returns list of (version, path) tuples.
    """
    if not MIGRATIONS_DIR.exists():
        logger.warning("migrations_dir_missing", path=str(MIGRATIONS_DIR))
        return []

    migrations = []
    for file in MIGRATIONS_DIR.glob("*.sql"):
        # Extract version from filename (timestamp prefix)
        match = re.match(r"^(\d{14})_", file.name)
        if match:
            version = match.group(1)
            migrations.append((version, file))
        else:
            logger.warning("migration_filename_invalid", filename=file.name)

    return sorted(migrations, key=lambda x: x[0])


async def ensure_schema_and_table(conn: asyncpg.Connection) -> None:
    """Create schema and migrations tracking table if they don't exist."""

    # Create schema
    await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")

    # Create migrations tracking table
    await conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_NAME}.schema_migrations (
            version VARCHAR(14) PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    logger.info("schema_ensured", schema=SCHEMA_NAME)


async def get_applied_versions(conn: asyncpg.Connection) -> set[str]:
    """Get set of already applied migration versions."""
    rows = await conn.fetch(f"SELECT version FROM {SCHEMA_NAME}.schema_migrations ORDER BY version")
    return {row["version"] for row in rows}


async def run_migration(
    conn: asyncpg.Connection, version: str, path: Path, direction: str = "up"
) -> None:
    """Run a single migration."""
    content = path.read_text()
    sql = parse_migration(content, direction)

    logger.info("migration_start", direction=direction, version=version, migration=path.name)

    async with conn.transaction():
        # Execute the migration SQL
        await conn.execute(sql)

        # Update tracking table
        if direction == "up":
            await conn.execute(
                f"INSERT INTO {SCHEMA_NAME}.schema_migrations (version) VALUES ($1)", version
            )
        else:
            await conn.execute(
                f"DELETE FROM {SCHEMA_NAME}.schema_migrations WHERE version = $1", version
            )

    logger.info("migration_done", direction=direction, version=version, migration=path.name)


async def migrate(database_url: str) -> int:
    """Run all pending migrations. Returns count of migrations run."""
    async with locked_connection(database_url) as conn:
        await ensure_schema_and_table(conn)

        applied = await get_applied_versions(conn)
        migrations = get_migration_files()

        pending = [(v, p) for v, p in migrations if v not in applied]

        counts = {"total": len(migrations), "applied": len(applied)}
        if not pending:
            logger.info("migrations_none_pending", **counts)
            return 0

        logger.info("migrations_pending", count=len(pending), **counts)

        for version, path in pending:
            await run_migration(conn, version, path, "up")

        logger.info("migrations_applied", count=len(pending))
        return len(pending)


async def rollback(database_url: str) -> bool:
    """Rollback the last applied migration. Returns True if rolled back."""
    async with locked_connection(database_url) as conn:
        await ensure_schema_and_table(conn)

        # Get the last applied migration
        row = await conn.fetchrow(f"""
            SELECT version FROM {SCHEMA_NAME}.schema_migrations
            ORDER BY version DESC LIMIT 1
        """)

        if not row:
            logger.info("rollback_none_applied")
            return False

        version = row["version"]
        migrations = get_migration_files()

        # Find the migration file
        migration_path = None
        for v, p in migrations:
            if v == version:
                migration_path = p
                break

        if not migration_path:
            logger.error("migration_file_missing", version=version)
            return False

        await run_migration(conn, version, migration_path, "down")
        logger.info("rollback_done", version=version, migration=migration_path.name)
        return True


async def status(database_url: str) -> None:
    """Show migration status."""
    conn = await connect_when_ready(database_url)

    try:
        await ensure_schema_and_table(conn)

        applied = await get_applied_versions(conn)
        migrations = get_migration_files()

        print("\nMigration Status:")
        print("-" * 60)

        for version, path in migrations:
            status_str = "✅ Applied" if version in applied else "⬜ Pending"
            print(f"  {status_str}  {path.name}")

        if not migrations:
            print("  No migration files found")

        print("-" * 60)
        print(f"Total: {len(migrations)} migrations, {len(applied)} applied\n")

    finally:
        await conn.close()


def main():
    """Main entry point."""
    setup_logging()
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        logger.error("database_url_missing")
        sys.exit(1)

    # Convert SQLAlchemy URL to asyncpg format if needed
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = database_url.replace("postgresql+asyncpg://", "postgresql://")

    # Parse command line arguments
    try:
        if "--status" in sys.argv:
            asyncio.run(status(database_url))
        elif "--rollback" in sys.argv:
            success = asyncio.run(rollback(database_url))
            sys.exit(0 if success else 1)
        else:
            asyncio.run(migrate(database_url))
    except Exception:
        logger.exception("db_migrate_failed")  # traceback inside the JSON record, not raw stderr
        sys.exit(1)


if __name__ == "__main__":
    main()
