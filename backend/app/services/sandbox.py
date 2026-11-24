"""
Per-conversation Docker sandbox manager (claude-cli provider only).

Each conversation **is** its own claude-toolbox container (`inkwest_conv_<sid>`),
paired with a sibling squid proxy (`inkwest_proxy_<sid>`). The toolbox sits on an
``--internal`` network with no direct egress; the proxy is dual-homed (internal +
external) and enforces a domain allowlist, so the toolbox can only reach whitelisted
hosts, and only through the proxy. This mirrors ``poc_squid.sh``.

The backend drives the host Docker daemon by shelling out to the ``docker`` CLI
(Docker-out-of-Docker via a mounted ``/var/run/docker.sock``). Shelling out — rather
than the sync ``docker`` SDK — keeps the module dependency-free and matches the
streaming ``docker exec`` the orchestrator needs anyway (see
:func:`app.services.ai_cli_client.execute_claude_cli`).

Per-conversation files live under ``sandbox_sessions_root/<sid>/`` (the backend owns
that root and mounts subdirs into the containers):

- ``logs/squid/``  → proxy ``access_log`` dir (durable egress audit; tailed for the UI)
- ``claude/``      → conv ``<sandbox_home>/.claude`` (durable Claude state → ``--resume``)
- ``workdir/``     → conv ``/workdir`` (durable agent scratch: git clone, scripts, …)
- AWS creds / kubeconfig → conv ``<sandbox_home>/{.aws,.kube}`` ``--tmpfs`` (RAM only)

Lifecycle: the pair is provisioned lazily on the first turn and reused across turns
(so ``--resume`` continuity holds). A background reaper soft-tears-down idle
conversations (containers freed, durable dir kept so a later turn can resume); an
explicit session delete hard-tears-down (dir removed too). Orphans from a crashed
backend are swept by label on startup.
"""

import asyncio
import contextlib
import errno
import itertools
import os
import shutil
import socket
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core import metrics
from app.core.config import get_settings
from app.core.logging import get_logger
from app.services import app_settings

logger = get_logger(__name__)

# Every container/network we create carries these so a restarted backend (whose
# in-memory registry is gone) can still find and reap its orphans by label.
_LABEL_SANDBOX = "inkwest.sandbox=1"
_REAP_INTERVAL_SECONDS = 60

# Hostnames the squid container must resolve to the BACKEND ITSELF rather than via DNS.
# Deliberately a closed set, not operator-configurable: an arbitrary host mapped to the
# backend would let any allowlisted name reach it, which is an SSRF footgun.
#   host.docker.internal     — local dev; Docker's own alias points at dind's inner bridge
#                              (a dead end), so it must be overridden.
#   inkwest-backend.internal — kube; the broker points here so model traffic stays pod-local
#                              instead of hairpinning through the shared load balancer.
_SELF_HOST_SENTINELS = frozenset({"host.docker.internal", "inkwest-backend.internal"})


@dataclass
class Sandbox:
    """Runtime handle to one conversation's container pair."""

    session_id: str
    conv: str  # inkwest_conv_<tag>  — the conversation container (claude-toolbox)
    proxy: str  # inkwest_proxy_<tag> — its squid proxy container
    int_net: str  # inkwest_intnet_<tag>
    ext_net: str  # inkwest_extnet_<tag>
    last_used: float  # event-loop clock; refreshed by ensure()/release()
    in_use: int = 0  # live turns holding this pair; reaper never touches in_use > 0
    user_id: str | None = None  # owner, for the per-user cap; None = unattributed (no cap)


@dataclass(frozen=True)
class _Names:
    conv: str
    proxy: str
    int_net: str
    ext_net: str


@dataclass(frozen=True)
class SandboxInfo:
    """Read-only snapshot of one running conversation sandbox (for the admin view)."""

    session_id: str  # from the inkwest.session label (canonical, dashes intact)
    user_id: str | None  # owner from the inkwest.user label; None = unattributed
    status: str  # docker Status, e.g. "Up 3 minutes" (conveys uptime)
    in_use: int  # live turns holding the pair (0 when the in-process registry doesn't know it)


# In-process registry + per-session lock so two concurrent turns (e.g. a reconnect
# racing the original) never double-provision the same conversation.
_sandboxes: dict[str, Sandbox] = {}
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_reaper_task: asyncio.Task[None] | None = None


def _now() -> float:
    return asyncio.get_running_loop().time()


def _names(session_id: str) -> _Names:
    # Docker object names allow [a-zA-Z0-9_.-]; UUID hex (dashes stripped) is safe and
    # collision-free. The canonical session id is preserved in the inkwest.session label.
    tag = session_id.replace("-", "")
    return _Names(
        conv=f"inkwest_conv_{tag}",
        proxy=f"inkwest_proxy_{tag}",
        int_net=f"inkwest_intnet_{tag}",
        ext_net=f"inkwest_extnet_{tag}",
    )


def session_dir(session_id: str) -> str:
    """Absolute host path of a conversation's durable directory root."""
    return os.path.join(get_settings().sandbox_sessions_root, session_id)


def squid_log_path(session_id: str) -> str:
    """Absolute host path of a conversation's durable squid access log."""
    return os.path.join(session_dir(session_id), "logs", "squid", "access.log")


# Where the conversation's workdir is mounted inside every sandbox container.
CONTAINER_WORKDIR = "/workdir"


def workdir_path(session_id: str) -> str:
    """Absolute host path of a conversation's workdir (mounted at ``/workdir``)."""
    return os.path.join(session_dir(session_id), "workdir")


