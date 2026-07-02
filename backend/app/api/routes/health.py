"""
Health check routes.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.models.schemas import HealthResponse, ReadyResponse, VersionResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """
    Basic health check endpoint.

    Returns a simple healthy status if the service is running.
    """
    return HealthResponse(status="healthy")


@router.get("/health/ready", response_model=ReadyResponse)
async def ready(db: AsyncSession = Depends(get_db)) -> ReadyResponse:
    """
    Readiness check endpoint.

    Checks database connectivity and returns ready status.
    """
    try:
        # Execute a simple query to verify database connection
        await db.execute(text("SELECT 1"))
        return ReadyResponse(status="ready", database="connected")
    except Exception as e:
        # Static client message — this probe is unauthenticated and public; the raw
        # exception can disclose DB host/driver/auth details. Log it server-side only.
        logger.warning("readiness_check_failed", exc_info=e)
        raise HTTPException(status_code=503, detail="Database not ready") from e


@router.get("/version", response_model=VersionResponse)
async def version() -> VersionResponse:
    """
    Running backend build metadata.

    Unauthenticated and non-sensitive: the SPA polls this on every WebSocket
    reconnect and compares it against the version captured at page load to
    detect a newer deployment and prompt a reload.
    """
    settings = get_settings()
    return VersionResponse(
        version=settings.version,
        commit=settings.commit_hash,
        build_timestamp=settings.build_timestamp,
    )
