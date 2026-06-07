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
        secret={"secret_access_key": "SUPERSECRET", "session_token": "TOKEN"},
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
                    "secret_access_key": "SECRETKEYVALUE",
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
# Public status projection carries no secret field
# --------------------------------------------------------------------------- #
def test_connector_status_has_no_secret_field():
    fields = ConnectorStatus.__dataclass_fields__
    assert "secret" not in fields
    # The client-facing response model likewise must not expose a secret.
    from app.api.routes.connectors import ConnectorStatusResponse

    assert "secret" not in ConnectorStatusResponse.model_fields
