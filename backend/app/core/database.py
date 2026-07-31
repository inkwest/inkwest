"""
Async database configuration using SQLAlchemy 2.0.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

settings = get_settings()

# Create async engine with asyncpg
# SQL logging is handled via SQLALCHEMY_LOG_LEVEL env var through structured logging
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=300,
    pool_timeout=10,
    # Pin search_path at connection startup (asyncpg sends it in the startup packet)
    # so unqualified table names resolve to the app's schema regardless of the DB
    # role name — a prod role such as `app_<name>` != schema `inkwest`, so the default
    # `"$user", public` misses it. `public` kept for extensions/functions homed there.
    connect_args={"server_settings": {"search_path": f"{settings.db_schema},public"}},
)

# Create async session maker
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Get database session dependency for FastAPI."""
    async with async_session_maker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Initialize database connection on startup."""
    # Import models to ensure they are registered with the Base
    from app.models import Base  # noqa: F401

    # For development, we could create tables here, but in production
    # we should use Alembic migrations
    # async with engine.begin() as conn:
    #     await conn.run_sync(Base.metadata.create_all)
    pass


async def close_db() -> None:
    """Close database connections on shutdown."""
    await engine.dispose()
