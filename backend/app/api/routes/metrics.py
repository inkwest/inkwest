"""
Read-only resource metrics for a conversation's sandbox (the session "Monitoring" tab).

Returns a single point-in-time snapshot — CPU / memory / network / block-IO for the
conversation + proxy containers, plus the durable dir's disk footprint. The frontend
polls this on an interval and keeps its own rolling window; there is no server-side
history (matching the ephemeral, idle-reaped sandbox lifecycle). See
:mod:`app.services.sandbox_metrics`.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_db
from app.models.session import Session
from app.models.user import User
from app.services import sandbox_metrics

router = APIRouter(prefix="/sessions", tags=["metrics"])


class ContainerStats(BaseModel):
    cpu_pct: float
    mem_bytes: float
    mem_limit_bytes: float
    net_rx_bytes: float
    net_tx_bytes: float
    blk_read_bytes: float
    blk_write_bytes: float


class MetricsSnapshot(BaseModel):
    ts: float
    status: str  # disabled | not_running | running
    conv: ContainerStats | None = None
    proxy: ContainerStats | None = None
    disk_bytes: float | None = None
    disk_total_bytes: float | None = None  # filesystem capacity — the disk chart's ceiling


async def _verify_session_owner(db: AsyncSession, session_id: str, user: User) -> None:
    """Ensure the session exists and belongs to the user (admins may view any)."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid session id") from e
    session = (await db.execute(select(Session).where(Session.id == sid))).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not user.is_admin() and session.user_id is not None and session.user_id != user.id:
        raise HTTPException(status_code=403, detail="You do not have access to this session")


@router.get("/{session_id}/metrics", response_model=MetricsSnapshot)
async def get_session_metrics(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> MetricsSnapshot:
    """Point-in-time resource snapshot for a conversation's sandbox containers."""
    await _verify_session_owner(db, session_id, current_user)
    return MetricsSnapshot(**await sandbox_metrics.sample(session_id))
