"""
Connector tool executors.

Each executor fetches the temporary scoped credential from the in-RAM store using
the session identity in :class:`ToolExecContext`, then injects it into the outbound
call — AWS creds via the subprocess **environment** (never argv, so they cannot
appear in command logs), GitLab via an Authorization header. The model
never sees the credential; it only supplies query parameters.
"""

import asyncio
import logging
import os
import subprocess
from typing import Any
from uuid import UUID

import httpx

from app.core.config import get_settings
from app.core.constants import CONNECTOR_TOOL_TIMEOUT
from app.services.connectors.base import ConnectorType, MintedCredential
from app.services.connectors.renewal import get_or_renew_cred
from app.tools.connector_validation import (
    validate_aws_call,
    validate_gitlab_api,
)
from app.tools.exec_context import ToolExecContext
from app.tools.executor import truncate_output

logger = logging.getLogger(__name__)


# Stable marker embedded in the re-auth message so orchestrators can detect a
# missing/expired connector and surface a first-class "Give access" affordance.
_REAUTH_MARKER = "Give access to"

_TOOL_TO_CONNECTOR: dict[str, ConnectorType] = {
    "aws_call": "aws",
    "gitlab_api": "gitlab",
}


def _needs_auth(connector: ConnectorType) -> tuple[str, bool]:
    label = {"aws": "AWS", "gitlab": "GitLab"}[connector]
    return (
        f"No active {label} access for this session. Ask the user to click "
        f'"{_REAUTH_MARKER} {label}" — the credential may be missing or expired.',
        True,
    )


def connector_for_tool(tool_name: str) -> ConnectorType | None:
    """Return the connector backing a tool name, or None for non-connector tools."""
    return _TOOL_TO_CONNECTOR.get(tool_name)


# AWS profiles are projected to a tmpfs (RAM) ~/.aws so `aws --profile` works with
# several accounts at once, WITHOUT persisting credentials to disk. Files are mode
# 0600, regenerated from the RAM store (the source of truth), and removed on revoke /
# session end.
#
# Base selection, in order:
#   1. ``sandbox_creds_root`` — a RAM-backed volume shared with the dind daemon, so the
#      sandboxed agent's container can bind-mount the session's ~/.aws (see sandbox.py).
#   2. ``/dev/shm/inkwest`` — backend-local RAM tmpfs (no sandbox / API-provider only).
#   3. a temp dir — off-container fallback (e.g. macOS tests); the RAM guarantee matters
#      in the container where the agent actually runs.
def _aws_base() -> str:
    creds_root = get_settings().sandbox_creds_root
    if creds_root:
        return creds_root
    if os.path.isdir("/dev/shm"):  # noqa: S108 — RAM-backed tmpfs, intentional
        return "/dev/shm/inkwest"
    import tempfile

    return os.path.join(tempfile.gettempdir(), "inkwest-aws")


def _aws_session_dir(session_id: UUID) -> str:
    return os.path.join(_aws_base(), str(session_id), ".aws")


def session_kube_dir(session_id: UUID) -> str:
    """Tmpfs dir holding the session's merged EKS kubeconfig (a single ``config`` file).

    A sibling of the ~/.aws projection under the same session root, so the existing
    ``cleanup_aws_files`` teardown removes it too (revoke / session end).
    """
    return os.path.join(_aws_base(), str(session_id), "kube")


def session_kubeconfigs(session_id: UUID) -> list[str]:
    """The session's merged kubeconfig path, or ``[]`` if none has been generated.

    Empty when the EKS refresh produced no reachable clusters or hasn't run yet — the
    caller then leaves ``KUBECONFIG`` unset. (Returns a list to keep the call site
    stable; today it's at most one ``config`` file with a context per cluster.)
    """
    config = os.path.join(session_kube_dir(session_id), "config")
    return [config] if os.path.isfile(config) else []


def _creds_block(name: str, p: dict[str, Any]) -> str:
    return (
        f"[{name}]\n"
        f"aws_access_key_id = {p['access_key_id']}\n"
        f"aws_secret_access_key = {p['secret_access_key']}\n"
        f"aws_session_token = {p['session_token']}\n"
    )


def _write_secure(path: str, content: str, mode: int = 0o600) -> None:
    # fchmod after open: O_CREAT's mode is ignored when the file already exists
    # (idempotent re-write), so force the mode explicitly each time.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
    except BaseException:
        os.close(fd)  # fdopen hasn't taken ownership yet — close the raw fd ourselves
        raise
    with os.fdopen(fd, "w") as f:  # now owns fd; closes it on exit/error
        f.write(content)