# The opencode session store's SQLite file, under the per-conversation ``opencode`` dir.
OPENCODE_STORE_DB = "opencode.db"


def reset_opencode_store(session_id: str) -> None:
    """Empty the conversation's opencode session store, keeping the directory itself.

    opencode resumes with ``--continue``, which resolves to the newest session in ITS OWN
    store — nothing the DB says. After a compaction boundary that store still holds the
    pre-boundary session, so it has to go or the engine keeps replaying the transcript the
    boundary just cut. Only the contents go: the directory is a bind-mount source, and a
    running container keeps the inode it mounted, so a recreated directory is one it never sees.

    Raises ``OSError`` when ``opencode.db`` (the store ``--continue`` resolves against)
    survives; any other leftover is logged and left.
    """
    store = os.path.join(session_dir(session_id), "opencode")
    try:
        entries = list(os.scandir(store))
    except FileNotFoundError:
        return
    except OSError as exc:
        # Unlistable is not empty: the store file check below still decides.
        logger.warning("opencode_store_unlistable", session_id=session_id, error=str(exc))
        entries = []
    for entry in entries:
        # Per entry: one undeletable file must not keep the rest of the session.
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.unlink(entry.path)
        except OSError as exc:
            logger.warning(
                "opencode_store_entry_not_removed",
                session_id=session_id,
                entry=entry.name,
                error=str(exc),
            )
    if os.path.lexists(os.path.join(store, OPENCODE_STORE_DB)):
        raise OSError(f"opencode store not reset: {OPENCODE_STORE_DB} survived")


STAGING_PREFIX = ".publish."


# In the session root, which is never bind-mounted (only its subdirectories are), so the
# sandbox cannot forge which boundary the opencode store was last written under.
OPENCODE_BOUNDARY_FILE = "opencode.boundary"
_NO_BOUNDARY = "none"


