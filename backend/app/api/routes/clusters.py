"""
Cluster management routes.

Read operations require authentication, write operations require admin role.
"""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AdminUser,
    CurrentUser,
    check_cluster_access,
    get_db,
    get_user_allowed_cluster_ids,
)
from app.models.cluster import Cluster
from app.models.schemas import (
    ClusterCreate,
    ClusterList,
    ClusterResponse,
    ClusterUpdate,
)

router = APIRouter(prefix="/clusters", tags=["clusters"])


@router.get("", response_model=ClusterList)
async def list_clusters(
    current_user: CurrentUser,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
) -> ClusterList:
    query = select(Cluster)
    if active_only:
        query = query.where(Cluster.is_active == True)  # noqa: E712

    allowed_ids = await get_user_allowed_cluster_ids(current_user, db)
    if allowed_ids is not None:
        if not allowed_ids:
            return ClusterList(items=[], total=0)
        query = query.where(Cluster.id.in_([UUID(cid) for cid in allowed_ids]))

    query = query.order_by(Cluster.name)

    result = await db.execute(query)
    clusters = result.scalars().all()

    return ClusterList(
        items=[ClusterResponse.model_validate(c) for c in clusters],
        total=len(clusters),
    )


@router.post("", response_model=ClusterResponse, status_code=status.HTTP_201_CREATED)
async def create_cluster(
    cluster_data: ClusterCreate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> ClusterResponse:
    kubeconfig = Path(cluster_data.kubeconfig_path)
    if not kubeconfig.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Kubeconfig file not found: {cluster_data.kubeconfig_path}",
        )

    existing = await db.execute(select(Cluster).where(Cluster.name == cluster_data.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cluster with name '{cluster_data.name}' already exists",
        )

    cluster = Cluster(**cluster_data.model_dump())
    db.add(cluster)
    await db.flush()
    await db.refresh(cluster)

    return ClusterResponse.model_validate(cluster)


@router.get("/{cluster_id}", response_model=ClusterResponse)
async def get_cluster(
    cluster_id: UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClusterResponse:
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()

    if not cluster:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cluster with ID '{cluster_id}' not found",
        )

    await check_cluster_access(current_user, str(cluster.id), db)

    return ClusterResponse.model_validate(cluster)


@router.patch("/{cluster_id}", response_model=ClusterResponse)
async def update_cluster(
    cluster_id: UUID,
    cluster_data: ClusterUpdate,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> ClusterResponse:
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()

    if not cluster:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cluster with ID '{cluster_id}' not found",
        )

    update_data = cluster_data.model_dump(exclude_unset=True)
    if "kubeconfig_path" in update_data:
        kubeconfig = Path(update_data["kubeconfig_path"])
        if not kubeconfig.is_file():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Kubeconfig file not found: {update_data['kubeconfig_path']}",
            )

    for field, value in update_data.items():
        setattr(cluster, field, value)

    await db.flush()
    await db.refresh(cluster)

    return ClusterResponse.model_validate(cluster)


@router.delete("/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cluster(
    cluster_id: UUID,
    admin: AdminUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(Cluster).where(Cluster.id == cluster_id))
    cluster = result.scalar_one_or_none()

    if not cluster:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cluster with ID '{cluster_id}' not found",
        )

    cluster.is_active = False
    await db.flush()
