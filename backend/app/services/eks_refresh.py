"""
Per-session EKS kubeconfig refresh.

When a user selects AWS account/role pairs for a session (``POST .../aws/select``),
the backend discovers the EKS clusters reachable with the freshly minted credentials
and writes **one self-contained kubeconfig per cluster** into the session's tmpfs
area (``/dev/shm/inkwest/<session_id>/kube``). Nothing is persisted to disk; the
files are removed on revoke / session end with the rest of the session tmpfs.

This is the in-process port of the ``refresh-eks`` shell helper, with the same discovery + one-kubeconfig-per-cluster output, but scoped to a single
session's profiles, RAM-only, async and idempotent — no ``aws-sso-util``, no
``~/.kube``, no global ``~/.aws``.

Each generated kubeconfig uses the standard AWS exec credential plugin
(``aws eks get-token``); the exec block carries the session's AWS profile and the
tmpfs ``~/.aws`` file paths, so the file works with the credential-free kubectl
executor without leaking secrets into the kubeconfig itself.
"""

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import yaml

from app.core.config import get_settings
from app.services.connectors.base import MintedCredential
from app.tools.connector_exec import session_kube_dir, write_aws_profiles

logger = logging.getLogger(__name__)

# (done, total) progress callback — fired per profile as discovery completes.
ProgressCb = Callable[[int, int], None]

# Background tasks are kept referenced so the event loop does not garbage-collect
# them mid-run (asyncio holds only a weak reference to fire-and-forget tasks).
_tasks: set[asyncio.Task[None]] = set()

# One lock per session serialises the dir-mutating critical section of a refresh with
# itself (overlapping re-selects) and with revoke's in-place clear — so they can't
# interleave a publish and a prune/clear and leave a torn kube set. The credential
# store is per-replica and refresh/revoke run on one event loop, so a plain dict of
# asyncio.Locks is enough; entries are dropped at session end (release_session_lock).
_session_locks: dict[str, asyncio.Lock] = {}


def session_lock(session_id: UUID) -> asyncio.Lock:
    """Get-or-create the per-session refresh/revoke serialisation lock."""
    key = str(session_id)
    lock = _session_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[key] = lock
    return lock


def release_session_lock(session_id: UUID) -> None:
    """Drop a session's lock at session end so the map doesn't grow unbounded."""
    _session_locks.pop(str(session_id), None)


def schedule_session_eks_refresh(
    session_id: UUID,
    cred: MintedCredential,
    *,
    progress_cb: ProgressCb | None = None,
    on_done: Callable[[], None] | None = None,
) -> None:
    """Fire-and-forget the EKS refresh for a session — never blocks the caller.

    No-op when the credential carries no minted profiles (e.g. still provisional);
    ``on_done`` still runs so a caller tracking progress can release its entry.
    ``progress_cb`` is fired per profile during discovery; ``on_done`` runs once the
    task settles (success or failure) — used to end the shared grant progress entry.
    """
    if not cred.secret.get("profiles"):
        if on_done:
            on_done()
        return
    task = asyncio.create_task(_run_safe(session_id, cred, progress_cb, on_done))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run_safe(
    session_id: UUID,
    cred: MintedCredential,
    progress_cb: ProgressCb | None = None,
    on_done: Callable[[], None] | None = None,
) -> None:
    try:
        count = await refresh_session_eks(session_id, cred, progress_cb=progress_cb)
        logger.info("eks_refresh_done", extra={"session_id": str(session_id), "kubeconfigs": count})
    except Exception:  # noqa: BLE001 — background task; failure must not crash the loop
        logger.exception("eks_refresh_failed", extra={"session_id": str(session_id)})
    finally:
        # Always release the progress entry — even on discovery failure the UI must
        # stop waiting (it falls back to closing the popup on the next idle poll).
        if on_done:
            on_done()


