"""Tests for API dependency functions."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_get_current_user_dev_mode() -> None:
    """In dev mode, returns dev user."""
    from app.api.dependencies import get_current_user

    mock_db = AsyncMock()
    dev_user = MagicMock()

    with (
        patch("app.api.dependencies.get_settings") as mock_settings,
        patch(
            "app.api.dependencies.get_or_create_dev_user",
            return_value=dev_user,
        ),
    ):
        settings = MagicMock()
        settings.oidc_enabled = False
        mock_settings.return_value = settings

        user = await get_current_user(mock_db)

    assert user is dev_user


@pytest.mark.asyncio
async def test_get_current_user_oidc_missing_auth() -> None:
    """In OIDC mode without Authorization header, raises 401."""
    from app.api.dependencies import get_current_user

    mock_db = AsyncMock()

    with patch("app.api.dependencies.get_settings") as mock_settings:
        settings = MagicMock()
        settings.oidc_enabled = True
        mock_settings.return_value = settings

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(mock_db, authorization=None)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_admin_passes_for_admin() -> None:
    """Admin user passes require_admin."""
    from app.api.dependencies import require_admin

    user = MagicMock()
    user.is_admin.return_value = True

    result = await require_admin(user)
    assert result is user


@pytest.mark.asyncio
async def test_require_admin_rejects_non_admin() -> None:
    """Non-admin user gets 403."""
    from app.api.dependencies import require_admin

    user = MagicMock()
    user.is_admin.return_value = False

    with pytest.raises(HTTPException) as exc_info:
        await require_admin(user)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_check_cluster_access_admin_allowed() -> None:
    """Admin users bypass cluster access checks."""
    from app.api.dependencies import check_cluster_access

    admin = MagicMock()
    admin.is_admin.return_value = True
    mock_db = AsyncMock()

    with patch(
        "app.api.dependencies.get_user_allowed_cluster_ids",
        return_value=None,
    ):
        await check_cluster_access(admin, "any-cluster", mock_db)


@pytest.mark.asyncio
async def test_check_cluster_access_denied() -> None:
    """User without cluster in allowed set gets 403."""
    from app.api.dependencies import check_cluster_access

    user = MagicMock()
    mock_db = AsyncMock()

    with patch(
        "app.api.dependencies.get_user_allowed_cluster_ids",
        return_value={"cluster-a", "cluster-b"},
    ):
        with pytest.raises(HTTPException) as exc_info:
            await check_cluster_access(user, "cluster-c", mock_db)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_check_cluster_access_allowed() -> None:
    """User with cluster in allowed set passes."""
    from app.api.dependencies import check_cluster_access

    user = MagicMock()
    mock_db = AsyncMock()

    with patch(
        "app.api.dependencies.get_user_allowed_cluster_ids",
        return_value={"cluster-a", "cluster-b"},
    ):
        await check_cluster_access(user, "cluster-a", mock_db)


@pytest.mark.asyncio
async def test_get_user_allowed_cluster_ids_admin() -> None:
    """Admin gets None (unrestricted)."""
    from app.api.dependencies import get_user_allowed_cluster_ids

    admin = MagicMock()
    admin.is_admin.return_value = True
    admin.roles = ["admin"]
    mock_db = AsyncMock()

    result = await get_user_allowed_cluster_ids(admin, mock_db)
    assert result is None


@pytest.mark.asyncio
async def test_get_user_allowed_cluster_ids_no_mappings() -> None:
    """No mappings configured = unrestricted access."""
    from app.api.dependencies import get_user_allowed_cluster_ids

    user = MagicMock()
    user.is_admin.return_value = False
    user.roles = ["viewer"]
    mock_db = AsyncMock()

    # First query: no mappings for user's roles
    empty_result = MagicMock()
    empty_result.scalars.return_value.all.return_value = []
    # Second query: no mappings exist at all
    no_global = MagicMock()
    no_global.scalar_one_or_none.return_value = None
    mock_db.execute = AsyncMock(side_effect=[empty_result, no_global])

    result = await get_user_allowed_cluster_ids(user, mock_db)
    assert result is None


@pytest.mark.asyncio
async def test_get_user_allowed_cluster_ids_with_mappings() -> None:
    """Returns allowed cluster IDs from role mappings."""
    from app.api.dependencies import get_user_allowed_cluster_ids

    user = MagicMock()
    user.is_admin.return_value = False
    user.roles = ["viewer"]
    mock_db = AsyncMock()

    cid1 = uuid.uuid4()
    mapping = MagicMock()
    mapping.allowed_clusters = [cid1]

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [mapping]
    mock_db.execute = AsyncMock(return_value=result_mock)

    result = await get_user_allowed_cluster_ids(user, mock_db)
    assert result == {str(cid1)}
