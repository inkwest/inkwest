"""
Connector tool executors.

Each executor fetches the temporary scoped credential from the in-RAM store using
the session identity in :class:`ToolExecContext`, then injects it into the outbound
call — AWS creds via the subprocess **environment** (never argv, so they cannot
appear in command logs), GitLab/Coralogix via an Authorization header. The model
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
from app.services.connectors.store import get_credential_store
from app.tools.connector_validation import (
    validate_aws_call,
    validate_coralogix_query,
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
    "coralogix_query": "coralogix",
}


def _needs_auth(connector: ConnectorType) -> tuple[str, bool]:
    label = {"aws": "AWS", "gitlab": "GitLab", "coralogix": "Coralogix"}[connector]
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
    """Tmpfs dir holding the session's generated EKS kubeconfigs (one file/cluster).

    A sibling of the ~/.aws projection under the same session root, so the existing
    ``cleanup_aws_files`` teardown removes it too (revoke / session end).
    """
    return os.path.join(_aws_base(), str(session_id), "kube")


def session_kubeconfigs(session_id: UUID) -> list[str]:
    """Absolute paths of the session's generated EKS kubeconfigs (one per cluster).

    Empty when the EKS refresh produced none (no reachable clusters) or has not run
    yet — the caller then leaves ``KUBECONFIG`` unset. Sorted for a deterministic
    merge order (kubectl takes the current-context from the first file).
    """
    kube_dir = session_kube_dir(session_id)
    try:
        return sorted(os.path.join(kube_dir, f) for f in os.listdir(kube_dir))
    except FileNotFoundError:
        return []


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


def cleanup_aws_files(session_id: UUID) -> None:
    """Remove the tmpfs ~/.aws for a session (on revoke / session end)."""
    import shutil

    shutil.rmtree(os.path.join(_aws_base(), str(session_id)), ignore_errors=True)


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
    store = get_credential_store()
    env: dict[str, str] = {}
    injected: list[str] = []

    # When the sandbox projects the per-session creds into the conversation container
    # (sandbox_creds_root set), the env must reference the IN-CONTAINER mount paths
    # (<sandbox_home>/.aws), not the backend's write paths — the container can't see
    # the backend filesystem. With no sandbox, the backend-local write paths are right.
    sandbox_home = settings.sandbox_home.rstrip("/")
    project_to_sandbox = bool(settings.sandbox_creds_root)

    aws = await store.get(user_id, session_id, "aws")
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

    gitlab = await store.get(user_id, session_id, "gitlab")
    if gitlab is not None and gitlab.secret.get("access_token"):
        env["GITLAB_TOKEN"] = gitlab.secret["access_token"]
        env["GITLAB_BASE_URL"] = get_settings().gitlab_base_url
        injected.append("gitlab")

    coralogix = await store.get(user_id, session_id, "coralogix")
    if coralogix is not None and coralogix.secret.get("api_key"):
        env["CORALOGIX_API_KEY"] = coralogix.secret["api_key"]
        if coralogix.secret.get("endpoint"):
            env["CORALOGIX_ENDPOINT"] = coralogix.secret["endpoint"]
        injected.append("coralogix")

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
    "coralogix": (
        "- Coralogix: a read-only API key is in $CORALOGIX_API_KEY for endpoint "
        "$CORALOGIX_ENDPOINT. Query logs/metrics/traces via curl against that endpoint."
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
    if ctx.user_id is None:
        return None
    return await get_credential_store().get(ctx.user_id, ctx.session_id, connector)


async def execute_aws(tool_input: dict[str, Any], ctx: ToolExecContext) -> tuple[str, bool]:
    error = validate_aws_call(tool_input)
    if error:
        return (f"Validation error: {error}", True)

    cred = await _get_cred(ctx, "aws")
    if cred is None or not cred.secret.get("profiles"):
        return _needs_auth("aws")

    service = str(tool_input["service"]).strip().lower()
    operation = str(tool_input["operation"]).strip().lower()
    args = [str(a) for a in tool_input.get("args", [])]

    settings = get_settings()
    cmd = [settings.aws_cli_path, service, operation, *args, "--output", "json"]
    # Optional profile selects one of the granted accounts; default = first profile.
    profile = tool_input.get("profile")
    if profile:
        cmd += ["--profile", str(profile)]
    # Credentials are projected to a tmpfs ~/.aws and referenced via env file paths,
    # NEVER passed in argv — keeps them out of any command logging.
    env = {**os.environ, **write_aws_profiles(ctx.session_id, cred)}
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

    cred = await _get_cred(ctx, "gitlab")
    if cred is None:
        return _needs_auth("gitlab")

    settings = get_settings()
    path = str(tool_input["path"]).strip()
    url = f"{settings.gitlab_base_url.rstrip('/')}/api/v4{path}"
    params = tool_input.get("params") if isinstance(tool_input.get("params"), dict) else None

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {cred.secret['access_token']}"},
                timeout=CONNECTOR_TOOL_TIMEOUT,
            )
    except httpx.HTTPError as e:
        return (f"GitLab request failed: {type(e).__name__}", True)

    if resp.status_code >= 400:
        return (f"GitLab API error {resp.status_code}:\n{resp.text}", True)
    return (truncate_output(resp.text, "gitlab_api"), False)


async def execute_coralogix(tool_input: dict[str, Any], ctx: ToolExecContext) -> tuple[str, bool]:
    error = validate_coralogix_query(tool_input)
    if error:
        return (f"Validation error: {error}", True)

    cred = await _get_cred(ctx, "coralogix")
    if cred is None:
        return _needs_auth("coralogix")

    payload = {
        "query": str(tool_input["query"]),
        "metadata": {
            "syntax": tool_input.get("syntax", "lucene"),
            "time_range": tool_input.get("time_range", "1h"),
        },
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                cred.secret["endpoint"],
                json=payload,
                headers={"Authorization": f"Bearer {cred.secret['api_key']}"},
                timeout=CONNECTOR_TOOL_TIMEOUT,
            )
    except httpx.HTTPError as e:
        return (f"Coralogix request failed: {type(e).__name__}", True)

    if resp.status_code >= 400:
        return (f"Coralogix API error {resp.status_code}:\n{resp.text}", True)
    return (truncate_output(resp.text, "coralogix_query"), False)
