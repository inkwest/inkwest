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

import pytest
import yaml

from app.core.config import get_settings
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
        path = os.path.join(session_kube_dir(sid), "config")  # single merged kubeconfig
        assert os.path.isfile(path)
        # File mode reflects the write target: 0644 on the shared sandbox creds volume
        # (must be cross-uid readable — per-session dir isolation is the boundary, and the
        # kubeconfig embeds no secrets), else 0600 on the backend-local /dev/shm projection.
        expected_mode = "0o644" if get_settings().sandbox_creds_root else "0o600"
        assert oct(os.stat(path).st_mode & 0o777) == expected_mode

        doc = yaml.safe_load(open(path).read())
        assert [ctx["name"] for ctx in doc["contexts"]] == ["acme0--eu-west-1--prod-cluster"]
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


async def test_refresh_is_idempotent_and_leaves_no_staging():
    sid = uuid.uuid4()
    eks = _mock_eks({"c1": _cluster_payload("c1")})
    try:
        with patch("boto3.client", return_value=eks):
            await refresh_session_eks(sid, _cred())
            count = await refresh_session_eks(sid, _cred())  # re-select / relaunch

        kube = session_kube_dir(sid)
        assert count == 1
        assert os.listdir(kube) == ["config"]  # single merged file, re-published in place
        assert glob.glob(f"{kube}.config.*") == []  # staging consumed, none left behind
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
    """One context per cluster (merged config); the eks-scoped role wins over a generic one."""
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
        assert count == 1  # one cluster → one context, not two
        assert os.listdir(kube) == ["config"]
        doc = yaml.safe_load(open(os.path.join(kube, "config")).read())
        assert [ctx["name"] for ctx in doc["contexts"]] == ["acme--eu-west-1--prod"]
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

            raw = open(os.path.join(session_kube_dir(sid), "config")).read()
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


def _contexts(kube: str) -> list[str]:
    doc = yaml.safe_load(open(os.path.join(kube, "config")).read())
    return [c["name"] for c in doc["contexts"]]


async def test_refresh_keeps_kube_dir_inode_stable_across_republish():
    """The kube dir is bind-mounted read-only into the conv container; a mount follows
    the inode, so regeneration must publish in place (rename the single `config` file,
    never swap the dir). A re-select with fewer clusters drops the stale context."""
    sid = uuid.uuid4()
    try:
        eks2 = _mock_eks({"c1": _cluster_payload("c1"), "c2": _cluster_payload("c2")})
        with patch("boto3.client", return_value=eks2):
            await refresh_session_eks(sid, _cred())
        kube = session_kube_dir(sid)
        inode_before = os.stat(kube).st_ino
        assert _contexts(kube) == ["acme0--eu-west-1--c1", "acme0--eu-west-1--c2"]

        # Re-select: only c1 now reachable. Dir inode must NOT change; c2 context drops.
        eks1 = _mock_eks({"c1": _cluster_payload("c1")})
        with patch("boto3.client", return_value=eks1):
            await refresh_session_eks(sid, _cred())
        assert os.stat(kube).st_ino == inode_before  # dir not swapped → mount intact
        assert _contexts(kube) == ["acme0--eu-west-1--c1"]  # stale c2 context dropped
        assert glob.glob(f"{kube}.config.*") == []  # per-call staging cleaned up
    finally:
        cleanup_aws_files(sid)


async def test_merged_kubeconfig_wires_each_context_to_its_own_cluster():
    """With multiple clusters in one config, each context's cluster/user refs and the
    exec --cluster-name must resolve to the RIGHT cluster — no cross-wiring in the merge."""
    sid = uuid.uuid4()
    eks = _mock_eks({"c1": _cluster_payload("c1"), "c2": _cluster_payload("c2")})
    try:
        with patch("boto3.client", return_value=eks):
            await refresh_session_eks(sid, _cred())
        doc = yaml.safe_load(open(os.path.join(session_kube_dir(sid), "config")).read())
        cluster_arns = {c["name"] for c in doc["clusters"]}
        exec_cluster = {}  # arn -> exec --cluster-name
        for u in doc["users"]:
            args = u["user"]["exec"]["args"]
            exec_cluster[u["name"]] = args[args.index("--cluster-name") + 1]
        assert len(doc["contexts"]) == 2
        for ctx in doc["contexts"]:
            arn = ctx["context"]["cluster"]
            assert ctx["context"]["user"] == arn  # context binds the same arn for cluster+user
            assert arn in cluster_arns  # cluster ref resolves
            assert arn in exec_cluster  # user ref resolves
            assert arn.endswith("cluster/" + exec_cluster[arn])  # exec targets THIS cluster
    finally:
        cleanup_aws_files(sid)


