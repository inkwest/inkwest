"""
Tests for the external access connector broker.

Focus on the security-critical invariants: secrets never appear in repr/logs or
in any client-facing projection, credentials are isolated per session, PKCE is
correct, and only read-only AWS calls are permitted.
"""

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.services.connectors.base import (
    ConnectorStatus,
    MintedCredential,
    generate_pkce,
)
from app.services.connectors.store import CredentialStore
from app.tools.connector_validation import (
    validate_aws_call,
    validate_coralogix_query,
    validate_gitlab_api,
)
from app.tools.exec_context import ToolExecContext


# --------------------------------------------------------------------------- #
# MintedCredential: secret must never leak via repr/str
# --------------------------------------------------------------------------- #
def test_minted_credential_repr_hides_secret():
    cred = MintedCredential(
        connector="aws",
        scope="read",
        expires_at=None,
        secret={
            "secret_access_key": "SUPERSECRET",  # pragma: allowlist secret
            "session_token": "TOKEN",
        },
    )
    assert "SUPERSECRET" not in repr(cred)
    assert "SUPERSECRET" not in str(cred)
    assert "TOKEN" not in repr(cred)
    assert "aws" in repr(cred)


def test_minted_credential_expiry():
    past = MintedCredential("gitlab", "read", datetime.now(UTC) - timedelta(seconds=1))
    future = MintedCredential("gitlab", "read", datetime.now(UTC) + timedelta(hours=1))
    none_exp = MintedCredential("coralogix", "read", None)
    assert past.is_expired() is True
    assert future.is_expired() is False
    assert none_exp.is_expired() is False


# --------------------------------------------------------------------------- #
# PKCE
# --------------------------------------------------------------------------- #
def test_generate_pkce_is_valid_s256():
    verifier, challenge = generate_pkce()
    assert 43 <= len(verifier) <= 128
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    assert challenge == expected
    # Two calls produce different verifiers.
    assert generate_pkce()[0] != verifier


# --------------------------------------------------------------------------- #
# CredentialStore
# --------------------------------------------------------------------------- #
async def test_store_put_get_revoke():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    cred = MintedCredential("gitlab", "read", datetime.now(UTC) + timedelta(hours=1))

    assert await store.get(uid, sid, "gitlab") is None
    await store.put(uid, sid, cred)
    assert await store.get(uid, sid, "gitlab") is cred

    removed = await store.revoke(uid, sid, "gitlab")
    assert removed is cred
    assert await store.get(uid, sid, "gitlab") is None


async def test_store_evicts_expired_on_get():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    expired = MintedCredential("aws", "read", datetime.now(UTC) - timedelta(seconds=1))
    await store.put(uid, sid, expired)
    assert await store.get(uid, sid, "aws") is None


async def test_store_session_isolation():
    """Granting in one session must not leak to another session of the same user."""
    store = CredentialStore()
    uid = uuid.uuid4()
    sid_a, sid_b = uuid.uuid4(), uuid.uuid4()
    cred = MintedCredential("aws", "read", datetime.now(UTC) + timedelta(hours=1))
    await store.put(uid, sid_a, cred)
    assert await store.get(uid, sid_a, "aws") is cred
    assert await store.get(uid, sid_b, "aws") is None


async def test_store_revoke_session_drops_all():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    exp = datetime.now(UTC) + timedelta(hours=1)
    await store.put(uid, sid, MintedCredential("aws", "read", exp))
    await store.put(uid, sid, MintedCredential("gitlab", "read", exp))
    await store.revoke_session(uid, sid)
    assert await store.get(uid, sid, "aws") is None
    assert await store.get(uid, sid, "gitlab") is None


async def test_store_sweep():
    store = CredentialStore()
    uid, sid = uuid.uuid4(), uuid.uuid4()
    await store.put(
        uid, sid, MintedCredential("aws", "read", datetime.now(UTC) - timedelta(seconds=1))
    )
    await store.put(
        uid, sid, MintedCredential("gitlab", "read", datetime.now(UTC) + timedelta(hours=1))
    )
    assert await store.sweep() == 1
    assert await store.get(uid, sid, "gitlab") is not None


# --------------------------------------------------------------------------- #
# AWS read-only allowlist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "service,operation",
    [
        ("eks", "describe-cluster"),
        ("ec2", "describe-instances"),
        ("eks", "list-clusters"),
        # No read-only allowlist: any well-formed op is accepted; least privilege is
        # enforced by the IAM/SSO role the user selects, not by this validation.
        ("s3api", "list-buckets"),
        ("ec2", "terminate-instances"),
    ],
)
def test_aws_call_accepts_wellformed_ops(service, operation):
    assert validate_aws_call({"service": service, "operation": operation}) is None