def opencode_store_is_stale(session_id: str, boundary_id: str | None) -> bool:
    """Whether the opencode store was last written under another boundary than this one.

    A missing record matches only a never-compacted session.
    """
    path = os.path.join(session_dir(session_id), OPENCODE_BOUNDARY_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except FileNotFoundError:
        recorded = _NO_BOUNDARY
    return recorded != (boundary_id or _NO_BOUNDARY)


def record_opencode_boundary(session_id: str, boundary_id: str | None) -> None:
    """Atomically record the boundary the opencode store's newest session ran under."""
    root = session_dir(session_id)
    staging = os.path.join(root, f"{STAGING_PREFIX}{uuid.uuid4().hex}")
    try:
        with open(staging, "w", encoding="utf-8") as fh:
            fh.write(boundary_id or _NO_BOUNDARY)
        os.replace(staging, os.path.join(root, OPENCODE_BOUNDARY_FILE))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(staging)


def _workdir_parts(relpath: str) -> list[str]:
    parts = relpath.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe workdir relpath: {relpath!r}")
    return parts


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Bounds every first-free-name search, so a sandbox flooding the names cannot spin us.
_MAX_NAME_TRIES = 100
_FRESH_DIR_TRIES = 5


def _file_name(name: str) -> str:
    """``name`` if it is one plain path segment, else ValueError."""
    if _workdir_parts(name) != [name]:
        raise ValueError(f"unsafe workdir file name: {name!r}")
    return name


def _link_no_clobber(src: str, dst_dir_fd: int, names: Iterable[str]) -> str:
    """Hard-link ``src`` under the first of ``names`` that does not exist; returns it.

    ``link`` fails on an existing name where ``rename`` would replace it, so nothing already
    there is ever overwritten.
    """
    for name in itertools.islice(map(_file_name, names), _MAX_NAME_TRIES):
        try:
            os.link(src, name, dst_dir_fd=dst_dir_fd, follow_symlinks=False)
        except FileExistsError:
            continue
        return name
    raise FileExistsError(errno.EEXIST, "no free name to publish under")


# A published file: its workdir relpath and (st_dev, st_ino), so removal hits only this file.
Published = tuple[str, tuple[int, int]]


def _open_workdir_dir(session_id: str, segments: list[str]) -> int:
    """Open ``<workdir>/<segments…>`` as a directory fd, refusing symlinked segments.

    Returns an fd the caller must close. ``O_NOFOLLOW`` makes the *kernel* refuse a
    symlinked segment as it opens it, and every subsequent operation is performed
    relative to that fd — so unlike an ``lstat`` check followed by a path-based write,
    swapping a directory for a symlink after the check cannot re-point the operation
    (CWE-367: the check and the use are the same object, not the same name).
    """
    fd = os.open(workdir_path(session_id), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for segment in segments:
            child = os.open(segment, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _publish_into(
    workdir_fd: int, dir_name: str, staging: str, names_for: Callable[[list[str]], Iterable[str]]
) -> str:
    """Link ``staging`` into ``dir_name``, created if absent; any OSError means unusable."""
    try:
        os.mkdir(dir_name, 0o755, dir_fd=workdir_fd)
        created = True
    except FileExistsError:
        created = False
    fd = os.open(dir_name, _DIR_FLAGS, dir_fd=workdir_fd)
    try:
        if created:
            # The toolbox runs as another uid: a hardened umask would hide the dir from it.
            os.fchmod(fd, 0o755)
        return _link_no_clobber(staging, fd, names_for(os.listdir(fd)))
    finally:
        os.close(fd)


def _publish_into_fresh(
    root_fd: int,
    workdir_fd: int,
    dir_name: str,
    staging: str,
    names_for: Callable[[list[str]], Iterable[str]],
) -> str:
    """Link ``staging`` into a new ``<dir_name>-<hex>`` directory, built outside the mount.

    Filled in the session root, then renamed into the workdir whole: the sandbox never sees
    it empty. Same filesystem: the workdir is a plain subdirectory of the session root.
    """
    stage = f"{STAGING_PREFIX}{uuid.uuid4().hex}"
    os.mkdir(stage, 0o755, dir_fd=root_fd)
    fd = os.open(stage, _DIR_FLAGS, dir_fd=root_fd)
    try:
        os.fchmod(fd, 0o755)
        name = _link_no_clobber(staging, fd, names_for([]))
        for _ in range(_FRESH_DIR_TRIES):
            fresh = f"{dir_name}-{uuid.uuid4().hex[:12]}"
            try:
                os.stat(fresh, dir_fd=workdir_fd, follow_symlinks=False)
                continue
            except FileNotFoundError:
                pass
            # Accepted: an empty directory the sandbox races in at this name is replaced.
            try:
                os.rename(stage, fresh, src_dir_fd=root_fd, dst_dir_fd=workdir_fd)
            except OSError:
                continue
            return f"{fresh}/{name}"
        os.unlink(name, dir_fd=fd)
        os.rmdir(stage, dir_fd=root_fd)
        raise FileExistsError(errno.EEXIST, "no free directory name to publish under")
    finally:
        os.close(fd)


def publish_workdir_file(
    session_id: str,
    dir_name: str,
    names_for: Callable[[list[str]], Iterable[str]],
    content: Iterable[str],
) -> Published:
    """Symlink-safe, no-clobber write of ``content`` under ``<workdir>/<dir_name>``.

    Returns the workdir relpath written and the file's identity. The file name is the first
    free one of ``names_for(<entries already in the directory>)``. ``content`` is written
    chunk by chunk, so a caller can stream a file larger than it wants to hold in memory.

    The workdir is world-writable and bind-mounted into the sandbox, so a prior turn's
    (untrusted) agent can plant anything under it. Nothing it may own is ever moved,
    renamed, chmod'ed or deleted:

    - The directory is opened ``O_NOFOLLOW`` and the file linked relative to that fd, so no
      path segment can be re-pointed, even mid-write.
    - The content is staged OUTSIDE the mount and hard-linked into place: a link never
      follows a symlink at the destination, and refuses an existing name, so nothing
      already there is overwritten.
    - ``dir_name`` is used whenever this process can link into it, whoever owns it; only a
      symlink, a file or an unwritable directory there sends the file to a fresh
      ``<dir_name>-<hex>`` directory, built outside the mount and renamed in whole.
    """
    _file_name(dir_name)
    os.makedirs(workdir_path(session_id), exist_ok=True)
    workdir_fd = os.open(workdir_path(session_id), os.O_RDONLY | os.O_DIRECTORY)
    root_fd = os.open(session_dir(session_id), os.O_RDONLY | os.O_DIRECTORY)
    # Staged in the session root, i.e. one level ABOVE the bind mount, so the agent cannot
    # reach the staging file either.
    staging = os.path.join(session_dir(session_id), f"{STAGING_PREFIX}{uuid.uuid4().hex}")
    try:
        with open(staging, "w", encoding="utf-8") as fh:
            fh.writelines(content)
        os.chmod(staging, 0o644)
        st = os.stat(staging)
        identity = (st.st_dev, st.st_ino)
        try:
            name = _publish_into(workdir_fd, dir_name, staging, names_for)
            return f"{dir_name}/{name}", identity
        except OSError as exc:
            logger.warning(
                "workdir_dir_unusable_fallback",
                session_id=session_id,
                dir=dir_name,
                error=str(exc),
            )
            relpath = _publish_into_fresh(root_fd, workdir_fd, dir_name, staging, names_for)
            return relpath, identity
    finally:
        os.close(root_fd)
        os.close(workdir_fd)
        with contextlib.suppress(FileNotFoundError):
            os.remove(staging)


def remove_workdir_file(session_id: str, published: Published) -> None:
    """Symlink-safe delete of a file :func:`publish_workdir_file` wrote, and only that file.

    Deleting through a joined path would follow a planted symlink and unlink a file outside
    the mount; deleting by name alone would take whatever the sandbox renamed over it since.
    A missing or replaced file is left alone.
    """
    relpath, identity = published
    parts = _workdir_parts(relpath)
    try:
        dir_fd = _open_workdir_dir(session_id, parts[:-1])
    except (OSError, ValueError):
        return
    try:
        st = os.stat(parts[-1], dir_fd=dir_fd, follow_symlinks=False)
        if (st.st_dev, st.st_ino) == identity:
            os.unlink(parts[-1], dir_fd=dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def sweep_staging_files(session_id: str) -> None:
    """Drop staging files and directories orphaned by a crash before the rename."""
    root = session_dir(session_id)
    with contextlib.suppress(OSError):
        for entry in os.scandir(root):
            if entry.name.startswith(STAGING_PREFIX):
                with contextlib.suppress(OSError):
                    if entry.is_dir(follow_symlinks=False):
                        shutil.rmtree(entry.path)
                    else:
                        os.remove(entry.path)


async def _docker(*args: str, timeout: float = 30.0, check: bool = True) -> tuple[int, str, str]:
    """Run a ``docker`` CLI command, returning (returncode, stdout, stderr).

    Raises RuntimeError on a non-zero exit when ``check`` is set, and TimeoutError if
    the command outruns ``timeout`` (the process is killed first).
    """
    docker = get_settings().sandbox_docker_path
    proc = await asyncio.create_subprocess_exec(
        docker,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"docker {args[0]} timed out after {timeout}s") from None
    out = raw_out.decode(errors="replace").strip()
    err = raw_err.decode(errors="replace").strip()
    rc = proc.returncode or 0
    if check and rc != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed (rc={rc}): {err or out}")
    return rc, out, err


async def _inspect(name: str, fmt: str) -> str | None:
    """`docker inspect -f <fmt> <name>`; None if the object doesn't exist."""
    rc, out, _ = await _docker("inspect", "-f", fmt, name, check=False)
    return out if rc == 0 else None


async def _is_running(name: str) -> bool:
    return await _inspect(name, "{{.State.Running}}") == "true"


async def _wait_healthy(name: str, timeout: int) -> None:
    """Block until the container reports healthy, or raise on exit/timeout."""
    deadline = _now() + timeout
    while _now() < deadline:
        health = await _inspect(name, "{{.State.Health.Status}}")
        if health == "healthy":
            return
        if await _inspect(name, "{{.State.Status}}") == "exited":
            raise RuntimeError(f"proxy {name} exited before becoming healthy")
        await asyncio.sleep(1)
    raise TimeoutError(f"proxy {name} not healthy within {timeout}s")


async def _remove_pair(names: _Names) -> None:
    """Best-effort removal of a pair's containers + networks: tolerates already-gone objects
    (``check=False`` swallows non-zero exits). May still raise ``TimeoutError`` if the daemon
    is wedged and a ``docker rm`` outruns its timeout — callers that must not fail (the reaper)
    already run under a try/except; the admin terminate route maps it to a 502."""
    await _docker("rm", "-f", names.conv, names.proxy, check=False, timeout=60)
    # Networks only delete once nothing is attached — containers are gone above.
    await _docker("network", "rm", names.int_net, names.ext_net, check=False)


async def _check_toolbox_image(image: str) -> None:
    """Fail fast with an actionable message when the toolbox image is absent.

    Without this, ``docker run`` tries to pull from the private ECR — which dind has
    no credentials for — and the turn dies on a cryptic auth error.
    """
    rc, _, _ = await _docker("image", "inspect", image, check=False)
    if rc != 0:
        raise RuntimeError(
            f"toolbox image {image!r} not found in the sandbox Docker daemon — "
            "run `make sandbox-pull` to pull it into dind"
        )


async def provision(session_id: str, user_id: str | None = None) -> Sandbox:
    """Create the conversation's container pair + networks. Rolls back on failure.

    Idempotent: any stale same-named objects (e.g. left by a failed prior attempt)
    are removed first, and the durable dirs are reused (so ``--resume`` survives a
    soft teardown). ``user_id`` attributes the pair to its owner (per-user cap + label).
    """
    settings = get_settings()
    await _check_toolbox_image(settings.sandbox_toolbox_image)
    sandbox_home = settings.sandbox_home.rstrip("/")
    names = _names(session_id)
    root = session_dir(session_id)

    # Per-session AWS/kube creds projection. When a shared RAM-backed creds volume is
    # configured, bind-mount THIS session's `<id>/{.aws,kube}` (written by the connector
    # layer on account/role select) read-only into the container, so the agent's `aws`
    # finds the granted profiles. The dirs are pre-created here (backend-owned) so the
    # daemon binds an existing source even before the user grants. Without the volume we
    # fall back to empty tmpfs (creds simply aren't propagated — API-provider only).
    creds_root = settings.sandbox_creds_root
    if creds_root:
        aws_src = os.path.join(creds_root, session_id, ".aws")
        kube_src = os.path.join(creds_root, session_id, "kube")
        # 0755 so the toolbox (a different uid) can traverse to the creds it's granted;
        # the per-session read-only mount is the isolation boundary. The 0600/0644 files
        # are written by the connector layer (write_aws_profiles).
        for path in (aws_src, kube_src):
            os.makedirs(path, mode=0o755, exist_ok=True)
            os.chmod(path, 0o755)  # exist_ok skips mode on an existing dir — force it
        creds_mounts = [
            "-v",
            f"{aws_src}:{sandbox_home}/.aws:ro",
            "-v",
            f"{kube_src}:{sandbox_home}/.kube:ro",
        ]
    else:
        creds_mounts = ["--tmpfs", f"{sandbox_home}/.aws", "--tmpfs", f"{sandbox_home}/.kube"]
    logs_dir = os.path.join(root, "logs", "squid")
    claude_dir = os.path.join(
        root, "claude"
    )  # CLAUDE_CONFIG_DIR -> durable Claude state (--resume)
    work_dir = workdir_path(session_id)
    # opencode engine: its per-conversation session store (SQLite opencode.db + storage/)
    # lives at $HOME/.local/share/opencode in-container; bind-mount it durably so a turn's
    # `--continue` resumes THIS conversation's session across turns + sandbox rebuilds.
    opencode_dir = os.path.join(root, "opencode")
    for path in (logs_dir, claude_dir, work_dir, opencode_dir):
        os.makedirs(path, exist_ok=True)
        # World-writable so the proxy (squid's uid) and the toolbox (its own uid, which may
        # differ from the backend's) can write their state. Per-conversation -> not a leak.
        os.chmod(path, 0o777)  # noqa: S103 — intentional, per-conversation dir
    sweep_staging_files(session_id)

    # Pre-create the squid access log owned by the backend and world-writable. squid (a
    # different uid in the proxy) then appends to it (O_APPEND keeps the existing mode),
    # while the backend's egress tailer can still READ it. Without this, squid creates it
    # as 0640/proxy and the tailer gets EACCES — the egress view stays stuck empty.
    access_log = os.path.join(logs_dir, "access.log")
    try:
        open(access_log, "a").close()  # noqa: SIM115 — just ensure it exists
        os.chmod(access_log, 0o666)  # noqa: S103 — squid (other uid) appends; backend reads
    except PermissionError:
        # Legacy squid-owned file (pre-fix): logs_dir is 0777, so replace it.
        os.unlink(access_log)
        open(access_log, "a").close()  # noqa: SIM115
        os.chmod(access_log, 0o666)  # noqa: S103

    # Clear any leftovers from a crashed attempt so `--name` can't collide.
    await _remove_pair(names)

    labels = ["--label", _LABEL_SANDBOX, "--label", f"inkwest.session={session_id}"]
    if user_id:
        labels += ["--label", f"inkwest.user={user_id}"]
    try:
        await _docker("network", "create", *labels, names.ext_net)
        await _docker("network", "create", "--internal", *labels, names.int_net)

        proxy_run = [
            "run",
            "-d",
            "--name",
            names.proxy,
            "--network",
            names.int_net,
            *labels,
            "-v",
            f"{logs_dir}:/var/log/squid",
            "--health-cmd",
            "squid -k check",
            "--health-interval",
            "2s",
            "--health-timeout",
            "3s",
            "--health-retries",
            "3",
        ]
        if settings.sandbox_squid_conf:
            proxy_run += ["-v", f"{settings.sandbox_squid_conf}:/etc/squid/squid.conf:ro"]
        if settings.sandbox_squid_errors:
            proxy_run += ["-v", f"{settings.sandbox_squid_errors}:/etc/squid/errors:ro"]
        if settings.sandbox_squid_confd:
            # conf.d drop-in for local ACL fragments. Mounted at a DEDICATED /conf.d (NOT
            # the image's /etc/squid/conf.d, which ships `http_access allow localnet`):
            # squid.conf `include`s /conf.d/*.conf, so the stock image's open rule is never
            # in scope and an absent mount fails closed (FATAL) instead of wide-open.
            proxy_run += ["-v", f"{settings.sandbox_squid_confd}:/conf.d:ro"]
        if settings.sandbox_proxy_dns:
            proxy_run += ["--dns", settings.sandbox_proxy_dns]
        # Backend reachability from inside squid: the toolbox egresses ONLY through squid, so
        # the CLI's MCP calls (mcp_proxy_base_url) and its model calls (llm_broker_base_url,
        # when split off) must resolve+reach the backend from the proxy container. Each host
        # must also be squid-allowlisted (see docs/mcp-oauth2-proxy.md §5). Never attach the
        # backend to the per-conversation networks instead — that wedges network teardown.
        #
        # A sentinel host is one Docker/DNS cannot resolve correctly for us: in dev, Docker's
        # `host-gateway` points at dind's INNER bridge (a dead end, nothing serves :8000); in
        # kube, the in-pod broker address has no Service DNS name. Both are fixed the same
        # way — map the name to the backend's OWN address, which the nested squid reaches via
        # dind's NAT. Portable, with no hardcoded subnet (unlike pinning --host-gateway-ip).
        # Squid honours /etc/hosts, which --add-host writes. Any other host is a real DNS name
        # resolved by the proxy's own resolver, so there is nothing to inject.
        backend_hosts = {
            host
            for url in (settings.mcp_proxy_base_url, settings.llm_broker_base_url)
            if url
            for host in (urlparse(url).hostname,)
            if host
        }
        self_hosts = sorted(backend_hosts & _SELF_HOST_SENTINELS)
        for host in backend_hosts - _SELF_HOST_SENTINELS:
            # .internal is reserved (no public DNS), so such a host that is not a known
            # sentinel can never resolve — a typo here otherwise surfaces only as an opaque
            # squid error on the first model/MCP call.
            if host.endswith(".internal"):
                logger.warning("sandbox_backend_host_not_a_sentinel", host=host)
        if self_hosts:
            try:
                self_ip = socket.gethostbyname(socket.gethostname())
                for host in self_hosts:
                    proxy_run += ["--add-host", f"{host}:{self_ip}"]
            except OSError as e:
                # Degrade to no add-host rather than fail provisioning; the affected paths just
                # won't be reachable (logged so the cause is visible, not silent).
                logger.warning("sandbox_self_ip_resolve_failed", error=str(e))
        proxy_run += [settings.sandbox_squid_image]
        await _docker(*proxy_run, timeout=settings.sandbox_provision_timeout)
        # Dual-home the proxy so it can resolve + egress to the internet.
        await _docker("network", "connect", names.ext_net, names.proxy)
        await _wait_healthy(names.proxy, settings.sandbox_provision_timeout)

        conv_run = [
            "run",
            "-d",
            "--name",
            names.conv,
            "--network",
            names.int_net,
            *labels,
            "-e",
            f"HTTP_PROXY=http://{names.proxy}:3128",
            "-e",
            f"HTTPS_PROXY=http://{names.proxy}:3128",
            "-e",
            f"http_proxy=http://{names.proxy}:3128",
            "-e",
            f"https_proxy=http://{names.proxy}:3128",
            "-e",
            "NO_PROXY=localhost,127.0.0.1",
            # Persist Claude Code's state (incl. session transcripts for --resume) on the
            # durable mount at <sandbox_home>/.claude. CLAUDE_CONFIG_DIR points there so it
            # works regardless of the image's own HOME env.
            "-e",
            f"CLAUDE_CONFIG_DIR={sandbox_home}/.claude",
            "-v",
            f"{claude_dir}:{sandbox_home}/.claude",
            "-v",
            f"{work_dir}:/workdir",
            # opencode engine session store (durable; default data dir is $HOME-based, so the
            # target mirrors the image's HOME). Idle for the claude-cli/inkwest-agent engines.
            "-v",
            f"{opencode_dir}:{sandbox_home}/.local/share/opencode",
            # RAM-only creds: the session's ~/.aws/~/.kube, bind-mounted from the shared
            # tmpfs creds volume (or an empty tmpfs when no such volume is configured).
            *creds_mounts,
            "--cpus",
            settings.sandbox_cpu_limit,
            "--memory",
            settings.sandbox_memory_limit,
            "--pids-limit",
            str(settings.sandbox_pids_limit),
        ]
        if settings.sandbox_env_file:
            conv_run += ["--env-file", settings.sandbox_env_file]
        conv_run += [settings.sandbox_toolbox_image, "sleep", "infinity"]
        await _docker(*conv_run, timeout=settings.sandbox_provision_timeout)
    except Exception:
        await _remove_pair(names)
        raise

    return Sandbox(
        session_id=session_id,
        conv=names.conv,
        proxy=names.proxy,
        int_net=names.int_net,
        ext_net=names.ext_net,
        last_used=_now(),
        user_id=user_id,
    )


async def ensure(session_id: str, user_id: str | None = None) -> str:
    """Acquire the conversation container for a turn, provisioning the pair if needed.

    Reuses a live pair across turns (refreshing its idle clock). A pair that died
    out-of-band (OOM kill, proxy crash) is torn down and rebuilt so the turn still
    runs against a working sandbox. ``user_id`` attributes a freshly provisioned pair
    to its owner for the per-user cap; call :func:`enforce_user_sandbox_cap` first.

    Bumps an in-use refcount the caller MUST drop with :func:`release` when the turn
    ends (use a ``finally``). ``last_used`` only marks turn *start*; a turn outliving
    the idle TTL would otherwise be reaped mid-``docker exec``. The refcount, not the
    clock, is what makes the reaper skip a sandbox with a turn in flight.
    """
    async with _locks[session_id]:
        sb = _sandboxes.get(session_id)
        if sb is not None and not (await _is_running(sb.conv) and await _is_running(sb.proxy)):
            logger.warning("sandbox_stale_rebuild", session_id=session_id)
            await _teardown_unlocked(session_id, remove_dir=False)  # already holding the lock
            sb = None
        if sb is None:
            provision_started = _now()
            try:
                sb = await provision(session_id, user_id)
            except Exception:
                metrics.sandbox_provision_total.labels(result="error").inc()
                raise
            metrics.sandbox_provision_total.labels(result="ok").inc()
            metrics.sandbox_provision_duration_seconds.observe(_now() - provision_started)
            _sandboxes[session_id] = sb
            metrics.sandbox_active.set(len(_sandboxes))
            logger.info("sandbox_provisioned", session_id=session_id, conv=sb.conv)
        # Refcount, not a bool: correct only because turn_runner enforces one active
        # turn per session, so ensure()/release() pair 1:1 (in_use is 0 or 1). The
        # stale-rebuild above resets a fresh pair to in_use=0 — safe under that invariant
        # (no other turn holds a lease). If per-session concurrency is ever allowed, a
        # doomed turn's release() would then under-count and the reaper could kill a live one.
        sb.in_use += 1
        sb.last_used = _now()
        return sb.conv


async def release(session_id: str) -> None:
    """Drop the in-use refcount taken by :func:`ensure` and restart the idle clock.

    Restarting ``last_used`` here means the idle TTL is measured from turn *end*, which
    is what "no input in the session" actually means. A no-op if the sandbox is already
    gone (e.g. an explicit session delete tore it down mid-turn)."""
    async with _locks[session_id]:
        sb = _sandboxes.get(session_id)
        if sb is None:
            return
        if sb.in_use > 0:
            sb.in_use -= 1
        sb.last_used = _now()


def in_use(session_id: str) -> bool:
    """Whether a live turn currently holds this conversation's pair.

    The runner frees a session's turn slot on the terminal event while the previous
    turn's generator tail is still running — that tail must not touch shared state
    (config files, the container) a NEW turn may now own. Registry read, no lock:
    callers only use it as a best-effort guard, races are re-checked under the lock
    by :func:`teardown` (``only_if_idle``).
    """
    sb = _sandboxes.get(session_id)
    return bool(sb and sb.in_use > 0)


# Idle pairs (reclaimable) are always reaped back down to the soft cap. Busy pairs (a live
# turn — never killable) may burst above it up to a hard ceiling — at least 2 over, or +25%
# of the cap, whichever is larger (so a small cap still gets a usable burst) — then a new
# turn is refused.
_CAP_HARD_MARGIN_MIN = 2


def hard_ceiling(cap: int) -> int:
    """Absolute concurrent-sandbox ceiling for a soft ``cap``: ``cap + max(2, cap // 4)``.

    Busy pairs may burst up to here; a turn that would push past it is refused. Single
    source of truth so the admin view (app.api.routes.admin) and the enforcer agree.
    """
    return cap + max(_CAP_HARD_MARGIN_MIN, cap // 4)


class SandboxCapExceededError(Exception):
    """A user is at the hard sandbox ceiling with every pair busy — none reclaimable.

    Raised by :func:`enforce_user_sandbox_cap` so the orchestrator refuses the turn with a
    user-facing message instead of provisioning past the ceiling.
    """

    def __init__(self, *, active: int, hard_cap: int) -> None:
        self.active = active
        self.hard_cap = hard_cap
        super().__init__(
            f"Too many conversations running at once ({active}; limit {hard_cap}). "
            "Wait for one to finish, or close an idle conversation, then retry."
        )


def _max_sessions_per_user() -> int:
    """The live per-user sandbox cap: admin override (app_settings) → env default."""
    return app_settings.get_int_setting(
        app_settings.SANDBOX_MAX_SESSIONS_PER_USER_KEY,
        get_settings().sandbox_max_sessions_per_user,
    )


async def _evict(session_id: str) -> None:
    """Soft-reap one sandbox under its per-session lock — for the per-user cap.

    Mirrors the idle reaper: re-validate under the lock and skip if it became busy or
    vanished after the caller's snapshot, so a turn that started in the gap is never
    torn down. Keeps the durable dir, so the conversation cold-starts (and resumes) later.
    """
    async with _locks[session_id]:
        sb = _sandboxes.get(session_id)
        if sb is None or sb.in_use > 0:
            return
        logger.info("sandbox_evict_user_cap", session_id=session_id, user_id=sb.user_id)
        metrics.sandbox_evicted_user_cap_total.inc()
        await _teardown_unlocked(session_id, remove_dir=False)


async def enforce_user_sandbox_cap(user_id: str | None, session_id: str) -> None:
    """Keep a user within their concurrent-sandbox band before a turn provisions a pair.

    Two tiers around the admin cap (see :func:`_max_sessions_per_user` / :func:`hard_ceiling`):

    * ``cap``: the soft limit. Reclaim the user's oldest IDLE pairs down to it. An evicted
      pair keeps its durable dir and cold-starts later (the background idle reaper is a
      separate, always-on mechanism — this only adds per-turn admission).
    * ``hard`` (cap + max(2, 25%)): absolute ceiling. Busy pairs hold a live turn and are
      never killed; if they alone would breach it the turn is REFUSED (raises the cap error).

    Called BEFORE :func:`ensure` so it never holds ``session_id``'s lock while reaping a
    victim's — no nested-lock deadlock. ``user_id`` None or cap ``<= 0`` (unlimited) is a no-op.
    """
    if user_id is None:
        return
    cap = _max_sessions_per_user()
    if cap <= 0:
        return
    hard = hard_ceiling(cap)

    # Atomic snapshot (no await through the reads below): this user's pairs, excluding the
    # incoming session — reused or about to be provisioned, it accounts for exactly one slot.
    owned = [
        sb
        for sb in list(_sandboxes.values())
        if sb.user_id == user_id and sb.session_id != session_id
    ]
    idle = sorted((sb for sb in owned if sb.in_use == 0), key=lambda sb: sb.last_used)
    busy = len(owned) - len(idle)

    # Hard ceiling: busy pairs can't be reclaimed. If they plus this turn would breach it,
    # refuse — no amount of idle eviction can make room.
    if busy + 1 > hard:
        metrics.sandbox_cap_refused_total.inc()
        raise SandboxCapExceededError(active=busy + 1, hard_cap=hard)

    # Reclaim oldest idle down to the soft cap. Only idle are touched, so the floor is
    # busy + 1 — already proven ≤ hard above.
    to_evict = min(len(idle), max(0, len(owned) + 1 - cap))
    for sb in idle[:to_evict]:
        await _evict(sb.session_id)


async def _teardown_unlocked(session_id: str, *, remove_dir: bool) -> None:
    """Tear down a pair WITHOUT taking the per-session lock — for callers that already
    hold it (``ensure``'s dead-pair rebuild, the reaper)."""
    await _remove_pair(_names(session_id))
    _sandboxes.pop(session_id, None)
    metrics.sandbox_active.set(len(_sandboxes))
    if remove_dir:
        shutil.rmtree(session_dir(session_id), ignore_errors=True)
    logger.info("sandbox_torn_down", session_id=session_id, removed_dir=remove_dir)


async def teardown(session_id: str, *, remove_dir: bool = True, only_if_idle: bool = False) -> None:
    """Remove a conversation's containers + networks (best-effort), UNDER the per-session
    lock so it can't race ``ensure()`` provisioning the same conversation (which would
    orphan a freshly-built pair or rmtree a dir a new turn just populated).

    ``remove_dir`` also deletes the durable ``…/<sid>/`` tree — set for an explicit
    session delete, but cleared by the idle reaper so a later turn can resume. Callers
    already holding the lock must use :func:`_teardown_unlocked` instead.

    ``only_if_idle`` skips the teardown when a live turn holds the pair (``in_use > 0``,
    checked under the same lock ``ensure`` uses) — for a finished turn's abnormal-exit
    cleanup, which must not kill a NEXT turn that already re-leased the container.
    """
    async with _locks[session_id]:
        if only_if_idle:
            sb = _sandboxes.get(session_id)
            if sb is not None and sb.in_use > 0:
                logger.info("sandbox_teardown_skipped_in_use", session_id=session_id)
                return
        await _teardown_unlocked(session_id, remove_dir=remove_dir)


async def list_sandboxes() -> list[SandboxInfo]:
    """Live snapshot of running conversation sandboxes — ground truth from the daemon.

    One row per conversation, keyed on the conv container (its squid sibling is folded
    in by filtering on the ``inkwest_conv_`` name). Reflects the actual dind/Docker state
    rather than the in-process registry, so it stays correct across a backend restart
    (orphans the registry forgot still show, with ``in_use=0``).

    Uses ``check=True`` so a wedged/unreachable daemon **raises** (RuntimeError/TimeoutError)
    rather than being silently read as "0 sandboxes" — the admin panel must not conflate an
    outage with an empty list.
    """
    # Tab-delimited because Status (the last field) embeds spaces.
    fmt = '{{.Label "inkwest.session"}}\t{{.Label "inkwest.user"}}\t{{.Status}}'
    _, out, _ = await _docker(
        "ps",
        "--filter",
        f"label={_LABEL_SANDBOX}",
        "--filter",
        "name=inkwest_conv_",
        "--format",
        fmt,
    )
    sandboxes: list[SandboxInfo] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue  # skip blanks / unexpected rows (a conv container must carry a session label)
        session_id, user_id, status = parts[0], parts[1], parts[2]
        sb = _sandboxes.get(session_id)
        sandboxes.append(
            SandboxInfo(
                session_id=session_id,
                user_id=user_id or None,
                status=status,
                in_use=sb.in_use if sb else 0,
            )
        )
    return sandboxes


async def terminate(session_id: str) -> bool:
    """Admin/explicit soft-kill of a running pair: remove its containers + networks but keep
    the durable session dir, so the owner's next turn cold-starts and resumes. Force — unlike
    the idle reaper it kills the pair even mid-turn (that turn's ``docker exec`` then fails).

    Returns True if a pair existed and was torn down, False if there was nothing to do.

    Skips entirely — and crucially does **not** materialize a per-session entry in the
    ``_locks`` defaultdict — for an id that is neither in the registry nor backed by a live
    container. Without that gate, terminating arbitrary ids (the endpoint accepts any UUID)
    would grow ``_locks`` unboundedly. The existence probe uses ``check=True`` so a daemon
    outage raises (surfaced as a 502) instead of being misread as "nothing to terminate".

    Benign, accepted race: a session still inside ``ensure()``'s ``provision()`` window isn't
    in ``_sandboxes`` yet (it's registered only once healthy) and its containers may not exist
    when the probe runs, so a terminate racing provisioning can no-op. Harmless — nothing is
    corrupted, and such a sandbox isn't in the admin list yet anyway; the operator retries once
    it appears. (A registry member always skips the probe and tears down under the lock.)
    """
    if session_id not in _sandboxes:
        names = _names(session_id)
        _, ids, _ = await _docker(
            "ps",
            "-aq",
            "--filter",
            f"name={names.conv}",
            "--filter",
            f"name={names.proxy}",
        )
        if not ids.strip():
            return False
    await teardown(session_id, remove_dir=False)
    return True


async def reap_orphans() -> None:
    """Sweep containers/networks left by a crashed/restarted backend (by label).

    Durable dirs are intentionally kept: a conversation can re-provision lazily and
    reuse its ``home/`` + ``workdir/`` (so ``--resume`` survives a backend restart).
    """
    _, ids, _ = await _docker("ps", "-aq", "--filter", f"label={_LABEL_SANDBOX}", check=False)
    container_ids = ids.split()
    if container_ids:
        await _docker("rm", "-f", *container_ids, check=False, timeout=60)
    _, nets, _ = await _docker(
        "network", "ls", "-q", "--filter", f"label={_LABEL_SANDBOX}", check=False
    )
    network_ids = nets.split()
    for net in network_ids:
        await _docker("network", "rm", net, check=False)
    if container_ids or network_ids:
        logger.info(
            "sandbox_orphans_reaped",
            containers=len(container_ids),
            networks=len(network_ids),
        )


async def _reap_idle() -> None:
    """One reaping sweep: soft-teardown sandboxes idle beyond the TTL.

    Snapshot the idle candidates, then re-validate each UNDER its per-session lock
    before tearing down. ensure()/release() touch ``in_use`` and ``last_used`` under the
    same lock, so a turn that started (or began provisioning) between the snapshot and
    the teardown is seen here and skipped — without this the reaper could ``docker rm -f``
    a container with a live ``docker exec`` turn in flight (one that outran the idle TTL,
    since ``last_used`` only marks the turn's start).
    """
    ttl = get_settings().sandbox_idle_ttl
    candidates = [
        sid
        for sid, sb in list(_sandboxes.items())
        if sb.in_use == 0 and _now() - sb.last_used > ttl
    ]
    for session_id in candidates:
        async with _locks[session_id]:
            sb = _sandboxes.get(session_id)
            if sb is None or sb.in_use > 0 or _now() - sb.last_used <= ttl:
                continue  # gone, busy with a turn, or refreshed after the snapshot
            logger.info("sandbox_reap_idle", session_id=session_id)
            # Keep the durable dir so a later turn can resume the conversation.
            await _teardown_unlocked(session_id, remove_dir=False)  # already holding the lock


async def _reaper_loop() -> None:
    while True:
        try:
            await asyncio.sleep(_REAP_INTERVAL_SECONDS)
            await _reap_idle()
        except asyncio.CancelledError:
            logger.info("sandbox_reaper_cancelled")
            return
        except Exception:
            logger.exception("sandbox_reaper_error")


def start_sandbox_reaper() -> None:
    global _reaper_task
    if _reaper_task is None or _reaper_task.done():
        _reaper_task = asyncio.create_task(_reaper_loop())
        logger.info("sandbox_reaper_started")


async def stop_sandbox_reaper() -> None:
    global _reaper_task
    if _reaper_task and not _reaper_task.done():
        _reaper_task.cancel()
        try:
            await _reaper_task
        except asyncio.CancelledError:
            pass
    _reaper_task = None
    logger.info("sandbox_reaper_stopped")
