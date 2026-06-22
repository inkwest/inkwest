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


async def test_aws_options_progress_idle_when_no_enumeration(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    from app.api.routes.connectors import _aws_enum_progress

    _aws_enum_progress.pop((str(mock_user.id), str(mock_session_obj.id)), None)
    with patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS):
        resp = await authenticated_client.get(
            f"/api/connectors/{mock_session_obj.id}/aws/options/progress"
        )
    assert resp.status_code == 200
    assert resp.json() == {"phase": "idle", "done": 0, "total": 0}


async def test_aws_options_progress_reports_live_enumeration(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    from app.api.routes.connectors import _aws_enum_progress

    key = (str(mock_user.id), str(mock_session_obj.id))
    _aws_enum_progress[key] = {"phase": "enumerating", "done": 12, "total": 47}
    try:
        with patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS):
            resp = await authenticated_client.get(
                f"/api/connectors/{mock_session_obj.id}/aws/options/progress"
            )
    finally:
        _aws_enum_progress.pop(key, None)
    assert resp.status_code == 200
    assert resp.json() == {"phase": "enumerating", "done": 12, "total": 47}


def test_list_accounts_and_roles_reports_progress() -> None:
    """The enumeration fires progress_cb once per account (total known up front)."""
    from app.services.connectors import aws

    def paginator(op: str) -> MagicMock:
        page = (
            {"accountList": [{"accountId": "1"}, {"accountId": "2"}]}
            if op == "list_accounts"
            else {"roleList": [{"roleName": "ReadOnly"}]}
        )
        return MagicMock(paginate=lambda **kw: [page])

    sso = MagicMock()
    sso.get_paginator.side_effect = paginator
    calls: list[tuple[int, int]] = []
    with patch("boto3.client", return_value=sso):
        accounts, roles = aws._list_accounts_and_roles(
            "tok", "eu-west-1", lambda done, total: calls.append((done, total))
        )
    assert [a["account_id"] for a in accounts] == ["1", "2"]
    assert len(roles) == 2
    # (0, 2) at start (ListAccounts done), then one tick per completed account.
    assert calls[0] == (0, 2)
    assert calls[-1] == (2, 2)


def test_end_enum_progress_only_pops_own_token() -> None:
    """A stale _end_enum_progress must not yank a newer enumeration's entry."""
    from app.api.routes import connectors as c

    key = ("u", "s")
    t1, _ = c._begin_enum_progress(key)
    t2, cb2 = c._begin_enum_progress(key)  # a second enumeration took over the key
    c._end_enum_progress(key, t1)  # the first one ending late must be a no-op
    assert key in c._aws_enum_progress
    cb2(3, 5)
    assert c._aws_enum_progress[key]["done"] == 3
    c._end_enum_progress(key, t2)
    assert key not in c._aws_enum_progress


async def test_aws_options_refresh_clears_progress_on_error(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    from app.api.routes import connectors as c

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(
        type="aws", enumerate_accounts_roles=AsyncMock(side_effect=RuntimeError("boto boom"))
    )
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/options/refresh"
        )
    assert resp.status_code == 502
    # The finally must have dropped the entry even though enumeration raised.
    assert (str(mock_user.id), str(mock_session_obj.id)) not in c._aws_enum_progress


async def test_aws_options_refresh_clears_progress_on_success(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    from app.api.routes import connectors as c

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(type="aws", enumerate_accounts_roles=AsyncMock(return_value=([], [])))
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch("app.api.routes.connectors.update_aws_warm_options", AsyncMock()),
    ):
        resp = await authenticated_client.post(
            f"/api/connectors/{mock_session_obj.id}/aws/options/refresh"
        )
    assert resp.status_code == 200
    assert (str(mock_user.id), str(mock_session_obj.id)) not in c._aws_enum_progress


async def test_aws_options_progress_rejects_malformed_session_id(
    authenticated_client: AsyncClient,
) -> None:
    with patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS):
        resp = await authenticated_client.get("/api/connectors/not-a-uuid/aws/options/progress")
    assert resp.status_code == 400


async def test_aws_device_poll_enumeration_failure_is_terminal(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """A boto failure during enumeration must drop the spent device code, not 500."""
    from app.api.routes import connectors as c

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    key = (str(mock_user.id), str(mock_session_obj.id))
    c._aws_device_pending[key] = "device-code-xyz"
    conn = MagicMock(
        type="aws",
        poll_device_token=AsyncMock(return_value={"access_token": "t", "expires_in": 3600}),
        provisional_from_token=AsyncMock(side_effect=RuntimeError("boto boom")),
    )
    store = MagicMock(get=AsyncMock(return_value=None))
    try:
        with (
            patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
            patch("app.api.routes.connectors.get_connector", return_value=conn),
            patch("app.api.routes.connectors.get_credential_store", return_value=store),
        ):
            resp = await authenticated_client.post(
                f"/api/connectors/{mock_session_obj.id}/aws/device/poll"
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"
        # Spent device code dropped → next poll won't re-redeem it.
        assert key not in c._aws_device_pending
        # Progress entry cleaned up by the finally even though enumeration raised.
        assert key not in c._aws_enum_progress
    finally:
        c._aws_device_pending.pop(key, None)
        c._aws_enum_progress.pop(key, None)


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
