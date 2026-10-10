"""
Per-session EKS kubeconfig refresh.

When a user selects AWS account/role pairs for a session (``POST .../aws/select``),
the backend discovers the EKS clusters reachable with the freshly minted credentials
and writes **one self-contained kubeconfig per cluster** into the session's RAM-only
kube dir — the shared ``<sandbox_creds_root>/<session_id>/kube`` volume when a sandbox
is configured (bind-mounted read-only into the conv container), else the backend-local
``/dev/shm/inkwest/<session_id>/kube`` tmpfs.

The kubeconfigs are **published in place** (files staged then renamed in, stale ones
pruned), under a per-session lock — the dir inode is never swapped, because the conv
container bind-mounts it and a mount follows the inode. Revoke clears the contents in
place but keeps the dir (``keep_dirs``); session end cancels any in-flight refresh and
removes the whole tree.

This is the in-process port of the ``refresh-eks`` shell helper,
with the same discovery + one-kubeconfig-per-cluster output, but scoped to a single
session's profiles, RAM-only, async and idempotent — no ``aws-sso-util``, no global
``~/.kube`` / ``~/.aws``.

Each kubeconfig uses the standard AWS exec credential plugin (``aws eks get-token``);
the exec block carries the session's AWS profile and the ``~/.aws`` file paths — the
**in-container** ``<sandbox_home>/.aws`` paths on a sandbox, the backend ``/dev/shm``
paths otherwise — so it works with the credential-free kubectl executor without
leaking secrets into the kubeconfig itself.
"""

import asyncio
import hashlib
import logging
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import yaml

from app.core.config import get_settings
from app.services.connectors.base import MintedCredential, ProgressCb
from app.tools.connector_exec import _write_secure, session_kube_dir, write_aws_profiles

logger = logging.getLogger(__name__)