async def refresh_session_eks(
    session_id: UUID, cred: MintedCredential, *, progress_cb: ProgressCb | None = None
) -> int:
    """Discover EKS clusters per profile and write one kubeconfig each (tmpfs).

    Idempotent / relaunchable: the kube dir's CONTENTS are republished in place each
    call (files staged then renamed in, stale ones pruned) — the dir inode is never
    swapped, because the conv container bind-mounts it and a mount follows the inode.
    The publish/prune is serialised per session (see session_lock) so overlapping
    re-selects and revoke can't leave a half-written set. Returns the file count.
    """
    profiles = cred.secret.get("profiles") or []
    if not profiles:
        return 0

    # Ensure the tmpfs ~/.aws projection exists so the kubeconfig exec plugin
    # (`aws eks get-token --profile ...`) can resolve credentials at kubectl time.
    aws_env = write_aws_profiles(session_id, cred)
    settings = get_settings()
    # The exec block runs WHERE kubectl runs. For the sandbox/claude-cli provider that
    # is inside the toolbox container, where the session's ~/.aws is bind-mounted at
    # `<sandbox_home>/.aws` — NOT at the backend's write path (`sandbox_creds_root/<sid>/
    # .aws`), which the container can't see. So reference the in-container path, mirroring
    # the translation build_connector_env does for the direct env; otherwise the plugin's
    # AWS_CONFIG_FILE points at a nonexistent path and `aws eks get-token` can't find the
    # profile. With no sandbox, kubectl runs on the backend and the write paths are right.
    if settings.sandbox_creds_root:
        home = settings.sandbox_home.rstrip("/")
        cred_file = f"{home}/.aws/credentials"
        cfg_file = f"{home}/.aws/config"
    else:
        cred_file = aws_env["AWS_SHARED_CREDENTIALS_FILE"]
        cfg_file = aws_env["AWS_CONFIG_FILE"]

    # One list+describe round-trip set per profile; fan out in threads (the boto3
    # low-level client is thread-safe) to avoid a serial N×latency walk.
    clusters = await asyncio.to_thread(_discover_all, profiles, progress_cb)
    # One kubeconfig PER CLUSTER: the same cluster is often reachable via several
    # selected roles in one account; collapse those to a single file.
    clusters = _dedup_prefer_eks(clusters)

    aws_cli = settings.aws_cli_path
    dest = session_kube_dir(session_id)
    # On the shared sandbox creds volume the dir/files must be world-readable: the
    # toolbox container reads them through the read-only bind mount as a (potentially)
    # different uid than the backend — same rationale as write_aws_profiles. On the
    # backend-local /dev/shm projection they stay private. The read-only mount + the
    # per-session isolation is the boundary, not the file mode.
    shared = bool(settings.sandbox_creds_root)
    dir_mode = 0o755 if shared else 0o700
    file_mode = 0o644 if shared else 0o600
    # dest is pre-created at sandbox provisioning and bind-mounted read-only into the
    # conv container. A bind mount follows the INODE, not the path, so we must NEVER
    # swap the dir (an os.replace(dir) would detach the live mount — it'd keep seeing
    # the old, empty inode). Publish in place: stage the files in a sibling dir, then
    # rename each into `dest` (per-FILE rename within the same tmpfs is atomic and
    # leaves the dir inode intact), and prune the kubeconfigs no longer reachable.
    os.makedirs(dest, mode=dir_mode, exist_ok=True)
    os.chmod(dest, dir_mode)  # exist_ok skips mode on an existing dir — force it
    # Per-call staging dir (random suffix): concurrent refreshes for the same session
    # must not share a staging path — one's rmtree would wipe the other's in-flight
    # files. Same parent => same tmpfs => per-file rename into dest stays atomic. The
    # finally removes this call's staging even on error (cleanup_aws_files is the
    # backstop for any orphan a crash leaves behind).
    staging = f"{dest}.new.{uuid4().hex}"
    os.makedirs(staging, mode=0o700, exist_ok=True)
    try:
        desired: set[str] = set()
        for c in clusters:
            fname = f"eks--{c['account_name']}--{c['cluster']}"
            desired.add(fname)
            kubeconfig = _build_kubeconfig(c, aws_cli, cred_file, cfg_file)
            _write_secure(
                os.path.join(staging, fname), yaml.safe_dump(kubeconfig, sort_keys=False), file_mode
            )

        # Publish + prune under the per-session lock so an overlapping re-select or a
        # concurrent revoke can't interleave a rename and a delete — last writer wins
        # with a complete set, never a torn one.
        async with session_lock(session_id):
            for fname in desired:
                os.replace(os.path.join(staging, fname), os.path.join(dest, fname))
            # Drop kubeconfigs for clusters that vanished since a previous refresh.
            for name in os.listdir(dest):
                if name not in desired:
                    try:
                        os.remove(os.path.join(dest, name))
                    except (FileNotFoundError, IsADirectoryError):
                        pass
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return len(clusters)


def _write_secure(path: str, content: str, mode: int = 0o600) -> None:
    # fchmod after open: O_CREAT's mode is ignored when the file already exists, and on
    # the shared creds volume the toolbox (a different uid) must be able to read it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "w") as f:
        f.write(content)


