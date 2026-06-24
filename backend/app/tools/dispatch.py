"""
Tool dispatch.

Single entry point that routes a tool call to the right executor based on its
name: kubectl tools to the existing kubectl executor, connector tools to the
credential-injecting connector executors. Orchestrators call ``execute_tool``
instead of ``execute_kubectl`` directly so connector tools work uniformly.
"""

import asyncio
import logging
import os
from typing import Any

from app.core.config import get_settings
from app.services.connectors.renewal import get_or_renew_cred
from app.tools.connector_exec import (
    execute_aws,
    execute_gitlab,
    session_kubeconfigs,
    write_aws_profiles,
)
from app.tools.exec_context import ToolExecContext
from app.tools.executor import execute_kubectl

logger = logging.getLogger(__name__)


async def _resolve_eks_kubeconfig(
    ctx: ToolExecContext,
) -> tuple[str | None, dict[str, str] | None]:
    """Resolve the session's EKS-discovered kubeconfig + the env its exec plugin needs.

    Granting AWS access that can reach EKS generates a merged kubeconfig per session (one
    context per reachable cluster — see ``eks_refresh``). The shell agent gets it via
    ``KUBECONFIG``, but the tool-based engines target kubectl by an explicit
    ``--kubeconfig`` path, so resolve it here when no cluster is statically configured.

    Returns ``(kubeconfig_path, extra_env)``:
      - Sandboxed agent (``projected_creds``): the broker already put the in-container
        merged config on ``KUBECONFIG`` and the AWS profile env in the process env, so use
        that path and inject nothing (``aws eks get-token`` reads the ambient AWS env). The
        portal credential store is unreachable here (one-way handoff), so never call it.
      - Backend-run providers: resolve the backend-local merged config and project the
        session's tmpfs ``~/.aws`` so the exec plugin can mint a token (that env is not in
        the backend process otherwise).
    """
    if ctx.projected_creds:
        return (os.environ.get("KUBECONFIG") or None, None)
    # Backend-run kubectl can authenticate only when the generated kubeconfig's exec block
    # points at backend-local ~/.aws paths — which it does ONLY without a shared sandbox
    # creds volume. With sandbox_creds_root set, that exec block carries in-container
    # <sandbox_home>/.aws paths the backend can't read (see eks_refresh), so EKS belongs to
    # the sandbox engines, not here: report "no cluster" cleanly instead of a get-token
    # failure the backend can't fix.
    if get_settings().sandbox_creds_root:
        return (None, None)
    configs = session_kubeconfigs(ctx.session_id)
    if not configs:
        return (None, None)
    cred = await get_or_renew_cred(ctx.user_id, ctx.session_id, "aws")
    env = (
        write_aws_profiles(ctx.session_id, cred)
        if cred is not None and cred.secret.get("profiles")
        else None
    )
    return (configs[0], env)


async def execute_tool(
    tool_name: str, tool_input: dict[str, Any], ctx: ToolExecContext
) -> tuple[str, bool]:
    """Execute a tool by name. Returns (output, is_error)."""
    if tool_name == "aws_call":
        return await execute_aws(tool_input, ctx)
    if tool_name == "gitlab_api":
        return await execute_gitlab(tool_input, ctx)
    # Default: kubectl tools (validation inside rejects unknown names).
    # A per-call `context` lets the agent target a specific discovered cluster; it falls
    # back to the session's configured context.
    context = tool_input.get("context") or ctx.context_name
    kubeconfig = ctx.kubeconfig_path
    extra_env: dict[str, str] | None = None
    # kubectl tools stay advertised even without a statically-configured cluster: AWS
    # access granting EKS provides one mid-session via the generated kubeconfig.
    if kubeconfig is None:
        kubeconfig, extra_env = await _resolve_eks_kubeconfig(ctx)
    if kubeconfig is None:
        return (
            "No Kubernetes cluster is configured for this session. "
            "Configure a cluster or grant AWS access that can reach an EKS cluster "
            "before using kubectl tools.",
            True,
        )
    return await execute_kubectl(tool_name, tool_input, kubeconfig, context, extra_env=extra_env)


async def execute_tools_parallel(
    tool_calls: list[dict[str, Any]], ctx: ToolExecContext
) -> list[dict[str, Any]]:
    """Execute multiple tool calls concurrently, routing each through ``execute_tool``.

    Mirrors :func:`app.tools.executor.execute_tools_parallel` but is connector-aware
    (kubectl + AWS/GitLab). Used by the OpenAI-format orchestrators.
    """

    async def run_one(tc: dict[str, Any]) -> dict[str, Any]:
        tool_id = tc.get("id", "unknown")
        try:
            output, is_error = await execute_tool(tc.get("name", ""), tc.get("input", {}), ctx)
        except Exception as e:  # noqa: BLE001 — surfaced to the model as an error result
            logger.exception("Exception executing tool %s", tool_id)
            output, is_error = (f"Error: {type(e).__name__}: {e}", True)
        return {"tool_use_id": tool_id, "content": output, "is_error": is_error}

    return await asyncio.gather(*[run_one(tc) for tc in tool_calls])
