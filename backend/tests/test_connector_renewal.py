"""
Tests for silent connector renewal from warm material (hot-auth step 1).

Invariants under test:
- the store no longer destroys renewable credentials on lazy expiry (the GitLab
  refresh token / AWS SSO token must survive access-token expiry),
- non-renewable credentials keep the historical evict-on-get contract,
- GitLab refresh windows are anchored to the original consent (no sliding cap),
- AWS re-mints STS profiles from the held SSO token without a device flow,
- concurrent renewals are single-flight (GitLab rotation makes a concurrent
  second refresh self-destructive).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.connectors.aws import AwsConnector
from app.services.connectors.base import ConnectorError, MintedCredential
from app.services.connectors.gitlab import _credential_from_token
from app.services.connectors.renewal import get_or_renew_cred
from app.services.connectors.store import CredentialStore, get_credential_store
from app.services.connectors.warm import (
    WarmGrant,
    get_warm_store,
    grant_aws_provisional_from_warm,
    grant_gitlab_from_warm,
    store_gitlab_warm,
)

_PAST = datetime.now(UTC) - timedelta(seconds=1)
_FUTURE = datetime.now(UTC) + timedelta(hours=1)


def _expired_renewable_gitlab() -> MintedCredential:
    # Session credential: access token only — the refresh token is user-scoped
    # warm material and never lives in a session credential.
    return MintedCredential(
        connector="gitlab",
        scope="read",
        expires_at=_PAST,
        renew_until=_FUTURE,
        secret={"access_token": "OLD"},  # pragma: allowlist secret
    )


async def _seed_gitlab_warm(uid: uuid.UUID) -> None:
    await store_gitlab_warm(uid, "REFRESH")


# --------------------------------------------------------------------------- #
# Store: renewable credentials survive expiry; dead ones keep the old contract
# --------------------------------------------------------------------------- #
async def test_store_keeps_expired_renewable_cred():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    await store.put(uid, sid, _expired_renewable_gitlab())
    # Default read still reports "nothing usable"...
    assert await store.get(uid, sid, "gitlab") is None
    # ...but the credential was NOT destroyed (renewal paths can still see it).
    held = await store.get(uid, sid, "gitlab", include_expired=True)
    assert held is not None
    assert held.secret["access_token"] == "OLD"  # pragma: allowlist secret


async def test_store_still_evicts_non_renewable_expired_cred():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    await store.put(uid, sid, MintedCredential("aws", "read", _PAST))
    assert await store.get(uid, sid, "aws") is None
    # Historical contract: nothing renewable → evicted, even with include_expired.
    assert await store.get(uid, sid, "aws", include_expired=True) is None


async def test_store_evicts_once_renew_window_passed():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    cred = _expired_renewable_gitlab()
    cred.renew_until = _PAST  # warm material is dead too
    await store.put(uid, sid, cred)
    assert await store.get(uid, sid, "gitlab", include_expired=True) is None


async def test_store_sweep_spares_renewable_creds():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    await store.put(uid, sid, _expired_renewable_gitlab())
    await store.put(uid, sid, MintedCredential("aws", "read", _PAST))
    assert await store.sweep() == 1  # only the non-renewable one
    assert await store.get(uid, sid, "gitlab", include_expired=True) is not None


# --------------------------------------------------------------------------- #
# GitLab: renewal window set on grant, anchored across refreshes
# --------------------------------------------------------------------------- #
def test_gitlab_credential_renew_window_only_with_refresh_token():
    with_rt = _credential_from_token(
        {"access_token": "A", "refresh_token": "R", "expires_in": 7200}, "read_api"
    )
    assert with_rt.renew_until is not None
    assert with_rt.renew_until > with_rt.expires_at
    without_rt = _credential_from_token({"access_token": "A", "expires_in": 7200}, "read_api")
    assert without_rt.renew_until is None


async def test_grant_gitlab_from_warm_rotates_and_strips_refresh_token():
    uid = uuid.uuid4()
    await _seed_gitlab_warm(uid)
    token = {"access_token": "NEW", "refresh_token": "ROTATED", "expires_in": 7200}
    try:
        with patch("app.services.connectors.gitlab._post_token", AsyncMock(return_value=token)):
            cred = await grant_gitlab_from_warm(uid)
        assert cred is not None and cred.secret["access_token"] == "NEW"
        # Session credential never carries the refresh token...
        assert "refresh_token" not in cred.secret
        # ...the rotated one was persisted to the warm grant before use...
        warm = await get_warm_store().get(uid, "gitlab")
        assert warm is not None
        assert warm.secret["refresh_token"] == "ROTATED"  # pragma: allowlist secret
        # ...and the renewal window is anchored to the warm grant's consent cap.
        assert cred.renew_until == warm.not_after
    finally:
        await get_warm_store().delete(uid, "gitlab")


async def test_grant_gitlab_from_warm_drops_grant_on_dead_refresh_token():
    uid = uuid.uuid4()
    await _seed_gitlab_warm(uid)
    try:
        with patch(
            "app.services.connectors.gitlab._post_token",
            AsyncMock(side_effect=ConnectorError("invalid_grant", fatal=True)),
        ):
            assert await grant_gitlab_from_warm(uid) is None
        # Dead token → warm grant dropped → next attempt falls back to OAuth.
        assert await get_warm_store().get(uid, "gitlab") is None
    finally:
        await get_warm_store().delete(uid, "gitlab")


async def test_grant_gitlab_from_warm_keeps_grant_on_transient_failure():
    uid = uuid.uuid4()
    await _seed_gitlab_warm(uid)
    try:
        with patch(
            "app.services.connectors.gitlab._post_token",
            AsyncMock(side_effect=ConnectorError("timeout", fatal=False)),
        ):
            assert await grant_gitlab_from_warm(uid) is None
        # Provider blip must NOT destroy the 24h grant — next attempt can succeed.
        assert await get_warm_store().get(uid, "gitlab") is not None
    finally:
        await get_warm_store().delete(uid, "gitlab")


async def test_post_token_fatality_mapping():
    import httpx

    from app.services.connectors.gitlab import _post_token

    real_client = httpx.AsyncClient  # patching gitlab.httpx.AsyncClient patches the module attr

    def _client_factory(handler):
        return lambda: real_client(transport=httpx.MockTransport(handler))

    # 401 from the token endpoint = grant rejected → fatal.
    with patch(
        "app.services.connectors.gitlab.httpx.AsyncClient",
        _client_factory(lambda req: httpx.Response(401, json={"error": "invalid_grant"})),
    ):
        try:
            await _post_token("https://gitlab.com", {})
            raise AssertionError("expected ConnectorError")
        except ConnectorError as e:
            assert e.fatal is True

    # 503 = provider hiccup → transient.
    with patch(
        "app.services.connectors.gitlab.httpx.AsyncClient",
        _client_factory(lambda req: httpx.Response(503)),
    ):
        try:
            await _post_token("https://gitlab.com", {})
            raise AssertionError("expected ConnectorError")
        except ConnectorError as e:
            assert e.fatal is False

    # Network-level failure (timeout) → transient.
    def _raise_timeout(req):
        raise httpx.ConnectTimeout("boom")

    with patch("app.services.connectors.gitlab.httpx.AsyncClient", _client_factory(_raise_timeout)):
        try:
            await _post_token("https://gitlab.com", {})
            raise AssertionError("expected ConnectorError")
        except ConnectorError as e:
            assert e.fatal is False


async def test_grant_aws_provisional_from_warm_enumerates_without_device_flow():
    uid = uuid.uuid4()
    await get_warm_store().put(
        uid,
        WarmGrant(platform="aws", secret={"sso_access_token": "SSO"}, not_after=_FUTURE),
    )
    accounts = [{"account_id": "111", "account_name": "acme"}]
    roles = [{"account_id": "111", "role_name": "ReadOnly"}]
    try:
        with patch(
            "app.services.connectors.aws._list_accounts_and_roles", return_value=(accounts, roles)
        ):
            cred = await grant_aws_provisional_from_warm(uid)
        assert cred is not None
        assert cred.selection is None  # needs_selection: straight to the picker
        assert cred.available_roles == roles
    finally:
        await get_warm_store().delete(uid, "aws")


async def test_grant_aws_provisional_from_warm_uses_cached_options():
    uid = uuid.uuid4()
    accounts = [{"account_id": "111", "account_name": "acme"}]
    roles = [{"account_id": "111", "role_name": "ReadOnly"}]
    await get_warm_store().put(
        uid,
        WarmGrant(
            platform="aws",
            secret={"sso_access_token": "SSO", "accounts": accounts, "roles": roles},
            not_after=_FUTURE,
        ),
    )
    try:
        # Cached lists present → must NOT hit the SSO portal.
        with patch(
            "app.services.connectors.aws._list_accounts_and_roles",
            side_effect=AssertionError("enumeration must be skipped when cached"),
        ):
            cred = await grant_aws_provisional_from_warm(uid)
        assert cred is not None
        assert cred.available_accounts == accounts
        assert cred.available_roles == roles
    finally:
        await get_warm_store().delete(uid, "aws")


async def test_store_aws_warm_caches_enumerated_options():
    from app.services.connectors.warm import store_aws_warm, update_aws_warm_options

    uid = uuid.uuid4()
    accounts = [{"account_id": "111", "account_name": "acme"}]
    roles = [{"account_id": "111", "role_name": "ReadOnly"}]
    provisional = MintedCredential(
        connector="aws",
        scope="sso:account:access",
        expires_at=_FUTURE,
        secret={"sso_access_token": "SSO", "region": "eu-west-1"},
        available_accounts=accounts,
        available_roles=roles,
    )
    try:
        await store_aws_warm(uid, provisional)
        warm = await get_warm_store().get(uid, "aws")
        assert warm is not None
        assert warm.secret["accounts"] == accounts
        assert warm.secret["roles"] == roles

        # A forced refresh replaces the cache.
        new_roles = [{"account_id": "111", "role_name": "Admin"}]
        await update_aws_warm_options(uid, accounts, new_roles)
        warm = await get_warm_store().get(uid, "aws")
        assert warm is not None and warm.secret["roles"] == new_roles
    finally:
        await get_warm_store().delete(uid, "aws")


async def test_grant_aws_provisional_from_warm_drops_grant_on_dead_sso_token():
    from botocore.exceptions import ClientError

    uid = uuid.uuid4()
    await get_warm_store().put(
        uid,
        WarmGrant(platform="aws", secret={"sso_access_token": "SSO"}, not_after=_FUTURE),
    )
    dead = ClientError({"Error": {"Code": "UnauthorizedException"}}, "ListAccounts")
    try:
        with patch("app.services.connectors.aws._list_accounts_and_roles", side_effect=dead):
            assert await grant_aws_provisional_from_warm(uid) is None
        # Dead SSO token → grant dropped → fallback to the device flow.
        assert await get_warm_store().get(uid, "aws") is None
    finally:
        await get_warm_store().delete(uid, "aws")


async def test_grant_aws_provisional_from_warm_keeps_grant_on_transient_failure():
    from botocore.exceptions import ClientError

    uid = uuid.uuid4()
    await get_warm_store().put(
        uid,
        WarmGrant(platform="aws", secret={"sso_access_token": "SSO"}, not_after=_FUTURE),
    )
    throttled = ClientError({"Error": {"Code": "TooManyRequestsException"}}, "ListAccounts")
    try:
        with patch("app.services.connectors.aws._list_accounts_and_roles", side_effect=throttled):
            assert await grant_aws_provisional_from_warm(uid) is None
        # Throttling/timeouts must NOT destroy the ~8h SSO grant.
        assert await get_warm_store().get(uid, "aws") is not None
    finally:
        await get_warm_store().delete(uid, "aws")


async def test_warm_grant_expires_at_policy_cap():
    uid = uuid.uuid4()
    await get_warm_store().put(
        uid, WarmGrant(platform="gitlab", secret={"refresh_token": "R"}, not_after=_PAST)
    )
    assert await get_warm_store().get(uid, "gitlab") is None  # lazily evicted


# --------------------------------------------------------------------------- #
# AWS: refreshToken captured, SSO token kept, silent re-mint
# --------------------------------------------------------------------------- #
def test_create_token_device_captures_refresh_token():
    from app.services.connectors.aws import _create_token_device

    boto_client = MagicMock()
    boto_client.create_token.return_value = {
        "accessToken": "SSO",
        "expiresIn": 28800,
        "refreshToken": "SSOREFRESH",
    }
    with patch("boto3.client", return_value=boto_client):
        token = _create_token_device("cid", "csecret", "device", "eu-west-1")
    assert token == {
        "access_token": "SSO",
        "expires_in": 28800,
        "refresh_token": "SSOREFRESH",  # pragma: allowlist secret
    }


def _sts_response() -> dict:
    return {
        "accessKeyId": "AKIA",
        "secretAccessKey": "SK",  # pragma: allowlist secret
        "sessionToken": "ST",
        "expiration": int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000),
    }


def _provisional() -> MintedCredential:
    return MintedCredential(
        connector="aws",
        scope="sso:account:access",
        expires_at=datetime.now(UTC) + timedelta(hours=8),
        secret={"sso_access_token": "SSO", "sso_refresh_token": None, "region": "eu-west-1"},
        available_accounts=[{"account_id": "111", "account_name": "acme"}],
        available_roles=[{"account_id": "111", "role_name": "ReadOnly"}],
    )


async def test_select_account_roles_keeps_sso_token_for_remint():
    provisional = _provisional()
    with patch("app.services.connectors.aws._get_role_credentials", return_value=_sts_response()):
        cred = await AwsConnector().select_account_roles(
            provisional, selections=[{"account_id": "111", "role_name": "ReadOnly"}]
        )
    assert cred.secret["sso_access_token"] == "SSO"  # pragma: allowlist secret
    assert cred.secret["profiles"][0]["account_id"] == "111"
    # Renewable for as long as the SSO token lives.
    assert cred.renew_until == provisional.expires_at


async def test_select_account_roles_is_all_or_nothing_on_partial_failure():
    """One failing pick fails the whole batch — concurrent minting is atomic, no
    partial grant (matches the original serial fail-fast behaviour)."""
    provisional = MintedCredential(
        connector="aws",
        scope="sso:account:access",
        expires_at=datetime.now(UTC) + timedelta(hours=8),
        secret={"sso_access_token": "SSO", "sso_refresh_token": None, "region": "eu-west-1"},
        available_accounts=[{"account_id": "111", "account_name": "acme"}],
        available_roles=[
            {"account_id": "111", "role_name": "ReadOnly"},
            {"account_id": "111", "role_name": "Admin"},
        ],
    )

    def _flaky(sso, access_token, account_id, role_name):
        if role_name == "Admin":
            raise RuntimeError("STS denied")
        return _sts_response()

    with patch("app.services.connectors.aws._get_role_credentials", side_effect=_flaky):
        try:
            await AwsConnector().select_account_roles(
                provisional,
                selections=[
                    {"account_id": "111", "role_name": "ReadOnly"},
                    {"account_id": "111", "role_name": "Admin"},
                ],
            )
            raise AssertionError("expected ConnectorError")
        except ConnectorError:
            pass


async def test_remint_profiles_reuses_saved_selection():
    with patch("app.services.connectors.aws._get_role_credentials", return_value=_sts_response()):
        cred = await AwsConnector().select_account_roles(
            _provisional(), selections=[{"account_id": "111", "role_name": "ReadOnly"}]
        )
        cred.expires_at = _PAST  # STS expired; SSO token still alive
        fresh = await AwsConnector().remint_profiles(cred)
    assert fresh.is_expired() is False
    assert fresh.selection == cred.selection
    assert fresh.secret["sso_access_token"] == "SSO"  # pragma: allowlist secret
    assert fresh.renew_until == cred.renew_until


async def test_remint_profiles_refuses_dead_sso_session():
    cred = MintedCredential(
        connector="aws",
        scope="read",
        expires_at=_PAST,
        renew_until=_PAST,  # SSO token gone
        secret={"sso_access_token": "SSO", "profiles": [{"name": "p"}]},
        selection={"profiles": [{"account_id": "111", "role_name": "ReadOnly", "name": "p"}]},
    )
    try:
        await AwsConnector().remint_profiles(cred)
        raise AssertionError("expected ConnectorError")
    except ConnectorError:
        pass


# --------------------------------------------------------------------------- #
# get_or_renew_cred: renew-on-read, single-flight, graceful failure
# --------------------------------------------------------------------------- #
async def test_get_or_renew_renews_expired_gitlab_from_warm():
    uid, sid = uuid.uuid4(), uuid.uuid4()
    store = get_credential_store()
    await store.put(uid, sid, _expired_renewable_gitlab())
    await _seed_gitlab_warm(uid)
    token = {"access_token": "NEW", "refresh_token": "ROTATED", "expires_in": 7200}
    try:
        with (
            patch("app.services.connectors.renewal.get_connector", return_value=MagicMock()),
            patch("app.services.connectors.gitlab._post_token", AsyncMock(return_value=token)),
        ):
            cred = await get_or_renew_cred(uid, sid, "gitlab")
        assert cred is not None and cred.secret["access_token"] == "NEW"
        # The fresh session credential was persisted (and carries no refresh token).
        held = await store.get(uid, sid, "gitlab")
        assert held is not None and "refresh_token" not in held.secret
    finally:
        await store.revoke_session(uid, sid)
        await get_warm_store().delete(uid, "gitlab")


async def test_get_or_renew_is_single_flight():
    uid, sid = uuid.uuid4(), uuid.uuid4()
    store = get_credential_store()
    await store.put(uid, sid, _expired_renewable_gitlab())
    await _seed_gitlab_warm(uid)
    calls = 0

    async def _slow_post_token(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"access_token": "NEW", "refresh_token": "ROTATED", "expires_in": 7200}

    try:
        with (
            patch("app.services.connectors.renewal.get_connector", return_value=MagicMock()),
            patch("app.services.connectors.gitlab._post_token", side_effect=_slow_post_token),
        ):
            results = await asyncio.gather(
                *[get_or_renew_cred(uid, sid, "gitlab") for _ in range(5)]
            )
        assert calls == 1  # GitLab rotation: a second concurrent refresh would brick the grant
        assert all(r is not None and r.secret["access_token"] == "NEW" for r in results)
    finally:
        await store.revoke_session(uid, sid)
        await get_warm_store().delete(uid, "gitlab")


async def test_get_or_renew_renews_expired_aws_and_refreshes_projections():
    uid, sid = uuid.uuid4(), uuid.uuid4()
    store = get_credential_store()
    with patch("app.services.connectors.aws._get_role_credentials", return_value=_sts_response()):
        cred = await AwsConnector().select_account_roles(
            _provisional(), selections=[{"account_id": "111", "role_name": "ReadOnly"}]
        )
        cred.expires_at = _PAST
        await store.put(uid, sid, cred)
        try:
            with (
                patch("app.services.connectors.renewal.get_connector", return_value=AwsConnector()),
                patch("app.tools.connector_exec.write_aws_profiles") as write_profiles,
                patch("app.services.eks_refresh.schedule_session_eks_refresh") as eks_refresh,
            ):
                fresh = await get_or_renew_cred(uid, sid, "aws")
            assert fresh is not None and fresh.is_expired() is False
            # tmpfs ~/.aws rewritten and the EKS refresh timer re-keyed to the new expiry.
            write_profiles.assert_called_once_with(sid, fresh)
            eks_refresh.assert_called_once_with(sid, fresh)
        finally:
            await store.revoke_session(uid, sid)


async def test_get_or_renew_failure_degrades_to_none():
    uid, sid = uuid.uuid4(), uuid.uuid4()
    store = get_credential_store()
    await store.put(uid, sid, _expired_renewable_gitlab())
    await _seed_gitlab_warm(uid)
    try:
        with (
            patch("app.services.connectors.renewal.get_connector", return_value=MagicMock()),
            patch(
                "app.services.connectors.gitlab._post_token",
                AsyncMock(side_effect=ConnectorError("invalid_grant")),
            ),
        ):
            assert await get_or_renew_cred(uid, sid, "gitlab") is None
    finally:
        await store.revoke_session(uid, sid)
        await get_warm_store().delete(uid, "gitlab")


async def test_get_or_renew_none_when_nothing_held():
    assert await get_or_renew_cred(uuid.uuid4(), uuid.uuid4(), "gitlab") is None
    assert await get_or_renew_cred(None, uuid.uuid4(), "gitlab") is None
