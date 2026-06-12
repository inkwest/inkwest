"""Tests for the connector renewal routes (/renewable, /gitlab/renew, /aws/renew-select)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient

from app.services.connectors.base import ConnectorError, ConnectorStatus, MintedCredential
from app.services.connectors.grants import PriorGrant

_SETTINGS = MagicMock(connectors_enabled=True)
_AWS_SEL = {"profiles": [{"account_id": "111", "role_name": "ReadOnly", "name": "acme-ReadOnly"}]}


def test_aws_targets_are_structured() -> None:
    from app.api.routes.connectors import _aws_targets

    cred = MagicMock(
        selection={
            "profiles": [
                {
                    "account_id": "111122223333",
                    "account_name": "acme-prod",
                    "role_name": "ReadOnly",
                },
                {"account_id": "444455556666", "account_name": "acme-dev", "role_name": "Admin"},
            ]
        }
    )
    assert _aws_targets(cred) == [
        {"account_id": "111122223333", "account_name": "acme-prod", "role": "ReadOnly"},
        {"account_id": "444455556666", "account_name": "acme-dev", "role": "Admin"},
    ]


def test_aws_targets_empty_without_selection() -> None:
    from app.api.routes.connectors import _aws_targets

    assert _aws_targets(MagicMock(selection=None)) == []


def _session_lookup(mock_db: AsyncMock, mock_session_obj: MagicMock, owner_id) -> None:
    """Wire the single _verify_session_owner SELECT(Session) to return an owned session."""
    mock_session_obj.user_id = owner_id
    result = MagicMock()
    result.scalar_one_or_none.return_value = mock_session_obj
    mock_db.execute = AsyncMock(return_value=result)


async def test_renewable_lists_prior_grants(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    grants = [PriorGrant("aws", _AWS_SEL), PriorGrant("gitlab", None)]
    store = MagicMock(get=AsyncMock(return_value=None))

    def fake_conn(name: str) -> MagicMock:
        c = MagicMock(type=name)
        c.public_status.return_value = ConnectorStatus(connector=name, state="not_connected")
        return c

    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.derive_prior_grants", AsyncMock(return_value=grants)),
        patch("app.api.routes.connectors.get_connector", side_effect=fake_conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
    ):
        resp = await authenticated_client.get(f"/api/connectors/{mock_session_obj.id}/renewable")

    assert resp.status_code == 200
    by = {d["connector"]: d for d in resp.json()}
    assert set(by) == {"aws", "gitlab"}
    assert by["aws"]["aws_selection"] == _AWS_SEL
    assert by["gitlab"]["aws_selection"] is None
    assert by["aws"]["live"] is False


async def test_renewable_skips_disabled_connector(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch(
            "app.api.routes.connectors.derive_prior_grants",
            AsyncMock(return_value=[PriorGrant("gitlab", None)]),
        ),
        patch("app.api.routes.connectors.get_connector", return_value=None),  # disabled since grant
        patch("app.api.routes.connectors.get_credential_store", return_value=MagicMock()),
    ):
        resp = await authenticated_client.get(f"/api/connectors/{mock_session_obj.id}/renewable")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_gitlab_renew_success(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    renewed = MintedCredential(
        "gitlab", "read", datetime.now(UTC) + timedelta(hours=2), secret={"access_token": "a2"}
    )
    store = MagicMock(put=AsyncMock())
    conn = MagicMock(type="gitlab")
    conn.public_status.return_value = ConnectorStatus(connector="gitlab", state="connected")

    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch(
            "app.api.routes.connectors.grant_gitlab_from_warm", AsyncMock(return_value=renewed)
        ) as warm_grant,
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/gitlab/renew"
        )

    assert resp.status_code == 200
    assert resp.json()["state"] == "connected"
    warm_grant.assert_awaited_once_with(mock_user.id)
    store.put.assert_awaited_once()


async def test_gitlab_renew_409_without_warm_grant(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    conn = MagicMock(type="gitlab")
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.grant_gitlab_from_warm", AsyncMock(return_value=None)),
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/gitlab/renew"
        )
    assert resp.status_code == 409


def _aws_provisional() -> MintedCredential:
    return MintedCredential(
        "aws",
        "read",
        datetime.now(UTC) + timedelta(minutes=10),
        secret={"sso_access_token": "t"},
        selection=None,  # provisional → needs_selection
    )


async def test_aws_renew_select_applies_saved_selection(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    connected = MintedCredential(
        "aws", "read", datetime.now(UTC) + timedelta(hours=1), selection=_AWS_SEL
    )
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(type="aws", select_account_roles=AsyncMock(return_value=connected))
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="connected")

    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch(
            "app.api.routes.connectors.derive_prior_grants",
            AsyncMock(return_value=[PriorGrant("aws", _AWS_SEL)]),
        ),
        patch("app.api.routes.connectors.schedule_session_eks_refresh") as eks,
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/renew-select"
        )

    assert resp.status_code == 200
    assert resp.json()["state"] == "connected"
    _, kwargs = conn.select_account_roles.call_args
    assert kwargs["selections"] == [{"account_id": "111", "role_name": "ReadOnly"}]
    eks.assert_called_once()


async def test_aws_renew_select_409_no_provisional(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=None))
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=MagicMock(type="aws")),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/renew-select"
        )
    assert resp.status_code == 409


async def test_aws_renew_select_409_no_saved_selection(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()))
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=MagicMock(type="aws")),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch("app.api.routes.connectors.derive_prior_grants", AsyncMock(return_value=[])),
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/renew-select"
        )
    assert resp.status_code == 409


async def test_aws_renew_select_400_when_selection_no_longer_valid(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(
        type="aws",
        select_account_roles=AsyncMock(side_effect=ConnectorError("Account/role not authorized")),
    )
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch(
            "app.api.routes.connectors.derive_prior_grants",
            AsyncMock(return_value=[PriorGrant("aws", _AWS_SEL)]),
        ),
        patch("app.api.routes.connectors.schedule_session_eks_refresh"),
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/renew-select"
        )
    assert resp.status_code == 400


def test_prior_grant_response_has_no_secret_field() -> None:
    from app.api.routes.connectors import PriorGrantResponse

    assert "secret" not in PriorGrantResponse.model_fields


# --------------------------------------------------------------------------- #
# grant-from-warm: derive a session credential from the user's warm master auth
# --------------------------------------------------------------------------- #
async def test_grant_from_warm_aws_needs_selection(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    provisional = _aws_provisional()
    store = MagicMock(put=AsyncMock())
    conn = MagicMock(type="aws")
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="needs_selection")

    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch(
            "app.api.routes.connectors.grant_aws_provisional_from_warm",
            AsyncMock(return_value=provisional),
        ) as warm_aws,
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/grant-from-warm"
        )

    assert resp.status_code == 200
    assert resp.json()["state"] == "needs_selection"
    # Keyed by the acting user, not the session owner.
    warm_aws.assert_awaited_once_with(mock_user.id)
    store.put.assert_awaited_once()


async def test_grant_from_warm_gitlab_connected(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    cred = MintedCredential(
        "gitlab", "read", datetime.now(UTC) + timedelta(hours=1), secret={"access_token": "a"}
    )
    store = MagicMock(put=AsyncMock())
    conn = MagicMock(type="gitlab")
    conn.public_status.return_value = ConnectorStatus(connector="gitlab", state="connected")

    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch(
            "app.api.routes.connectors.grant_gitlab_from_warm", AsyncMock(return_value=cred)
        ) as warm_gl,
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/gitlab/grant-from-warm"
        )

    assert resp.status_code == 200
    assert resp.json()["state"] == "connected"
    warm_gl.assert_awaited_once_with(mock_user.id)
    store.put.assert_awaited_once()


async def test_aws_options_refresh_reenumerates_and_updates_cache(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    provisional = _aws_provisional()
    accounts = [{"account_id": "111", "account_name": "acme"}]
    roles = [{"account_id": "111", "role_name": "ReadOnly"}]
    store = MagicMock(get=AsyncMock(return_value=provisional), put=AsyncMock())
    conn = MagicMock(type="aws", enumerate_accounts_roles=AsyncMock(return_value=(accounts, roles)))
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch("app.api.routes.connectors.update_aws_warm_options", AsyncMock()) as upd,
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/options/refresh"
        )
    assert resp.status_code == 200
    assert resp.json() == {"accounts": accounts, "roles": roles}
    conn.enumerate_accounts_roles.assert_awaited_once()
    store.put.assert_awaited_once()  # provisional cred re-stored with fresh lists
    upd.assert_awaited_once_with(mock_user.id, accounts, roles)


async def test_aws_options_refresh_409_when_no_provisional(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=None))
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=MagicMock(type="aws")),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/options/refresh"
        )
    assert resp.status_code == 409


async def test_grant_from_warm_409_without_warm_grant(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    conn = MagicMock(type="gitlab")
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.grant_gitlab_from_warm", AsyncMock(return_value=None)),
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/gitlab/grant-from-warm"
        )
    assert resp.status_code == 409
