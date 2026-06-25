"""Tests for the connector renewal routes (/renewable, /gitlab/renew, /aws/renew-select)."""

import time
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


async def test_aws_options_progress_serves_done_then_reaps_after_grace(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """A terminal "done" is served while within grace, then reaped to "idle" once it
    expires — this is what lets the picker observe completion without catching the
    active window. The expired read also removes the entry (no leak)."""
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    from app.api.routes.connectors import _aws_enum_progress

    key = (str(mock_user.id), str(mock_session_obj.id))
    url = f"/api/connectors/{mock_session_obj.id}/aws/options/progress"
    try:
        # Within grace → the poll reports "done" so the picker can close.
        _aws_enum_progress[key] = {
            "phase": "done",
            "done": 2,
            "total": 2,
            "token": object(),
            "expires_at": time.monotonic() + 100,
        }
        with patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS):
            resp = await authenticated_client.get(url)
        assert resp.json() == {"phase": "done", "done": 2, "total": 2}

        # Past grace → reaped to "idle" and the entry is dropped on read.
        _aws_enum_progress[key]["expires_at"] = time.monotonic() - 1
        with patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS):
            resp = await authenticated_client.get(url)
        assert resp.json() == {"phase": "idle", "done": 0, "total": 0}
        assert key not in _aws_enum_progress
    finally:
        _aws_enum_progress.pop(key, None)


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


def test_finish_enum_progress_only_finishes_own_token() -> None:
    """A stale discovery settling late must not clobber a newer grant's live entry."""
    from app.api.routes import connectors as c

    key = ("u", "s2")
    try:
        t1, cb1 = c._begin_enum_progress(key, phase="discovering")
        cb1(1, 3)
        t2, _ = c._begin_enum_progress(key, phase="granting")  # a fresh grant took over
        c._finish_enum_progress(key, t1)  # the superseded discovery ending late: no-op
        assert c._aws_enum_progress[key]["phase"] == "granting"
        assert "expires_at" not in c._aws_enum_progress[key]
        # The owning token does flip it to the lingering terminal state.
        c._finish_enum_progress(key, t2)
        assert c._aws_enum_progress[key]["phase"] == "done"
        assert c._aws_enum_progress[key]["expires_at"] > time.monotonic()
    finally:
        c._aws_enum_progress.pop(key, None)


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


# --------------------------------------------------------------------------- #
# OAuth callback: self-closing popup HTML (no full-page redirect)
# --------------------------------------------------------------------------- #
def test_popup_html_escapes_and_self_closes() -> None:
    from app.api.routes import connectors as c

    resp = c._popup_html("<b>x</b> & y", ok=True)
    body = resp.body.decode()
    assert resp.status_code == 200
    assert "window.close()" in body
    assert "&lt;b&gt;x&lt;/b&gt;" in body  # message HTML-escaped (no reflected markup)
    assert "<b>x</b>" not in body


async def test_oauth_callback_invalid_state_returns_self_closing_html(
    authenticated_client: AsyncClient, mock_db: AsyncMock
) -> None:
    """A bad/expired state self-closes the popup (HTML 200), never redirects the page."""
    result = MagicMock(first=MagicMock(return_value=None))  # no pending-state row
    mock_db.execute = AsyncMock(return_value=result)

    resp = await authenticated_client.get("/api/connectors/callback?state=nope&code=xyz")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "window.close()" in resp.text


async def test_oauth_callback_success_self_closes_and_warms_refresh_token(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """A successful GitLab exchange self-closes the popup (the opener polls status) and
    moves the refresh token to the warm store."""
    row = MagicMock(
        code_verifier="v",
        connector="gitlab",
        user_id=mock_user.id,
        session_id=mock_session_obj.id,
        created_at=datetime.now(UTC),
    )
    mock_db.execute = AsyncMock(return_value=MagicMock(first=MagicMock(return_value=row)))
    cred = MintedCredential(
        "gitlab", "read", datetime.now(UTC) + timedelta(hours=1), secret={"refresh_token": "rt"}
    )
    conn = MagicMock(exchange_code=AsyncMock(return_value=cred))
    store = MagicMock(put=AsyncMock())
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch("app.api.routes.connectors.store_gitlab_warm", AsyncMock()) as warm,
    ):
        resp = await authenticated_client.get("/api/connectors/callback?state=s&code=c")
    assert resp.status_code == 200
    assert "window.close()" in resp.text
    assert "Connected to gitlab" in resp.text
    warm.assert_awaited_once()  # refresh token moved to the warm store…
    assert "refresh_token" not in cred.secret  # …and popped from the session credential


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


# --------------------------------------------------------------------------- #
# Grant progress: STS minting ("granting") → EKS discovery ("discovering")
# --------------------------------------------------------------------------- #
_AWS_SEL_EKS = {"profiles": [{"account_id": "111", "role_name": "eks-ro", "name": "acme-eks-ro"}]}


