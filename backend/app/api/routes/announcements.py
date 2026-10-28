"""
Announcement banner routes (spec 004).

Any signed-in user reads the live banners; admins manage them. Every committed write
broadcasts ``announcements_changed`` on the sync channel so open tabs refetch.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.api.dependencies import AdminUser, CurrentUser, get_db
from app.core.logging import get_logger
from app.models.announcement import Announcement
from app.models.schemas import (
    AnnouncementAdmin,
    AnnouncementCreate,
    AnnouncementPublic,
    AnnouncementWrite,
)
from app.services import pubsub

logger = get_logger(__name__)

router = APIRouter(prefix="/announcements", tags=["announcements"])
admin_router = APIRouter(prefix="/admin/announcements", tags=["admin"])


def active_announcements_query() -> Select[tuple[Announcement]]:
    """Enabled and unexpired banners, oldest first."""
    return (
        select(Announcement)
        .where(
            Announcement.enabled,
            or_(Announcement.ends_at.is_(None), Announcement.ends_at > func.now()),
        )
        .order_by(Announcement.created_at.asc())
    )


async def _broadcast_change() -> None:
    """Tell every connection to refetch; best effort, the write is already committed."""
    try:
        await pubsub.notify({"type": "announcements_changed"}, user_id=None)
    except Exception:  # noqa: BLE001 — broadcast is advisory; never fail the write on it
        logger.warning("announcements_broadcast_failed", exc_info=True)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Announcement not found")


async def _get_or_404(db: AsyncSession, announcement_id: UUID) -> Announcement:
    announcement = await db.get(Announcement, announcement_id)
    if announcement is None:
        raise _not_found()
    return announcement


@router.get("", response_model=list[AnnouncementPublic])
async def list_active_announcements(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[AnnouncementPublic]:
    rows = (await db.execute(active_announcements_query())).scalars().all()
    return [AnnouncementPublic.model_validate(r) for r in rows]


@admin_router.get("", response_model=list[AnnouncementAdmin])
async def list_announcements(
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> list[AnnouncementAdmin]:
    query = select(Announcement).order_by(Announcement.created_at.desc())
    rows = (await db.execute(query)).scalars().all()
    return [AnnouncementAdmin.model_validate(r) for r in rows]


@admin_router.post("", response_model=AnnouncementAdmin, status_code=status.HTTP_201_CREATED)
async def create_announcement(
    body: AnnouncementCreate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> AnnouncementAdmin:
    announcement = Announcement(**body.model_dump(), created_by=admin.id)
    db.add(announcement)
    await db.flush()
    await db.refresh(announcement)
    response = AnnouncementAdmin.model_validate(announcement)
    await db.commit()

    logger.info("announcement_created", announcement_id=str(response.id), admin_id=str(admin.id))
    await _broadcast_change()
    return response


@admin_router.put("/{announcement_id}", response_model=AnnouncementAdmin)
async def update_announcement(
    announcement_id: UUID,
    body: AnnouncementWrite,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> AnnouncementAdmin:
    announcement = await _get_or_404(db, announcement_id)
    for field, value in body.model_dump().items():
        setattr(announcement, field, value)
    # Bumped even on an identical PUT: it keys per-browser dismissal, an edit must re-show it.
    announcement.updated_at = func.now()
    try:
        await db.flush()
    except StaleDataError:
        # A concurrent DELETE committed between the load and this UPDATE; get_db rolls back.
        raise _not_found() from None
    await db.refresh(announcement)
    response = AnnouncementAdmin.model_validate(announcement)
    await db.commit()

    logger.info(
        "announcement_updated", announcement_id=str(announcement_id), admin_id=str(admin.id)
    )
    await _broadcast_change()
    return response


@admin_router.delete("/{announcement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_announcement(
    announcement_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    announcement = await _get_or_404(db, announcement_id)
    await db.delete(announcement)
    await db.commit()

    logger.info(
        "announcement_deleted", announcement_id=str(announcement_id), admin_id=str(admin.id)
    )
    await _broadcast_change()