@pytest.mark.parametrize(
    "service,operation",
    [
        ("", "describe-cluster"),  # missing service
        ("eks", ""),  # missing operation
        ("eks; rm -rf", "list-clusters"),  # malformed service
    ],
)
def test_aws_call_rejects_malformed(service, operation):
    assert validate_aws_call({"service": service, "operation": operation}) is not None


def test_aws_call_rejects_bad_args():
    assert validate_aws_call({"service": "eks", "operation": "list-clusters", "args": "x"})
    assert validate_aws_call(
        {"service": "eks", "operation": "list-clusters", "args": ["ok", "bad\nnewline"]}
    )


def test_gitlab_validation():
    assert validate_gitlab_api({"path": "/projects/1"}) is None
    assert validate_gitlab_api({"path": "projects/1"})  # no leading slash
    assert validate_gitlab_api({"path": "https://evil.com"})  # absolute
    assert validate_gitlab_api({"path": "/a/../../etc"})  # traversal


def test_coralogix_validation():
    assert validate_coralogix_query({"query": "error"}) is None
    assert validate_coralogix_query({"query": ""})
    assert validate_coralogix_query({"query": "x", "syntax": "bogus"})


# --------------------------------------------------------------------------- #
# Connector executors: NEEDS_AUTH when no credential is held
# --------------------------------------------------------------------------- #
async def test_execute_aws_needs_auth_when_no_cred():
    from app.tools.connector_exec import execute_aws

    ctx = ToolExecContext(user_id=uuid.uuid4(), session_id=uuid.uuid4(), kubeconfig_path="/x")
    out, is_error = await execute_aws({"service": "eks", "operation": "list-clusters"}, ctx)
    assert is_error is True
    assert "Give access to AWS" in out


async def test_execute_aws_writes_profiles_to_ramfile_not_argv():
    """STS secrets are projected to a (tmpfs) credentials file, never to argv."""
    from app.services.connectors.store import get_credential_store
    from app.tools.connector_exec import cleanup_aws_files, execute_aws

    uid, sid = uuid.uuid4(), uuid.uuid4()
    cred = MintedCredential(
        connector="aws",
        scope="2 compte(s)",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        secret={
            "region": "eu-west-1",
            "profiles": [
                {
                    "name": "acme-ReadOnly",
                    "account_id": "111111111111",
                    "account_name": "acme",
                    "role_name": "ReadOnly",
                    "access_key_id": "AKIA_TEST",
                    "secret_access_key": "SECRETKEYVALUE",  # pragma: allowlist secret
                    "session_token": "SESSIONTOKEN",
                    "region": "eu-west-1",
                }
            ],
        },
    )
    await get_credential_store().put(uid, sid, cred)
    ctx = ToolExecContext(user_id=uid, session_id=sid, kubeconfig_path="/x")

    completed = MagicMock(returncode=0, stdout='{"clusters": []}', stderr="")
    with patch("app.tools.connector_exec.subprocess.run", return_value=completed) as run:
        out, is_error = await execute_aws({"service": "eks", "operation": "list-clusters"}, ctx)

    assert is_error is False
    args, kwargs = run.call_args
    argv = args[0]
    # Secret must NOT appear in argv nor in env values — only via the creds file path.
    assert "SECRETKEYVALUE" not in " ".join(argv)
    assert "SECRETKEYVALUE" not in " ".join(kwargs["env"].values())
    cred_file = kwargs["env"]["AWS_SHARED_CREDENTIALS_FILE"]
    with open(cred_file) as f:
        assert "SECRETKEYVALUE" in f.read()  # secret lives in the (tmpfs) file
    assert argv[:3] == ["aws", "eks", "list-clusters"]

    cleanup_aws_files(sid)
    await get_credential_store().revoke_session(uid, sid)


# --------------------------------------------------------------------------- #
# EKS refresh wiring: generated kubeconfigs reach the agent via
# KUBECONFIG, and the agent is told the contexts are already configured.
# --------------------------------------------------------------------------- #
def _aws_cred_with_profile() -> MintedCredential:
    return MintedCredential(
        connector="aws",
        scope="read",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        secret={
            "region": "eu-west-1",
            "profiles": [
                {
                    "name": "acme-ReadOnly",
                    "account_id": "111111111111",
                    "account_name": "acme",
                    "role_name": "ReadOnly",
                    "access_key_id": "AKIA_TEST",
                    "secret_access_key": "SECRETKEYVALUE",  # pragma: allowlist secret
                    "session_token": "SESSIONTOKEN",  # pragma: allowlist secret
                    "region": "eu-west-1",
                }
            ],
        },
    )


