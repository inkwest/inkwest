"""
Tests for the per-session EKS kubeconfig refresh (refresh_eks port).

Security-critical invariants: the generated kubeconfigs are written to the session
tmpfs, carry NO secret material (only an exec plugin + file paths), are idempotent
on re-run, degrade gracefully when an account is not reachable, and are removed by
the shared session-tmpfs cleanup.
"""

import asyncio
import glob
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
        path = os.path.join(session_kube_dir(sid), "eks--acme0--eu-west-1--prod-cluster")
        assert os.path.isfile(path)
        # Files are mode 0600 (RAM, secret-adjacent).
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"

        doc = yaml.safe_load(open(path).read())
        assert doc["current-context"] == "acme0--eu-west-1--prod-cluster"
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
        assert sorted(os.listdir(kube)) == ["eks--acme0--eu-west-1--c1"]
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
        assert os.listdir(kube) == ["eks--acme--eu-west-1--prod"]
        doc = yaml.safe_load(open(os.path.join(kube, "eks--acme--eu-west-1--prod")).read())
        env = {e["name"]: e["value"] for e in doc["users"][0]["user"]["exec"]["env"]}
        assert env["AWS_PROFILE"] == "acme-eks-readonly-access"  # eks role preferred
    finally:
        cleanup_aws_files(sid)


async def test_kubeconfig_exec_uses_in_container_aws_paths_in_sandbox(tmp_path):
    """Sandbox: the exec block must point at the in-container ~/.aws mount, not the
    backend's write path — else `aws eks get-token` can't find the profile (the plugin
    runs inside the toolbox where the backend path doesn't exist)."""
    sid = uuid.uuid4()
    eks = _mock_eks({"c1": _cluster_payload("c1")})
    # sandbox_creds_root set => paths must be translated to <sandbox_home>/.aws/*.
    settings = MagicMock(
        aws_cli_path="aws",
        sandbox_creds_root=str(tmp_path),
        sandbox_home="/home/sandbox",
    )
    try:
        # session_kube_dir / cleanup resolve their base via get_settings too, so keep
        # everything that touches paths inside the patch context.
        with (
            patch("boto3.client", return_value=eks),
            patch("app.services.eks_refresh.get_settings", return_value=settings),
            patch("app.tools.connector_exec.get_settings", return_value=settings),
        ):
            await refresh_session_eks(sid, _cred())

            raw = open(os.path.join(session_kube_dir(sid), "eks--acme0--eu-west-1--c1")).read()
            env = {
                e["name"]: e["value"]
                for e in yaml.safe_load(raw)["users"][0]["user"]["exec"]["env"]
            }
            assert env["AWS_CONFIG_FILE"] == "/home/sandbox/.aws/config"
            assert env["AWS_SHARED_CREDENTIALS_FILE"] == "/home/sandbox/.aws/credentials"
            assert str(tmp_path) not in raw  # backend write path must NOT leak in
            cleanup_aws_files(sid)
    finally:
        cleanup_aws_files(sid)


async def test_refresh_no_profiles_is_noop():
    sid = uuid.uuid4()
    cred = MintedCredential(connector="aws", scope="read", expires_at=None, secret={})
    assert await refresh_session_eks(sid, cred) == 0


def test_discover_all_reports_progress_per_profile():
    """progress_cb fires (0, total) up front, then one tick per completed profile."""
    from app.services import eks_refresh

    profiles = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    calls: list[tuple[int, int]] = []
    with patch.object(eks_refresh, "_discover_for_profile", side_effect=lambda p: []):
        eks_refresh._discover_all(profiles, lambda done, total: calls.append((done, total)))
    assert calls[0] == (0, 3)
    assert calls[-1] == (3, 3)
    assert [d for d, _ in calls] == [0, 1, 2, 3]  # monotonic


async def test_schedule_no_profiles_still_runs_on_done():
    """on_done must fire even with nothing to discover, so a tracker releases its entry."""
    from app.services import eks_refresh

    fired: list[int] = []
    cred = MintedCredential(connector="aws", scope="read", expires_at=None, secret={})
    eks_refresh.schedule_session_eks_refresh(uuid.uuid4(), cred, on_done=lambda: fired.append(1))
    assert fired == [1]


async def test_run_safe_calls_on_done_on_success_and_failure():
    """The shared progress entry must be released whether discovery succeeds or raises."""
    from unittest.mock import AsyncMock

    from app.services import eks_refresh

    fired: list[str] = []
    with patch.object(eks_refresh, "refresh_session_eks", AsyncMock(return_value=2)):
        await eks_refresh._run_safe(uuid.uuid4(), MagicMock(), None, lambda: fired.append("ok"))
    assert fired == ["ok"]

    with patch.object(eks_refresh, "refresh_session_eks", AsyncMock(side_effect=RuntimeError())):
        await eks_refresh._run_safe(uuid.uuid4(), MagicMock(), None, lambda: fired.append("fail"))
    assert fired == ["ok", "fail"]