def write_aws_profiles(session_id: UUID, cred: MintedCredential) -> dict[str, str]:
    """Project the AWS profiles (RAM store) to a tmpfs ~/.aws and return the env vars.

    Returns {} if the credential has no minted profiles yet. The first profile is
    also written as ``[default]`` so a bare ``aws ...`` works.

    On the shared sandbox creds volume the dir/files are world-readable (0755/0644)
    so the toolbox container — which runs as a different uid than the backend — can
    read them; the per-session read-only bind mount is the isolation boundary (same
    rationale as the 0777 durable dirs in sandbox.py). On the backend-local /dev/shm
    projection they stay 0700/0600 (only the backend, same uid, ever reads them).
    """
    profiles = cred.secret.get("profiles") or []
    if not profiles:
        return {}
    shared = bool(get_settings().sandbox_creds_root)
    dir_mode, file_mode = (0o755, 0o644) if shared else (0o700, 0o600)
    base = _aws_session_dir(session_id)
    os.makedirs(base, mode=dir_mode, exist_ok=True)
    os.chmod(base, dir_mode)  # exist_ok skips mode on an existing dir — force it
    cred_path = os.path.join(base, "credentials")
    cfg_path = os.path.join(base, "config")

    default = profiles[0]
    region = cred.secret.get("region") or default.get("region") or ""

    cred_blocks = [_creds_block("default", default)]
    cfg_blocks = [f"[default]\nregion = {region}\n"]
    for p in profiles:
        cred_blocks.append(_creds_block(p["name"], p))
        cfg_blocks.append(
            f"[profile {p['name']}]\n"
            f"region = {p.get('region') or region}\n"
            f"# {p.get('account_name')} ({p.get('account_id')}) / {p.get('role_name')}\n"
        )

    _write_secure(cred_path, "\n".join(cred_blocks), file_mode)
    _write_secure(cfg_path, "\n".join(cfg_blocks), file_mode)
    return {
        "AWS_SHARED_CREDENTIALS_FILE": cred_path,
        "AWS_CONFIG_FILE": cfg_path,
        "AWS_DEFAULT_REGION": region,
    }


def cleanup_aws_files(session_id: UUID, *, keep_dirs: bool = False) -> None:
    """Remove a session's tmpfs ~/.aws projection + generated EKS kubeconfigs.

    ``keep_dirs=True`` (revoke while the sandbox container is still running): clear the
    CONTENTS of the pre-created ``.aws``/``kube`` dirs in place but keep the dirs
    themselves. The conv container bind-mounts them read-only and a mount follows the
    inode, not the path — dropping the dir would detach the live mount, so a later
    re-grant in the same conversation would never reach the agent. Clearing in place
    leaves the agent seeing empty (revoked) creds through the same stable inode.

    ``keep_dirs=False`` (session end, container gone): drop the whole session dir.
    """
    import shutil

    def _remove(path: str) -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    base = os.path.join(_aws_base(), str(session_id))
    if not keep_dirs:
        shutil.rmtree(base, ignore_errors=True)
        return
    # Full clean while keeping the bind-mounted dirs: empty `.aws`/`kube` in place
    # (stable inode for the live mount) and drop everything else under <id> — notably
    # any `kube.config.<hex>` staging an interrupted refresh may have orphaned.
    try:
        entries = os.listdir(base)
    except FileNotFoundError:
        return
    mounted = {".aws", "kube"}
    for name in entries:
        p = os.path.join(base, name)
        if name in mounted and os.path.isdir(p) and not os.path.islink(p):
            for child in os.listdir(p):
                _remove(os.path.join(p, child))
        else:
            _remove(p)