def _safe_seg(value: str) -> str:
    """Sanitize a path/context segment: the AWS SSO account name is a display string
    that can contain '/' or spaces, which in a kubeconfig FILENAME would let the rename
    escape the session dir. Mirror aws._profile_name's allowlist.

    Also strip a leading '.'/'-'/'_' (fall back to 'x' if nothing remains) so the assembled
    context alias always starts alphanumeric — never flag-shaped, never a dotfile, and
    within tools/validation.CONTEXT_PATTERN so every discovered cluster stays targetable
    via the kubectl ``context`` argument."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value).lstrip("._-") or "x"


# Background refresh tasks, tracked per session: kept referenced so the event loop
# doesn't GC them mid-run (asyncio holds only a weak ref to fire-and-forget tasks),
# and so session end can cancel a still-running refresh before it recreates the tmpfs
# dir under a just-removed session (see cancel_session_refresh).
_tasks_by_session: dict[str, set[asyncio.Task[None]]] = {}

# One lock per session serialises the dir-mutating critical section of a refresh with
# itself (overlapping re-selects) and with revoke's in-place clear — so they can't
# interleave a publish and a prune/clear and leave a torn kube set. The credential
# store is per-replica and refresh/revoke run on one event loop, so a plain dict of
# asyncio.Locks is enough; entries are dropped at session end (release_session_lock).
_session_locks: dict[str, asyncio.Lock] = {}


def session_lock(session_id: UUID) -> asyncio.Lock:
    """Get-or-create the per-session refresh/revoke serialisation lock.

    ``setdefault`` (the repo's keyed-lock idiom, cf. connectors/renewal.py) so two
    concurrent callers for one session always get the *same* lock object — never prune
    a lock here: a free lock can be one a caller already holds a reference to but hasn't
    awaited yet, and dropping it would split two ops onto different locks. Bounded by
    active sessions and dropped eagerly at session end (release_session_lock)."""
    return _session_locks.setdefault(str(session_id), asyncio.Lock())


def release_session_lock(session_id: UUID) -> None:
    """Drop a session's lock at session end so the map doesn't grow unbounded."""
    _session_locks.pop(str(session_id), None)


async def cancel_session_refresh(session_id: UUID) -> None:
    """Cancel any in-flight EKS refresh for a session and wait for it to unwind.

    Called at session end before the tmpfs is torn down: otherwise a refresh still in
    its discovery/publish could ``os.makedirs`` the kube dir back *after* cleanup's
    rmtree, orphaning a tmpfs dir for a session that no longer exists.
    """
    tasks = _tasks_by_session.pop(str(session_id), None)
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


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
    key = str(session_id)
    task = asyncio.create_task(_run_safe(session_id, cred, progress_cb, on_done))
    _tasks_by_session.setdefault(key, set()).add(task)

    def _discard(t: asyncio.Task[None]) -> None:
        siblings = _tasks_by_session.get(key)
        if siblings is not None:
            siblings.discard(t)
            if not siblings:
                _tasks_by_session.pop(key, None)

    task.add_done_callback(_discard)


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
    # One context per cluster (keyed by ARN): the same cluster is often reachable via
    # several selected roles in one account; collapse those, preferring the eks-* role.
    clusters = _dedup_prefer_eks(clusters)

    aws_cli = settings.aws_cli_path
    dest = session_kube_dir(session_id)
    # On the shared sandbox creds volume the dir/file must be world-readable: the
    # toolbox container reads them through the read-only bind mount as a (potentially)
    # different uid than the backend — same rationale as write_aws_profiles. On the
    # backend-local /dev/shm projection they stay private. The read-only mount + the
    # per-session isolation is the boundary, not the file mode.
    shared = bool(settings.sandbox_creds_root)
    dir_mode = 0o755 if shared else 0o700
    file_mode = 0o644 if shared else 0o600
    # dest is pre-created at sandbox provisioning and bind-mounted read-only into the
    # conv container. A bind mount follows the INODE, not the path, so we never swap the
    # dir. We write ONE merged kubeconfig (`config`, a context per cluster) and publish
    # it with a single atomic rename into the stable dir — no per-file churn, no prune,
    # and a whole-file swap means the agent never sees a torn set.
    os.makedirs(dest, mode=dir_mode, exist_ok=True)
    os.chmod(dest, dir_mode)  # exist_ok skips mode on an existing dir — force it
    config_path = os.path.join(dest, "config")

    if not clusters:
        # No reachable clusters → remove a stale config in place (keep the dir inode).
        async with session_lock(session_id):
            try:
                os.remove(config_path)
            except FileNotFoundError:
                pass
        return 0

    # Stage on a sibling temp on the same tmpfs (never inside the mounted dir, so a
    # half-written file is invisible to the container), then one atomic rename into
    # place under the per-session lock so it can't interleave with a re-select/revoke.
    staging = f"{dest}.config.{uuid4().hex}"
    try:
        merged = _build_merged_kubeconfig(clusters, aws_cli, cred_file, cfg_file)
        _write_secure(staging, yaml.safe_dump(merged, sort_keys=False), file_mode)
        async with session_lock(session_id):
            os.replace(staging, config_path)
    finally:
        try:
            os.remove(staging)  # no-op once the rename consumed it
        except FileNotFoundError:
            pass
    return len(clusters)


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
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    region = profile.get("region")
    account_name = profile.get("account_name") or profile.get("account_id")
    try:
        # Bounded timeouts + capped retries: discovery runs in a ThreadPoolExecutor that
        # can't be force-killed, so a hung/slow EKS endpoint must fail fast rather than
        # pin a worker thread (cancelling the task can't stop an in-flight boto call).
        eks: Any = boto3.client(
            "eks",
            region_name=region,
            aws_access_key_id=profile["access_key_id"],
            aws_secret_access_key=profile["secret_access_key"],
            aws_session_token=profile["session_token"],
            config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}),
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


def _build_merged_kubeconfig(
    clusters: list[dict[str, Any]], aws_cli: str, cred_file: str, cfg_file: str
) -> dict[str, Any]:
    """Build ONE exec-plugin kubeconfig with a context per reachable cluster.

    Equivalent to merging ``aws eks update-kubeconfig`` output for every cluster, plus
    the AWS file-path env on each exec block so the credential-free kubectl executor
    resolves the session's tmpfs profile. No secret material is embedded — only paths.
    The agent selects a cluster with ``kubectl --context <name>``; current-context is
    the first context by sorted alias, so it's deterministic (not discovery-ordered).
    """
    cfg_clusters: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    users: list[dict[str, Any]] = []
    used_aliases: set[str] = set()
    for c in clusters:
        arn = c["arn"]
        # Sanitize each segment (account_name is an org-controlled display string) and
        # region-qualify so two same-named clusters in different regions stay distinct.
        alias = (
            f"{_safe_seg(c['account_name'])}"
            f"--{_safe_seg(c['region'] or 'noregion')}--{_safe_seg(c['cluster'])}"
        )
        # Two distinct ARNs can still collapse to one alias — e.g. two SSO accounts that
        # share a display name, each with a same-named cluster in the same region. Keep
        # context names unique (kubectl --context must be unambiguous) by suffixing a
        # short, stable ARN hash on collision.
        if alias in used_aliases:
            alias = f"{alias}--{hashlib.sha1(arn.encode()).hexdigest()[:8]}"
        used_aliases.add(alias)
        cfg_clusters.append(
            {
                "name": arn,
                "cluster": {"server": c["endpoint"], "certificate-authority-data": c["ca"]},
            }
        )
        contexts.append({"name": alias, "context": {"cluster": arn, "user": arn}})
        users.append(
            {
                "name": arn,
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": aws_cli,
                        "args": [
                            "--region",
                            c["region"],
                            "eks",
                            "get-token",
                            "--cluster-name",
                            c["cluster"],
                            "--output",
                            "json",
                        ],
                        "env": [
                            {"name": "AWS_PROFILE", "value": c["profile"]},
                            {"name": "AWS_SHARED_CREDENTIALS_FILE", "value": cred_file},
                            {"name": "AWS_CONFIG_FILE", "value": cfg_file},
                        ],
                    }
                },
            }
        )
    contexts.sort(key=lambda ctx: ctx["name"])
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": cfg_clusters,
        "contexts": contexts,
        "users": users,
        "current-context": contexts[0]["name"] if contexts else "",
    }