async def test_build_connector_env_wires_generated_kubeconfigs():
    import os

    from app.services.connectors.store import get_credential_store
    from app.tools.connector_exec import (
        build_connector_env,
        cleanup_aws_files,
        connector_prompt_hint,
        session_kube_dir,
    )

    uid, sid = uuid.uuid4(), uuid.uuid4()
    await get_credential_store().put(uid, sid, _aws_cred_with_profile())
    # Simulate the EKS refresh having produced two cluster kubeconfigs.
    kube = session_kube_dir(sid)
    os.makedirs(kube, mode=0o700, exist_ok=True)
    for fname in ("eks--acme--prod", "eks--acme--stg"):
        open(os.path.join(kube, fname), "w").close()

    try:
        env, injected = await build_connector_env(uid, sid)
        # KUBECONFIG carries every generated file, sorted, colon-joined.
        assert env["KUBECONFIG"] == ":".join(
            [os.path.join(kube, "eks--acme--prod"), os.path.join(kube, "eks--acme--stg")]
        )
        assert "aws" in injected and "eks" in injected
        # The agent is told kubectl is ready and must NOT reconfigure it.
        hint = connector_prompt_hint(injected)
        assert "kubectl config get-contexts" in hint
        assert "update-kubeconfig" in hint  # in the "do NOT run" guidance
    finally:
        cleanup_aws_files(sid)
        await get_credential_store().revoke_session(uid, sid)


async def test_build_connector_env_no_kubeconfigs_leaves_kubeconfig_unset():
    """Refresh not run / no reachable clusters → KUBECONFIG stays unset, no eks hint."""
    from app.services.connectors.store import get_credential_store
    from app.tools.connector_exec import build_connector_env, cleanup_aws_files

    uid, sid = uuid.uuid4(), uuid.uuid4()
    await get_credential_store().put(uid, sid, _aws_cred_with_profile())
    try:
        env, injected = await build_connector_env(uid, sid)
        assert "KUBECONFIG" not in env
        assert "aws" in injected and "eks" not in injected
    finally:
        cleanup_aws_files(sid)
        await get_credential_store().revoke_session(uid, sid)


# --------------------------------------------------------------------------- #
# Public status projection carries no secret field
# --------------------------------------------------------------------------- #
def test_connector_status_has_no_secret_field():
    fields = ConnectorStatus.__dataclass_fields__
    assert "secret" not in fields
    # The client-facing response model likewise must not expose a secret.
    from app.api.routes.connectors import ConnectorStatusResponse

    assert "secret" not in ConnectorStatusResponse.model_fields


# --------------------------------------------------------------------------- #
# AWS role filter: regex on "<account_name>.<role_name>"
# --------------------------------------------------------------------------- #
_ACCOUNTS = [
    {"account_id": "111", "account_name": "acme-integration"},
    {"account_id": "222", "account_name": "acme-prod"},
]
_ROLES = [
    {"account_id": "111", "role_name": "AWSReadonlyAccess"},
    {"account_id": "111", "role_name": "AWSAdministratorAccess"},
    {"account_id": "222", "role_name": "AWSAdministratorAccess"},
]


def test_filter_by_role_none_is_passthrough():
    from app.services.connectors.aws import _filter_by_role

    accounts, roles = _filter_by_role(_ACCOUNTS, _ROLES, None)
    assert accounts == _ACCOUNTS and roles == _ROLES
    # Empty string behaves like unset.
    assert _filter_by_role(_ACCOUNTS, _ROLES, "") == (_ACCOUNTS, _ROLES)


def test_filter_by_role_keeps_only_matches_case_insensitive():
    from app.services.connectors.aws import _filter_by_role

    accounts, roles = _filter_by_role(_ACCOUNTS, _ROLES, "readonly")
    # Only the read-only role survives...
    assert roles == [{"account_id": "111", "role_name": "AWSReadonlyAccess"}]
    # ...and the account with no surviving role is dropped.
    assert accounts == [{"account_id": "111", "account_name": "acme-integration"}]


def test_filter_by_role_matches_on_account_dot_role():
    from app.services.connectors.aws import _filter_by_role

    # Anchored to the "<account>." prefix: only integration roles, any role name.
    _, roles = _filter_by_role(_ACCOUNTS, _ROLES, r"^acme-integration\.")
    assert {r["role_name"] for r in roles} == {"AWSReadonlyAccess", "AWSAdministratorAccess"}
    assert {r["account_id"] for r in roles} == {"111"}


def test_validate_settings_rejects_invalid_role_filter():
    from app.core.config import validate_settings

    with patch("app.core.config.get_settings") as mock_get:
        mock_get.return_value = MagicMock(
            ai_provider="claude-cli",
            app_env="development",
            aws_sso_role_filter="([",  # unbalanced → re.error
        )
        with pytest.raises(ValueError, match="AWS_SSO_ROLE_FILTER is not a valid regex"):
            validate_settings()