async def build_connector_env(
    user_id: UUID | None, session_id: UUID
) -> tuple[dict[str, str], list[str]]:
    """Build standard env vars from the session's connected connectors.

    Used by the shell-based (claude-cli) orchestrator: injecting the minted
    credentials as standard env vars (AWS_*, GITLAB_TOKEN, …) lets the agent use
    `aws`, `curl`, etc. naturally. Per-session by construction — the caller passes
    these only to that session's subprocess, so conversations never share creds.
    Read-only is the responsibility of the chosen IAM/SSO role, not of this layer.

    Returns (env_vars, injected_connector_names) — names are safe to log; the
    values (secrets) are not logged.
    """
    if user_id is None:
        return {}, []
    settings = get_settings()
    env: dict[str, str] = {}
    injected: list[str] = []

    # When the sandbox projects the per-session creds into the conversation container
    # (sandbox_creds_root set), the env must reference the IN-CONTAINER mount paths
    # (<sandbox_home>/.aws), not the backend's write paths — the container can't see
    # the backend filesystem. With no sandbox, the backend-local write paths are right.
    sandbox_home = settings.sandbox_home.rstrip("/")
    project_to_sandbox = bool(settings.sandbox_creds_root)

    aws = await get_or_renew_cred(user_id, session_id, "aws")
    # Only a fully selected AWS credential carries minted profiles (provisional =
    # SSO token only). Profiles are written to a tmpfs ~/.aws (RAM, not disk).
    if aws is not None and aws.secret.get("profiles"):
        # Ensure the files exist on the (shared) creds tmpfs; idempotent.
        local_env = write_aws_profiles(session_id, aws)
        if project_to_sandbox:
            env["AWS_SHARED_CREDENTIALS_FILE"] = f"{sandbox_home}/.aws/credentials"
            env["AWS_CONFIG_FILE"] = f"{sandbox_home}/.aws/config"
            if local_env.get("AWS_DEFAULT_REGION"):
                env["AWS_DEFAULT_REGION"] = local_env["AWS_DEFAULT_REGION"]
        else:
            env.update(local_env)
        injected.append("aws")
        # Wire the per-session EKS kubeconfigs (generated on role select) into
        # KUBECONFIG so the agent's `kubectl` finds them. Without this the refresh
        # writes files nothing reads: kubectl falls back to localhost:8080 and the
        # agent wrongly reports "no cluster configured". May be empty on the first
        # turn if discovery hasn't finished — then we just leave KUBECONFIG unset.
        kubeconfigs = session_kubeconfigs(session_id)
        if kubeconfigs:
            if project_to_sandbox:
                env["KUBECONFIG"] = ":".join(
                    f"{sandbox_home}/.kube/{os.path.basename(k)}" for k in kubeconfigs
                )
            else:
                env["KUBECONFIG"] = ":".join(kubeconfigs)
            injected.append("eks")

    gitlab = await get_or_renew_cred(user_id, session_id, "gitlab")
    if gitlab is not None and gitlab.secret.get("access_token"):
        env["GITLAB_TOKEN"] = gitlab.secret["access_token"]
        env["GITLAB_BASE_URL"] = get_settings().gitlab_base_url
        injected.append("gitlab")

    return env, injected


# Per-connector usage hint for shell-based agents (claude-cli). Tells the agent the
# env vars are present and how to use them — the equivalent of the tool descriptions
# the API providers already get. Only connected connectors are listed.
_CONNECTOR_HINTS: dict[str, str] = {
    "aws": (
        "- AWS: the `aws` CLI is preconfigured with read-only profiles (one per "
        "granted account/role). List them with `aws configure list-profiles`, then "
        "use `aws --profile <name> ...` (or bare `aws ...` for the default profile), "
        "e.g. `aws --profile <name> eks list-clusters`, `aws --profile <name> s3 ls`."
    ),
    "eks": (
        "- Kubernetes (EKS): `kubectl` is already configured (KUBECONFIG is set) with "
        "one context per reachable EKS cluster. List them with "
        "`kubectl config get-contexts`, then target one with "
        "`kubectl --context <name> get pods -A`. Do NOT run `aws eks update-kubeconfig` "
        "or otherwise reconfigure kubectl — the contexts are already there."
    ),
    "gitlab": (
        "- GitLab: a read-only token is in $GITLAB_TOKEN for $GITLAB_BASE_URL. Read "
        "code via the API, e.g. "
        '`curl -s -H "Authorization: Bearer $GITLAB_TOKEN" '
        '"$GITLAB_BASE_URL/api/v4/projects?membership=true&per_page=20"`.'
    ),
}


def connector_prompt_hint(injected: list[str]) -> str:
    """Build a prompt section telling a shell agent which connectors it can use.

    Returns "" when no connector is connected.
    """
    lines = [_CONNECTOR_HINTS[c] for c in injected if c in _CONNECTOR_HINTS]
    if not lines:
        return ""
    return (
        "\n\nExternal access is available for this investigation (read-only, "
        "user-granted). Use these when relevant:\n" + "\n".join(lines)
    )


def is_needs_auth(output: str, is_error: bool) -> bool:
    """Whether a tool result indicates a missing/expired connector credential."""
    return is_error and _REAUTH_MARKER in output


async def _get_cred(ctx: ToolExecContext, connector: ConnectorType) -> MintedCredential | None:
    # Renew-on-read: an expired credential with live warm material (GitLab refresh
    # token, AWS SSO token) is silently re-derived instead of failing NEEDS_AUTH.
    return await get_or_renew_cred(ctx.user_id, ctx.session_id, connector)