def _granted(selection: dict) -> MintedCredential:
    return MintedCredential(
        "aws",
        "read",
        datetime.now(UTC) + timedelta(hours=1),
        secret={"profiles": [{"name": "p"}]},
        selection=selection,
    )


async def _post_select(client: AsyncClient, sid, role_name: str):
    return await client.post(
        f"/api/connectors/{sid}/aws/select",
        json={"selections": [{"account_id": "111", "role_name": role_name}]},
    )


async def test_aws_select_with_eks_role_hands_progress_to_discovery(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """An eks-* pick keeps the progress entry alive (phase "discovering") for the
    background EKS task, which owns its release via on_done."""
    from app.api.routes import connectors as c

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(
        type="aws", select_account_roles=AsyncMock(return_value=_granted(_AWS_SEL_EKS))
    )
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="connected")
    key = (str(mock_user.id), str(mock_session_obj.id))
    c._aws_enum_progress.pop(key, None)
    try:
        with (
            patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
            patch("app.api.routes.connectors.get_connector", return_value=conn),
            patch("app.api.routes.connectors.get_credential_store", return_value=store),
            patch("app.api.routes.connectors.write_aws_profiles"),
            patch("app.api.routes.connectors.schedule_session_eks_refresh") as eks,
        ):
            resp = await _post_select(authenticated_client, mock_session_obj.id, "eks-ro")

        assert resp.status_code == 200
        # The mint got a progress callback (drives the "granting" phase).
        _, sel_kwargs = conn.select_account_roles.call_args
        assert callable(sel_kwargs["progress_cb"])
        # The EKS task got both a progress callback and the release hook.
        _, eks_kwargs = eks.call_args
        assert callable(eks_kwargs["progress_cb"])
        assert callable(eks_kwargs["on_done"])
        # Entry survives the response, flipped to "discovering" (on_done not yet fired),
        # seeded with the real profile count so a gap poll reads a determinate 0/N.
        assert c._aws_enum_progress[key]["phase"] == "discovering"
        assert c._aws_enum_progress[key]["done"] == 0
        assert c._aws_enum_progress[key]["total"] == 1  # _granted() has one profile
        # The task's on_done settles the entry into a terminal "done" that lingers a few
        # seconds (not an immediate pop) so a poll is guaranteed to observe it even when
        # discovery finishes between two polls. Last seeded counts are preserved.
        eks_kwargs["on_done"]()
        entry = c._aws_enum_progress[key]
        assert entry["phase"] == "done"
        assert (entry["done"], entry["total"]) == (0, 1)
        assert entry["expires_at"] > time.monotonic()
    finally:
        c._aws_enum_progress.pop(key, None)


async def test_aws_select_mixed_selection_arms_wait_when_any_eks(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """want_eks is any(), not all(): one eks-* among other picks still tracks discovery."""
    from app.api.routes import connectors as c

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(
        type="aws", select_account_roles=AsyncMock(return_value=_granted(_AWS_SEL_EKS))
    )
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="connected")
    key = (str(mock_user.id), str(mock_session_obj.id))
    c._aws_enum_progress.pop(key, None)
    try:
        with (
            patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
            patch("app.api.routes.connectors.get_connector", return_value=conn),
            patch("app.api.routes.connectors.get_credential_store", return_value=store),
            patch("app.api.routes.connectors.write_aws_profiles"),
            patch("app.api.routes.connectors.schedule_session_eks_refresh") as eks,
        ):
            resp = await authenticated_client.post(
                f"/api/connectors/{mock_session_obj.id}/aws/select",
                json={
                    "selections": [
                        {"account_id": "111", "role_name": "ReadOnly"},
                        {"account_id": "111", "role_name": "eks-ro"},
                    ]
                },
            )
        assert resp.status_code == 200
        assert c._aws_enum_progress[key]["phase"] == "discovering"
        assert "on_done" in eks.call_args.kwargs
    finally:
        c._aws_enum_progress.pop(key, None)


async def test_aws_select_eks_role_gate_is_case_insensitive(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """An upper-case "EKS-*" pick must also keep the entry alive (matches _dedup_prefer_eks)."""
    from app.api.routes import connectors as c

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(
        type="aws", select_account_roles=AsyncMock(return_value=_granted(_AWS_SEL_EKS))
    )
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="connected")
    key = (str(mock_user.id), str(mock_session_obj.id))
    c._aws_enum_progress.pop(key, None)
    try:
        with (
            patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
            patch("app.api.routes.connectors.get_connector", return_value=conn),
            patch("app.api.routes.connectors.get_credential_store", return_value=store),
            patch("app.api.routes.connectors.write_aws_profiles"),
            patch("app.api.routes.connectors.schedule_session_eks_refresh"),
        ):
            resp = await _post_select(authenticated_client, mock_session_obj.id, "EKS-Admin")
        assert resp.status_code == 200
        assert c._aws_enum_progress[key]["phase"] == "discovering"
    finally:
        c._aws_enum_progress.pop(key, None)