def test_merged_kubeconfig_disambiguates_colliding_context_aliases():
    """Two distinct ARNs that sanitize to the same alias (e.g. two same-display-named
    accounts, each with a same-named cluster in one region) must still get unique context
    names so `kubectl --context` is unambiguous."""
    from app.services.eks_refresh import _build_merged_kubeconfig

    base = {
        "account_name": "acme",
        "region": "eu-west-1",
        "cluster": "prod",
        "endpoint": "https://x",
        "ca": "Q0E=",
        "profile": "p",
    }
    clusters = [
        {**base, "arn": "arn:aws:eks:eu-west-1:111111111111:cluster/prod"},
        {**base, "arn": "arn:aws:eks:eu-west-1:222222222222:cluster/prod"},
    ]
    names = [c["name"] for c in _build_merged_kubeconfig(clusters, "aws", "/c", "/cfg")["contexts"]]
    assert len(names) == len(set(names)) == 2  # unique despite the same base alias
    assert "acme--eu-west-1--prod" in names
    assert any(n.startswith("acme--eu-west-1--prod--") for n in names)  # hash-suffixed dup


async def test_concurrent_refreshes_use_distinct_staging():
    """Two refreshes for one session must not share a staging path (one's write would
    clobber the other's in-flight file). Distinct random staging => both complete."""
    sid = uuid.uuid4()
    try:
        eks = _mock_eks({"c1": _cluster_payload("c1"), "c2": _cluster_payload("c2")})
        with patch("boto3.client", return_value=eks):
            await asyncio.gather(
                refresh_session_eks(sid, _cred()),
                refresh_session_eks(sid, _cred()),
            )
        kube = session_kube_dir(sid)
        assert os.listdir(kube) == ["config"]
        assert _contexts(kube) == ["acme0--eu-west-1--c1", "acme0--eu-west-1--c2"]
        assert glob.glob(f"{kube}.config.*") == []  # no staging left behind by either run
    finally:
        cleanup_aws_files(sid)


@pytest.mark.parametrize("order", ["revoke_first", "staging_first"])
async def test_refresh_and_revoke_do_not_interleave_under_lock(order: str) -> None:
    """A refresh publishing while a revoke clears in place must serialise on the session
    lock — the terminal kube set is either fully published or fully cleared, never torn.

    Both sides run in threads, so which reaches the session dir first is timing: both orders
    are forced. A revoke landing after the refresh staged drops that staging (cleanup_aws_files)
    and the refresh fails its rename — the revoke wins.
    """
    import threading

    from app.services import eks_refresh
    from app.tools.connector_exec import cleanup_aws_files as cleanup

    sid = uuid.uuid4()
    kube = session_kube_dir(sid)
    revoked = threading.Event()
    real_discover = eks_refresh._discover_all

    def discover_after_revoke(*args, **kwargs):
        assert revoked.wait(5), "revoke never ran"
        return real_discover(*args, **kwargs)

    async def revoke_in_place() -> None:
        # Mirror the real revoke path: cleanup off the loop via to_thread, under lock.
        async with eks_refresh.session_lock(sid):
            if order == "staging_first":
                for _ in range(500):
                    if glob.glob(f"{kube}.config.*"):
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("refresh never staged")
            await asyncio.to_thread(cleanup, sid, keep_dirs=True)
        revoked.set()

    discover = discover_after_revoke if order == "revoke_first" else real_discover
    try:
        eks = _mock_eks({"c1": _cluster_payload("c1"), "c2": _cluster_payload("c2")})
        with (
            patch("boto3.client", return_value=eks),
            patch.object(eks_refresh, "_discover_all", discover),
        ):
            refreshed, revoke_result = await asyncio.gather(
                refresh_session_eks(sid, _cred()), revoke_in_place(), return_exceptions=True
            )

        assert revoke_result is None
        if order == "revoke_first":
            assert refreshed == 2
            assert os.listdir(kube) == ["config"]  # fully published
        else:
            assert isinstance(refreshed, FileNotFoundError), refreshed
            assert os.listdir(kube) == []  # fully cleared
        assert glob.glob(f"{kube}.config.*") == []
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
        orphan = f"{kube}.config.deadbeef"
        open(orphan, "w").close()

        cleanup_aws_files(sid, keep_dirs=True)
        assert os.path.isdir(kube)  # dir kept
        assert os.stat(kube).st_ino == inode  # same inode → live mount keeps working
        assert os.listdir(kube) == []  # contents cleared (revoked)
        assert not os.path.exists(orphan)  # orphaned staging swept

        cleanup_aws_files(sid)  # session end: whole dir gone
        assert not os.path.isdir(kube)
    finally:
        cleanup_aws_files(sid)