def _projected_aws_env() -> dict[str, str] | None:
    """The current env when the broker has projected AWS profiles into it, else None.

    In the sandboxed agent the broker pre-provides the session's ~/.aws on a tmpfs and
    injects ``AWS_SHARED_CREDENTIALS_FILE``/``AWS_CONFIG_FILE`` (in-container paths) as
    env at spawn — the same mechanism the claude-cli shell uses. We reuse that env (so
    the squid HTTP(S)_PROXY + PATH the CLI needs are preserved) but **strip the backend's
    Bedrock bearer token**: the agent's container env carries ``AWS_BEARER_TOKEN_BEDROCK``
    for the model call, and the connector ``aws`` tool has no service allowlist
    (least privilege is the IAM role's job) — so leaving it in would let a model-chosen
    ``aws bedrock-runtime ...`` invoke Bedrock on the *backend's* privileged identity
    instead of the user's scoped connector role. Absence/missing file ⇒ no access yet.
    """
    cred_file = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    if not cred_file or not os.path.isfile(cred_file):
        return None
    env = dict(os.environ)
    env.pop("AWS_BEARER_TOKEN_BEDROCK", None)
    return env


async def execute_aws(tool_input: dict[str, Any], ctx: ToolExecContext) -> tuple[str, bool]:
    error = validate_aws_call(tool_input)
    if error:
        return (f"Validation error: {error}", True)

    # Sandboxed agent: creds were pre-provided by the broker (projected tmpfs + env);
    # source them from the environment, never the portal store (one-way handoff).
    if ctx.projected_creds:
        env = _projected_aws_env()
        if env is None:
            return _needs_auth("aws")
    else:
        cred = await _get_cred(ctx, "aws")
        if cred is None or not cred.secret.get("profiles"):
            return _needs_auth("aws")
        # Credentials are projected to a tmpfs ~/.aws and referenced via env file paths,
        # NEVER passed in argv — keeps them out of any command logging.
        env = {**os.environ, **write_aws_profiles(ctx.session_id, cred)}

    service = str(tool_input["service"]).strip().lower()
    operation = str(tool_input["operation"]).strip().lower()
    args = [str(a) for a in tool_input.get("args", [])]

    settings = get_settings()
    cmd = [settings.aws_cli_path, service, operation, *args, "--output", "json"]
    # Optional profile selects one of the granted accounts; default = first profile.
    profile = tool_input.get("profile")
    if profile:
        cmd += ["--profile", str(profile)]
    logger.info("Executing aws_call", extra={"service": service, "operation": operation})

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                subprocess.run,
                cmd,
                capture_output=True,
                text=True,
                timeout=CONNECTOR_TOOL_TIMEOUT,
                env=env,
            ),
            timeout=CONNECTOR_TOOL_TIMEOUT + 5,
        )
    except (subprocess.TimeoutExpired, TimeoutError):
        return (f"AWS command timed out after {CONNECTOR_TOOL_TIMEOUT}s", True)
    except FileNotFoundError:
        return ("aws CLI not found in PATH", True)
    except Exception as e:
        logger.exception("Unexpected error executing aws_call")
        return (f"Execution error: {type(e).__name__}", True)

    if result.returncode != 0:
        err = result.stderr or f"exit code {result.returncode}"
        return (f"Command failed:\n{err}", True)
    return (truncate_output(result.stdout, "aws_call"), False)


async def execute_gitlab(tool_input: dict[str, Any], ctx: ToolExecContext) -> tuple[str, bool]:
    error = validate_gitlab_api(tool_input)
    if error:
        return (f"Validation error: {error}", True)

    settings = get_settings()
    # Sandboxed agent: token pre-provided as env by the broker (one-way handoff); the
    # portal path reads the in-RAM minted credential with renew-on-read.
    if ctx.projected_creds:
        token = os.environ.get("GITLAB_TOKEN")
        if not token:
            return _needs_auth("gitlab")
        base_url = os.environ.get("GITLAB_BASE_URL") or settings.gitlab_base_url
    else:
        cred = await _get_cred(ctx, "gitlab")
        if cred is None:
            return _needs_auth("gitlab")
        token = cred.secret["access_token"]
        base_url = settings.gitlab_base_url

    path = str(tool_input["path"]).strip()
    url = f"{base_url.rstrip('/')}/api/v4{path}"
    params = tool_input.get("params") if isinstance(tool_input.get("params"), dict) else None

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=CONNECTOR_TOOL_TIMEOUT,
            )
    except httpx.HTTPError as e:
        return (f"GitLab request failed: {type(e).__name__}", True)

    if resp.status_code >= 400:
        return (f"GitLab API error {resp.status_code}:\n{resp.text}", True)
    return (truncate_output(resp.text, "gitlab_api"), False)