async def test_aws_select_without_eks_role_releases_progress_immediately(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """A non-eks pick has no cluster RBAC worth waiting on: discovery still runs but
    untracked, and the progress entry is released before the response returns."""
    from app.api.routes import connectors as c

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(type="aws", select_account_roles=AsyncMock(return_value=_granted(_AWS_SEL)))
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="connected")
    key = (str(mock_user.id), str(mock_session_obj.id))
    c._aws_enum_progress.pop(key, None)
    try:
        with (
            patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
            patch("app.api.routes.connectors.get_connector", return_value=conn),
            patch("app.api.routes.connectors.get_credential_store", return_value=store),
            patch("app.api.routes.connectors.write_aws_profiles"),
            patch("app.api.routes.connectors.schedule_session_eks_refresh") as eks,
        ):
            resp = await _post_select(authenticated_client, mock_session_obj.id, "ReadOnly")

        assert resp.status_code == 200
        eks.assert_called_once()
        # Scheduled without progress tracking (fire-and-forget, as before).
        _, eks_kwargs = eks.call_args
        assert "progress_cb" not in eks_kwargs
        assert "on_done" not in eks_kwargs
        assert key not in c._aws_enum_progress
    finally:
        c._aws_enum_progress.pop(key, None)


async def test_aws_select_clears_progress_on_mint_error(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """A failed STS mint must release the entry (no dangling "granting" forever)."""
    from app.api.routes import connectors as c

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    store = MagicMock(get=AsyncMock(return_value=_aws_provisional()), put=AsyncMock())
    conn = MagicMock(
        type="aws",
        select_account_roles=AsyncMock(side_effect=ConnectorError("Account/role not authorized")),
    )
    key = (str(mock_user.id), str(mock_session_obj.id))
    c._aws_enum_progress.pop(key, None)
    try:
        with (
            patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
            patch("app.api.routes.connectors.get_connector", return_value=conn),
            patch("app.api.routes.connectors.get_credential_store", return_value=store),
            patch("app.api.routes.connectors.schedule_session_eks_refresh"),
        ):
            resp = await _post_select(authenticated_client, mock_session_obj.id, "eks-ro")
        assert resp.status_code == 400
        assert key not in c._aws_enum_progress
    finally:
        c._aws_enum_progress.pop(key, None)


async def test_aws_options_progress_reports_discovering_phase(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """The endpoint echoes the stored phase verbatim (not a hardcoded "enumerating")."""
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    from app.api.routes.connectors import _aws_enum_progress

    key = (str(mock_user.id), str(mock_session_obj.id))
    _aws_enum_progress[key] = {"phase": "discovering", "done": 2, "total": 5}
    try:
        with patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS):
            resp = await authenticated_client.get(
                f"/api/connectors/{mock_session_obj.id}/aws/options/progress"
            )
    finally:
        _aws_enum_progress.pop(key, None)
    assert resp.status_code == 200
    assert resp.json() == {"phase": "discovering", "done": 2, "total": 5}


def test_progress_cb_is_owner_guarded_no_clobber_no_resurrect() -> None:
    """A superseded owner can neither clobber a newer entry nor resurrect an ended one."""
    from app.api.routes import connectors as c

    key = ("u", "owner-guard")
    t1, cb1 = c._begin_enum_progress(key, phase="granting")
    assert c._aws_enum_progress[key]["phase"] == "granting"
    t2, _cb2 = c._begin_enum_progress(key, phase="granting")  # a newer grant took over
    cb1(5, 5)  # stale owner must not clobber the newer entry
    assert c._aws_enum_progress[key]["token"] is t2
    assert c._aws_enum_progress[key]["done"] == 0
    c._end_enum_progress(key, t2)
    cb1(9, 9)  # late stale tick after release must not resurrect
    assert key not in c._aws_enum_progress


async def test_mint_sts_profiles_reports_progress() -> None:
    """_mint_sts_profiles fires (0, total) up front then one tick per completed pick."""
    from app.services.connectors import aws

    picks = [
        {"account_id": "1", "role_name": "r1", "account_name": "a1"},
        {"account_id": "2", "role_name": "r2", "account_name": "a2"},
    ]
    creds = {
        "accessKeyId": "AK",
        "secretAccessKey": "SK",  # pragma: allowlist secret
        "sessionToken": "ST",
        "expiration": 1000,
    }
    calls: list[tuple[int, int]] = []
    with (
        patch.object(aws, "_build_sso_client", return_value=MagicMock()),
        patch.object(aws, "_get_role_credentials", return_value=creds),
    ):
        profiles, _ = await aws._mint_sts_profiles(
            "tok", picks, "eu-west-1", progress_cb=lambda d, t: calls.append((d, t))
        )
    assert len(profiles) == 2
    assert calls[0] == (0, 2)
    assert calls[-1] == (2, 2)


def test_progress_registry_is_thread_safe_no_resurrect() -> None:
    """The publish callback fires from worker threads while begin/end run on the loop;
    _enum_lock must make a cb racing _end unable to resurrect the popped entry."""
    import threading

    from app.api.routes import connectors as c

    key = ("u", "thread-race")
    c._aws_enum_progress.pop(key, None)
    token, cb = c._begin_enum_progress(key, phase="discovering")
    # A worker thread hammers the owner-guarded publish while the loop thread ends it.
    worker = threading.Thread(target=lambda: [cb(1, 2) for _ in range(2000)])
    worker.start()
    c._end_enum_progress(key, token)
    worker.join()
    # Once ended, no racing cb may have re-added the entry (owner check sees it gone).
    assert key not in c._aws_enum_progress


async def test_aws_revoke_cancels_refresh_before_clearing(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """Revoke must cancel any in-flight EKS refresh BEFORE clearing creds, else the
    refresh could re-publish the kubeconfigs revoke just deleted."""
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    revoked = MintedCredential(
        "aws", "read", datetime.now(UTC) + timedelta(hours=1), secret={"profiles": [{"name": "p"}]}
    )
    store = MagicMock(revoke=AsyncMock(return_value=revoked))
    conn = MagicMock(type="aws", revoke=AsyncMock())
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="not_connected")
    order: list[str] = []
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch(
            "app.api.routes.connectors.cancel_session_refresh",
            AsyncMock(side_effect=lambda *_: order.append("cancel")),
        ),
        patch(
            "app.tools.connector_exec.cleanup_aws_files",
            side_effect=lambda *a, **k: order.append("clear"),
        ),
    ):
        resp = await authenticated_client.post(f"/api/connectors/{mock_session_obj.id}/aws/revoke")
    assert resp.status_code == 200
    assert order == ["cancel", "clear"]  # cancel strictly before clear