async def test_refresh_to_zero_clusters_removes_config_in_place():
    """A re-select that now finds NO reachable clusters must remove the stale `config`
    in place — keeping the bind-mounted dir's inode (a mount follows the inode, so an
    rmtree/recreate here would detach the live mount, the exact bug this design guards)."""
    sid = uuid.uuid4()
    try:
        eks1 = _mock_eks({"c1": _cluster_payload("c1")})
        with patch("boto3.client", return_value=eks1):
            await refresh_session_eks(sid, _cred())
        kube = session_kube_dir(sid)
        inode = os.stat(kube).st_ino
        assert os.listdir(kube) == ["config"]

        # Re-select: zero clusters reachable now (role lost access / clusters gone).
        eks0 = _mock_eks({})
        with patch("boto3.client", return_value=eks0):
            count = await refresh_session_eks(sid, _cred())
        assert count == 0
        assert os.path.isdir(kube)  # dir kept
        assert os.stat(kube).st_ino == inode  # NOT swapped → live mount intact
        assert os.listdir(kube) == []  # stale config removed in place
        assert glob.glob(f"{kube}.config.*") == []  # no staging left behind
    finally:
        cleanup_aws_files(sid)


def test_discover_uses_bounded_boto_client():
    """The EKS client must carry bounded connect/read timeouts + capped retries: discovery
    runs in a non-killable thread pool, so a hung endpoint has to fail fast rather than pin
    a worker thread (cancelling the task can't stop an in-flight boto call)."""
    from app.services import eks_refresh

    captured: dict[str, object] = {}

    def _capture(_service, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return _mock_eks({})

    with patch("boto3.client", side_effect=_capture):
        eks_refresh._discover_for_profile(
            {
                "name": "p",
                "region": "eu-west-1",
                "access_key_id": "AKIA_TEST",
                "secret_access_key": "S",  # pragma: allowlist secret
                "session_token": "T",  # pragma: allowlist secret
            }
        )
    cfg = captured["config"]
    assert cfg.connect_timeout == 5
    assert cfg.read_timeout == 15
    assert cfg.retries["max_attempts"] == 2  # pin the value, tolerate botocore adding keys


def test_safe_seg_neutralizes_path_separators_and_traversal():
    """_safe_seg guards a security rationale: the SSO account_name is an org-controlled
    display string that can carry '/', '..' or spaces; none may survive into a path/context
    segment (a surviving '/' would let a kubeconfig write escape the session dir)."""
    from app.services.eks_refresh import _safe_seg

    evil = "../../etc/passwd name"
    safe = _safe_seg(evil)
    assert "/" not in safe and os.sep not in safe  # no separator survives
    assert " " not in safe  # spaces neutralised too
    # Joining the sanitised segment into a base dir can't escape it.
    assert os.path.dirname(os.path.join("/base", safe)) == "/base"


def test_merged_kubeconfig_sanitizes_malicious_account_name():
    """End-to-end: a malicious account_name must not inject a path separator into the
    context alias built by _build_merged_kubeconfig."""
    from app.services.eks_refresh import _build_merged_kubeconfig

    clusters = [
        {
            "account_name": "../../evil",
            "region": "eu-west-1",
            "cluster": "prod",
            "endpoint": "https://x",
            "ca": "Q0E=",
            "profile": "p",
            "arn": "arn:aws:eks:eu-west-1:111111111111:cluster/prod",
        }
    ]
    [ctx] = _build_merged_kubeconfig(clusters, "aws", "/c", "/cfg")["contexts"]
    assert "/" not in ctx["name"]  # no path separator in the context alias


def test_merged_kubeconfig_uses_noregion_when_profile_has_no_region():
    """A profile with no region must alias to 'noregion' rather than emit a literal
    'None' segment into the context name."""
    from app.services.eks_refresh import _build_merged_kubeconfig

    clusters = [
        {
            "account_name": "acme",
            "region": None,
            "cluster": "prod",
            "endpoint": "https://x",
            "ca": "Q0E=",
            "profile": "p",
            "arn": "arn:aws:eks:eu-west-1:111111111111:cluster/prod",
        }
    ]
    [ctx] = _build_merged_kubeconfig(clusters, "aws", "/c", "/cfg")["contexts"]
    assert ctx["name"] == "acme--noregion--prod"
    assert "None" not in ctx["name"]


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
