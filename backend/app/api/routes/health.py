"""
Health check routes.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db
from app.models.schemas import HealthResponse, ReadyResponse

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
        raise HTTPException(
            status_code=503,
            detail=f"Database not ready: {str(e)}",
        )