async def test_aws_revoke_clears_creds_under_session_lock(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """The clear must run while holding the per-session lock — not just after the cancel.
    Otherwise an overlapping re-select could interleave a publish with the revoke's clear
    and leave a torn kube set. Assert the lock is held during cleanup_aws_files."""
    from app.services import eks_refresh

    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    revoked = MintedCredential(
        "aws", "read", datetime.now(UTC) + timedelta(hours=1), secret={"profiles": [{"name": "p"}]}
    )
    store = MagicMock(revoke=AsyncMock(return_value=revoked))
    conn = MagicMock(type="aws", revoke=AsyncMock())
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="not_connected")
    held: list[bool] = []

    # cleanup is called with sid as its first positional arg (under the session lock, via
    # asyncio.to_thread); query the same keyed lock to confirm it's held at clear time.
    def _clear(sid_arg, **_kwargs):  # noqa: ANN001, ANN202
        held.append(eks_refresh.session_lock(sid_arg).locked())

    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch("app.tools.connector_exec.cleanup_aws_files", side_effect=_clear),
    ):
        resp = await authenticated_client.post(f"/api/connectors/{mock_session_obj.id}/aws/revoke")
    assert resp.status_code == 200
    assert held == [True]  # lock was held while clearing


async def test_aws_revoke_clears_creds_in_place_with_keep_dirs(
    authenticated_client: AsyncClient, mock_db: AsyncMock, mock_user: MagicMock, mock_session_obj
) -> None:
    """Revoke must clear creds IN PLACE (keep_dirs=True): the conv container may still
    bind-mount the dirs, and a mount follows the inode — rmtree would detach it."""
    _session_lookup(mock_db, mock_session_obj, mock_user.id)
    revoked = MintedCredential(
        "aws", "read", datetime.now(UTC) + timedelta(hours=1), secret={"profiles": [{"name": "p"}]}
    )
    store = MagicMock(revoke=AsyncMock(return_value=revoked))
    conn = MagicMock(type="aws", revoke=AsyncMock())
    conn.public_status.return_value = ConnectorStatus(connector="aws", state="not_connected")
    with (
        patch("app.api.routes.connectors.get_settings", return_value=_SETTINGS),
        patch("app.api.routes.connectors.get_connector", return_value=conn),
        patch("app.api.routes.connectors.get_credential_store", return_value=store),
        patch("app.tools.connector_exec.cleanup_aws_files") as cleanup,
    ):
        resp = await authenticated_client.post(f"/api/connectors/{mock_session_obj.id}/aws/revoke")
    assert resp.status_code == 200
    cleanup.assert_called_once()
    assert cleanup.call_args.kwargs == {"keep_dirs": True}
