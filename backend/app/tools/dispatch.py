"""
Tool dispatch.

Single entry point that routes a tool call to the right executor based on its
name: kubectl tools to the existing kubectl executor, connector tools to the
credential-injecting connector executors. Orchestrators call ``execute_tool``
instead of ``execute_kubectl`` directly so connector tools work uniformly.
"""

import asyncio
import logging
from typing import Any

from app.tools.connector_exec import execute_aws, execute_coralogix, execute_gitlab
from app.tools.exec_context import ToolExecContext
from app.tools.executor import execute_kubectl

logger = logging.getLogger(__name__)


async def execute_tool(
    tool_name: str, tool_input: dict[str, Any], ctx: ToolExecContext
) -> tuple[str, bool]:
    """Execute a tool by name. Returns (output, is_error)."""
    if tool_name == "aws_call":
        return await execute_aws(tool_input, ctx)
    if tool_name == "gitlab_api":
        return await execute_gitlab(tool_input, ctx)
    if tool_name == "coralogix_query":
        return await execute_coralogix(tool_input, ctx)
    # Default: kubectl tools (validation inside rejects unknown names).
    # kubectl tools stay advertised even without a cluster (AWS access can
    # provide one mid-session); degrade gracefully when no kubeconfig is set.
    if ctx.kubeconfig_path is None:
        return (
            "No Kubernetes cluster is configured for this session. "
            "Configure a cluster or AWS access granting EKS before using kubectl tools.",
            True,
        )
    return await execute_kubectl(tool_name, tool_input, ctx.kubeconfig_path, ctx.context_name)


async def execute_tools_parallel(
    tool_calls: list[dict[str, Any]], ctx: ToolExecContext
) -> list[dict[str, Any]]:
    """Execute multiple tool calls concurrently, routing each through ``execute_tool``.

    Mirrors :func:`app.tools.executor.execute_tools_parallel` but is connector-aware
    (kubectl + AWS/GitLab/Coralogix). Used by the OpenAI-format orchestrators.
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