async def test_refresh_keeps_kube_dir_inode_stable_and_prunes_stale():
    """The kube dir is bind-mounted read-only into the conv container; a mount follows
    the inode, so regeneration must publish in place (no dir swap) — else the container
    keeps seeing the old, empty dir. A re-select with fewer clusters must also prune."""
    sid = uuid.uuid4()
    try:
        eks2 = _mock_eks({"c1": _cluster_payload("c1"), "c2": _cluster_payload("c2")})
        with patch("boto3.client", return_value=eks2):
            await refresh_session_eks(sid, _cred())
        kube = session_kube_dir(sid)
        inode_before = os.stat(kube).st_ino
        assert sorted(os.listdir(kube)) == [
            "eks--acme0--eu-west-1--c1",
            "eks--acme0--eu-west-1--c2",
        ]

        # Re-select: only c1 now reachable. Inode must NOT change; c2 must be pruned.
        eks1 = _mock_eks({"c1": _cluster_payload("c1")})
        with patch("boto3.client", return_value=eks1):
            await refresh_session_eks(sid, _cred())
        assert os.stat(kube).st_ino == inode_before  # dir not swapped → mount intact
        assert sorted(os.listdir(kube)) == ["eks--acme0--eu-west-1--c1"]  # stale c2 dropped
        assert glob.glob(f"{kube}.new*") == []  # per-call staging cleaned up
    finally:
        cleanup_aws_files(sid)


async def test_concurrent_refreshes_use_distinct_staging():
    """Two refreshes for one session must not share a staging dir (one's rmtree would
    wipe the other's in-flight files). Distinct random staging => both complete."""
    sid = uuid.uuid4()
    try:
        eks = _mock_eks({"c1": _cluster_payload("c1"), "c2": _cluster_payload("c2")})
        with patch("boto3.client", return_value=eks):
            await asyncio.gather(
                refresh_session_eks(sid, _cred()),
                refresh_session_eks(sid, _cred()),
            )
        kube = session_kube_dir(sid)
        assert sorted(os.listdir(kube)) == [
            "eks--acme0--eu-west-1--c1",
            "eks--acme0--eu-west-1--c2",
        ]
        assert glob.glob(f"{kube}.new*") == []  # no staging left behind by either run
    finally:
        cleanup_aws_files(sid)


async def test_refresh_and_revoke_do_not_interleave_under_lock():
    """A refresh publishing while a revoke clears in place must serialise on the session
    lock — the terminal kube set is either fully published or fully cleared, never torn."""
    from app.services import eks_refresh
    from app.tools.connector_exec import cleanup_aws_files as cleanup

    sid = uuid.uuid4()
    try:
        eks = _mock_eks({"c1": _cluster_payload("c1"), "c2": _cluster_payload("c2")})

        async def revoke_in_place() -> None:
            # Mirror the real revoke path: cleanup off the loop via to_thread, under lock.
            async with eks_refresh.session_lock(sid):
                await asyncio.to_thread(cleanup, sid, keep_dirs=True)

        with patch("boto3.client", return_value=eks):
            await asyncio.gather(refresh_session_eks(sid, _cred()), revoke_in_place())

        kube = session_kube_dir(sid)
        full = ["eks--acme0--eu-west-1--c1", "eks--acme0--eu-west-1--c2"]
        names = sorted(os.listdir(kube))
        assert names == [] or names == full  # fully published or fully cleared, not torn
        assert glob.glob(f"{kube}.new*") == []
    finally:
        cleanup(sid)


async def test_cleanup_keep_dirs_clears_contents_but_preserves_inode():
    """Revoke while the container is alive must clear creds in place (stable inode), not
    rmtree the bind-mounted dirs; the default still drops the whole session dir."""
    sid = uuid.uuid4()
    try:
        eks = _mock_eks({"c1": _cluster_payload("c1")})
        with patch("boto3.client", return_value=eks):
            await refresh_session_eks(sid, _cred())
        kube = session_kube_dir(sid)
        inode = os.stat(kube).st_ino
        assert os.listdir(kube)  # populated

        # Orphaned staging from a crashed refresh must be swept by the full clean.
        orphan = f"{kube}.new.deadbeef"
        os.makedirs(orphan, exist_ok=True)

        cleanup_aws_files(sid, keep_dirs=True)
        assert os.path.isdir(kube)  # dir kept
        assert os.stat(kube).st_ino == inode  # same inode → live mount keeps working
        assert os.listdir(kube) == []  # contents cleared (revoked)
        assert not os.path.isdir(orphan)  # orphaned staging swept

        cleanup_aws_files(sid)  # session end: whole dir gone
        assert not os.path.isdir(kube)
    finally:
        cleanup_aws_files(sid)


async def test_cancel_session_refresh_cancels_in_flight_task():
    """Session end must cancel a still-running refresh so it can't recreate the tmpfs
    after cleanup. The task is tracked per session and dropped once cancelled."""
    from app.services import eks_refresh

    sid = uuid.uuid4()
    started = asyncio.Event()
    blocked = asyncio.Event()  # never set → the stub blocks until cancelled

    async def slow_refresh(session_id, cred, *, progress_cb=None):  # noqa: ANN001, ANN202
        started.set()
        await blocked.wait()
        return 0

    try:
        with patch.object(eks_refresh, "refresh_session_eks", slow_refresh):
            eks_refresh.schedule_session_eks_refresh(sid, _cred())
            await asyncio.wait_for(started.wait(), timeout=1)
            assert str(sid) in eks_refresh._tasks_by_session  # tracked while running
            await eks_refresh.cancel_session_refresh(sid)
            assert str(sid) not in eks_refresh._tasks_by_session  # cancelled + dropped
    finally:
        eks_refresh._tasks_by_session.pop(str(sid), None)
