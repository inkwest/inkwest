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
from app.services.connectors.gitlab import GitLabConnector, _credential_from_token
from app.services.connectors.renewal import get_or_renew_cred
from app.services.connectors.store import CredentialStore, get_credential_store

_PAST = datetime.now(UTC) - timedelta(seconds=1)
_FUTURE = datetime.now(UTC) + timedelta(hours=1)


def _expired_renewable_gitlab() -> MintedCredential:
    return MintedCredential(
        connector="gitlab",
        scope="read",
        expires_at=_PAST,
        renew_until=_FUTURE,
        secret={"access_token": "OLD", "refresh_token": "REFRESH"},  # pragma: allowlist secret
    )


# --------------------------------------------------------------------------- #
# Store: renewable credentials survive expiry; dead ones keep the old contract
# --------------------------------------------------------------------------- #
async def test_store_keeps_expired_renewable_cred():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    await store.put(uid, sid, _expired_renewable_gitlab())
    # Default read still reports "nothing usable"...
    assert await store.get(uid, sid, "gitlab") is None
    # ...but the credential (and its refresh token) was NOT destroyed.
    held = await store.get(uid, sid, "gitlab", include_expired=True)
    assert held is not None
    assert held.secret["refresh_token"] == "REFRESH"  # pragma: allowlist secret


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


async def test_gitlab_refresh_does_not_slide_renew_window():
    conn = GitLabConnector()
    cred = _expired_renewable_gitlab()
    token = {"access_token": "NEW", "refresh_token": "ROTATED", "expires_in": 7200}
    with patch("app.services.connectors.gitlab._post_token", AsyncMock(return_value=token)):
        renewed = await conn.refresh(cred)
    assert renewed.secret["access_token"] == "NEW"
    assert renewed.secret["refresh_token"] == "ROTATED"  # pragma: allowlist secret
    # Anchored to the original consent, not re-extended by the refresh.
    assert renewed.renew_until == cred.renew_until


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
async def test_get_or_renew_renews_expired_gitlab():
    uid, sid = uuid.uuid4(), uuid.uuid4()
    store = get_credential_store()
    await store.put(uid, sid, _expired_renewable_gitlab())
    token = {"access_token": "NEW", "refresh_token": "ROTATED", "expires_in": 7200}
    try:
        with (
            patch("app.services.connectors.renewal.get_connector", return_value=GitLabConnector()),
            patch("app.services.connectors.gitlab._post_token", AsyncMock(return_value=token)),
        ):
            cred = await get_or_renew_cred(uid, sid, "gitlab")
        assert cred is not None and cred.secret["access_token"] == "NEW"
        # The rotated refresh token was persisted back to the store before use.
        held = await store.get(uid, sid, "gitlab")
        assert held is not None
        assert held.secret["refresh_token"] == "ROTATED"  # pragma: allowlist secret
    finally:
        await store.revoke_session(uid, sid)


async def test_get_or_renew_is_single_flight():
    uid, sid = uuid.uuid4(), uuid.uuid4()
    store = get_credential_store()
    await store.put(uid, sid, _expired_renewable_gitlab())
    calls = 0

    async def _slow_post_token(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"access_token": "NEW", "refresh_token": "ROTATED", "expires_in": 7200}

    try:
        with (
            patch("app.services.connectors.renewal.get_connector", return_value=GitLabConnector()),
            patch("app.services.connectors.gitlab._post_token", side_effect=_slow_post_token),
        ):
            results = await asyncio.gather(
                *[get_or_renew_cred(uid, sid, "gitlab") for _ in range(5)]
            )
        assert calls == 1  # GitLab rotation: a second concurrent refresh would brick the grant
        assert all(r is not None and r.secret["access_token"] == "NEW" for r in results)
    finally:
        await store.revoke_session(uid, sid)


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
    try:
        with (
            patch("app.services.connectors.renewal.get_connector", return_value=GitLabConnector()),
            patch(
                "app.services.connectors.gitlab._post_token",
                AsyncMock(side_effect=ConnectorError("invalid_grant")),
            ),
        ):
            assert await get_or_renew_cred(uid, sid, "gitlab") is None
    finally:
        await store.revoke_session(uid, sid)


async def test_get_or_renew_none_when_nothing_held():
    assert await get_or_renew_cred(uuid.uuid4(), uuid.uuid4(), "gitlab") is None
    assert await get_or_renew_cred(None, uuid.uuid4(), "gitlab") is None