def _dedup_prefer_eks(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse to one entry per cluster (keyed by ARN), choosing the best role.

    When a cluster is reachable via several selected roles, prefer the role whose
    name mentions ``eks``: a purpose-built ``eks-*`` role is the one mapped in the
    cluster's ``aws-auth`` / access entries, whereas a generic ``*ReadOnly`` role
    may be able to ``DescribeCluster`` (so it's discovered) yet have no in-cluster
    RBAC — its token would 403. Ties / no eks-role fall back to discovery order
    (= selection order), so the choice is deterministic.
    """

    def is_eks_role(c: dict[str, Any]) -> bool:
        return "eks" in (c.get("role_name") or "").lower()

    chosen: dict[str, dict[str, Any]] = {}
    for c in clusters:
        cur = chosen.get(c["arn"])
        if cur is None or (not is_eks_role(cur) and is_eks_role(c)):
            chosen[c["arn"]] = c
    return list(chosen.values())


def _discover_all(
    profiles: list[dict[str, Any]], progress_cb: ProgressCb | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    total = len(profiles)
    done = 0
    if progress_cb:
        progress_cb(0, total)
    # ex.map preserves submission order, so ``done`` counts profiles whose
    # list+describe round-trip has returned — monotonic, good enough for the bar.
    with ThreadPoolExecutor(max_workers=min(16, len(profiles))) as ex:
        for res in ex.map(_discover_for_profile, profiles):
            out.extend(res)
            done += 1
            if progress_cb:
                progress_cb(done, total)
    return out


def _discover_for_profile(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """List + describe EKS clusters for one profile (sync; run in a thread).

    Returns [] (and logs) on access/credential errors so a single account the role
    cannot reach never aborts the whole refresh — mirrors the shell helper's
    per-account "no access" skip.
    """
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    region = profile.get("region")
    account_name = profile.get("account_name") or profile.get("account_id")
    try:
        eks: Any = boto3.client(
            "eks",
            region_name=region,
            aws_access_key_id=profile["access_key_id"],
            aws_secret_access_key=profile["secret_access_key"],
            aws_session_token=profile["session_token"],
        )
        names: list[str] = []
        for page in eks.get_paginator("list_clusters").paginate():
            names.extend(page.get("clusters", []))

        result: list[dict[str, Any]] = []
        for name in names:
            cluster = eks.describe_cluster(name=name)["cluster"]
            result.append(
                {
                    "account_name": account_name,
                    "profile": profile["name"],
                    "role_name": profile.get("role_name") or "",
                    "region": region,
                    "cluster": name,
                    "endpoint": cluster["endpoint"],
                    "ca": cluster["certificateAuthority"]["data"],
                    "arn": cluster["arn"],
                }
            )
        return result
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        logger.info("eks_discover_skip", extra={"account": account_name, "code": code})
        return []
    except BotoCoreError:
        logger.warning("eks_discover_error", extra={"account": account_name})
        return []


def _build_kubeconfig(
    cluster: dict[str, Any], aws_cli: str, cred_file: str, cfg_file: str
) -> dict[str, Any]:
    """Build a self-contained exec-plugin kubeconfig for one cluster.

    Equivalent to ``aws eks update-kubeconfig`` output, plus the AWS file-path env
    on the exec block so the credential-free kubectl executor can still resolve the
    session's tmpfs profile. No secret material is embedded — only file paths.
    """
    context_alias = f"{cluster['account_name']}--{cluster['cluster']}"
    arn = cluster["arn"]
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": arn,
                "cluster": {
                    "server": cluster["endpoint"],
                    "certificate-authority-data": cluster["ca"],
                },
            }
        ],
        "contexts": [{"name": context_alias, "context": {"cluster": arn, "user": arn}}],
        "current-context": context_alias,
        "users": [
            {
                "name": arn,
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": aws_cli,
                        "args": [
                            "--region",
                            cluster["region"],
                            "eks",
                            "get-token",
                            "--cluster-name",
                            cluster["cluster"],
                            "--output",
                            "json",
                        ],
                        "env": [
                            {"name": "AWS_PROFILE", "value": cluster["profile"]},
                            {"name": "AWS_SHARED_CREDENTIALS_FILE", "value": cred_file},
                            {"name": "AWS_CONFIG_FILE", "value": cfg_file},
                        ],
                    }
                },
            }
        ],
    }
