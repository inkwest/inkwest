"""
Tests for the per-session EKS kubeconfig refresh (refresh_eks port).

Security-critical invariants: the generated kubeconfigs are written to the session
tmpfs, carry NO secret material (only an exec plugin + file paths), are idempotent
on re-run, degrade gracefully when an account is not reachable, and are removed by
the shared session-tmpfs cleanup.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import yaml

from app.services.connectors.base import MintedCredential
from app.services.eks_refresh import refresh_session_eks
from app.tools.connector_exec import cleanup_aws_files, session_kube_dir


def _cred(*, n_profiles: int = 1) -> MintedCredential:
    profiles = [
        {
            "name": f"acme{i}-ReadOnly",
            "account_id": f"11111111111{i}",
            "account_name": f"acme{i}",
            "role_name": "ReadOnly",
            "access_key_id": "AKIA_TEST",
            "secret_access_key": "SECRETKEYVALUE",  # pragma: allowlist secret
            "session_token": "SESSIONTOKEN",  # pragma: allowlist secret
            "region": "eu-west-1",
        }
        for i in range(n_profiles)
    ]
    return MintedCredential(
        connector="aws",
        scope="read",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        secret={"region": "eu-west-1", "profiles": profiles},
    )


def _mock_eks(clusters: dict[str, dict] | None = None, *, error_code: str | None = None):
    """Build a mock boto3 eks client.

    clusters maps cluster-name -> describe_cluster()["cluster"] payload.
    error_code raises a ClientError from list_clusters (e.g. access denied).
    """
    from botocore.exceptions import ClientError

    client = MagicMock()
    if error_code is not None:
        client.get_paginator.side_effect = ClientError(
            {"Error": {"Code": error_code}}, "ListClusters"
        )
        return client

    clusters = clusters or {}
    paginator = MagicMock()
    paginator.paginate.return_value = [{"clusters": list(clusters)}]
    client.get_paginator.return_value = paginator
    client.describe_cluster.side_effect = lambda name: {"cluster": clusters[name]}
    return client


def _cluster_payload(name: str) -> dict:
    return {
        "name": name,
        "arn": f"arn:aws:eks:eu-west-1:111111111110:cluster/{name}",
        "endpoint": f"https://{name}.eks.eu-west-1.amazonaws.com",
        "certificateAuthority": {"data": "QkFTRTY0Q0E="},  # base64("BASE64CA")
    }


async def test_refresh_generates_one_kubeconfig_per_cluster_in_tmpfs():
    sid = uuid.uuid4()
    eks = _mock_eks({"prod-cluster": _cluster_payload("prod-cluster")})
    try:
        with patch("boto3.client", return_value=eks):
            count = await refresh_session_eks(sid, _cred())

        assert count == 1
        path = os.path.join(session_kube_dir(sid), "eks--acme0--prod-cluster")
        assert os.path.isfile(path)
        # Files are mode 0600 (RAM, secret-adjacent).
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"

        doc = yaml.safe_load(open(path).read())
        assert doc["current-context"] == "acme0--prod-cluster"
        exec_block = doc["users"][0]["user"]["exec"]
        assert exec_block["args"][:4] == ["--region", "eu-west-1", "eks", "get-token"]
        env = {e["name"]: e["value"] for e in exec_block["env"]}
        assert env["AWS_PROFILE"] == "acme0-ReadOnly"
        assert env["AWS_SHARED_CREDENTIALS_FILE"].endswith(".aws/credentials")

        # No secret material may appear anywhere in the kubeconfig.
        assert "SECRETKEYVALUE" not in open(path).read()
        assert "SESSIONTOKEN" not in open(path).read()
    finally:
        cleanup_aws_files(sid)


async def test_refresh_is_idempotent_and_leaves_no_staging_dir():
    sid = uuid.uuid4()
    eks = _mock_eks({"c1": _cluster_payload("c1")})
    try:
        with patch("boto3.client", return_value=eks):
            await refresh_session_eks(sid, _cred())
            count = await refresh_session_eks(sid, _cred())  # re-select / relaunch

        kube = session_kube_dir(sid)
        assert count == 1
        assert sorted(os.listdir(kube)) == ["eks--acme0--c1"]
        assert not os.path.isdir(f"{kube}.new")  # staging swapped, not left behind
    finally:
        cleanup_aws_files(sid)


async def test_refresh_skips_accounts_without_access():
    sid = uuid.uuid4()
    eks = _mock_eks(error_code="AccessDeniedException")
    try:
        with patch("boto3.client", return_value=eks):
            count = await refresh_session_eks(sid, _cred())
        assert count == 0
        assert os.listdir(session_kube_dir(sid)) == []
    finally:
        cleanup_aws_files(sid)


async def test_cleanup_removes_generated_kubeconfigs():
    sid = uuid.uuid4()
    eks = _mock_eks({"c1": _cluster_payload("c1")})
    with patch("boto3.client", return_value=eks):
        await refresh_session_eks(sid, _cred())
    assert os.path.isdir(session_kube_dir(sid))

    cleanup_aws_files(sid)
    assert not os.path.isdir(session_kube_dir(sid))


async def test_same_cluster_via_two_roles_dedups_and_prefers_eks_role():
    """One kubeconfig per cluster; the eks-scoped role wins over a generic one."""
    sid = uuid.uuid4()
    base = {
        "account_id": "111111111110",
        "account_name": "acme",
        "access_key_id": "AKIA_TEST",
        "secret_access_key": "SECRETKEYVALUE",  # pragma: allowlist secret
        "session_token": "SESSIONTOKEN",  # pragma: allowlist secret
        "region": "eu-west-1",
    }
    # Generic role listed FIRST (discovery/selection order) so we prove preference
    # beats order: the eks role must still win the write.
    cred = MintedCredential(
        connector="aws",
        scope="read",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        secret={
            "region": "eu-west-1",
            "profiles": [
                {**base, "name": "acme-AWSReadOnlyAccess", "role_name": "AWSReadOnlyAccess"},
                {**base, "name": "acme-eks-readonly-access", "role_name": "eks-readonly-access"},
            ],
        },
    )
    # Same mock for every boto3.client() call → both profiles see the same cluster.
    eks = _mock_eks({"prod": _cluster_payload("prod")})
    try:
        with patch("boto3.client", return_value=eks):
            count = await refresh_session_eks(sid, cred)

        kube = session_kube_dir(sid)
        assert count == 1  # one cluster → one file, not two
        assert os.listdir(kube) == ["eks--acme--prod"]
        doc = yaml.safe_load(open(os.path.join(kube, "eks--acme--prod")).read())
        env = {e["name"]: e["value"] for e in doc["users"][0]["user"]["exec"]["env"]}
        assert env["AWS_PROFILE"] == "acme-eks-readonly-access"  # eks role preferred
    finally:
        cleanup_aws_files(sid)


async def test_refresh_no_profiles_is_noop():
    sid = uuid.uuid4()
    cred = MintedCredential(connector="aws", scope="read", expires_at=None, secret={})
    assert await refresh_session_eks(sid, cred) == 0
