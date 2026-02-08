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
import logging
import os
import re
import sys
from pathlib import Path

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Migration directory relative to this script
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SCHEMA_NAME = "inkwest"


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
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return []

    migrations = []
    for file in MIGRATIONS_DIR.glob("*.sql"):
        # Extract version from filename (timestamp prefix)
        match = re.match(r"^(\d{14})_", file.name)
        if match:
            version = match.group(1)
            migrations.append((version, file))
        else:
            logger.warning(f"Skipping invalid migration filename: {file.name}")

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

    logger.info(f"Ensured schema '{SCHEMA_NAME}' and migrations table exist")


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

    logger.info(f"Running migration {direction}: {path.name}")

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

    logger.info(f"Completed migration {direction}: {path.name}")


async def migrate(database_url: str) -> int:
    """Run all pending migrations. Returns count of migrations run."""
    conn = await asyncpg.connect(database_url)

    try:
        await ensure_schema_and_table(conn)

        applied = await get_applied_versions(conn)
        migrations = get_migration_files()

        pending = [(v, p) for v, p in migrations if v not in applied]

        if not pending:
            logger.info("No pending migrations")
            return 0

        logger.info(f"Found {len(pending)} pending migration(s)")

        for version, path in pending:
            await run_migration(conn, version, path, "up")

        logger.info(f"Successfully applied {len(pending)} migration(s)")
        return len(pending)

    finally:
        await conn.close()


async def rollback(database_url: str) -> bool:
    """Rollback the last applied migration. Returns True if rolled back."""
    conn = await asyncpg.connect(database_url)

    try:
        await ensure_schema_and_table(conn)

        # Get the last applied migration
        row = await conn.fetchrow(f"""
            SELECT version FROM {SCHEMA_NAME}.schema_migrations
            ORDER BY version DESC LIMIT 1
        """)

        if not row:
            logger.info("No migrations to rollback")
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
            logger.error(f"Migration file not found for version: {version}")
            return False

        await run_migration(conn, version, migration_path, "down")
        logger.info(f"Rolled back migration: {migration_path.name}")
        return True

    finally:
        await conn.close()


async def status(database_url: str) -> None:
    """Show migration status."""
    conn = await asyncpg.connect(database_url)

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
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        logger.error("DATABASE_URL environment variable is required")
        sys.exit(1)

    # Convert SQLAlchemy URL to asyncpg format if needed
    if database_url.startswith("postgresql+asyncpg://"):
        database_url = database_url.replace("postgresql+asyncpg://", "postgresql://")

    # Parse command line arguments
    if "--status" in sys.argv:
        asyncio.run(status(database_url))
    elif "--rollback" in sys.argv:
        success = asyncio.run(rollback(database_url))
        sys.exit(0 if success else 1)
    else:
        count = asyncio.run(migrate(database_url))
        logger.info(f"Migration complete. {count} migration(s) applied.")


if __name__ == "__main__":
    main()
