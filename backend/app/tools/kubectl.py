"""
Kubectl command builder.

This module builds kubectl commands as argument lists for safe execution.
NEVER uses shell=True or string concatenation for command building.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_kubectl_command(
    tool_name: str, tool_input: dict[str, Any], kubeconfig: str, context: str | None = None
) -> list[str]:
    """
    Build kubectl command as argument list.
    NEVER use shell=True or string concatenation.

    Args:
        tool_name: Name of the kubectl tool (e.g., 'kubectl_get')
        tool_input: Tool input parameters
        kubeconfig: Path to kubeconfig file
        context: Optional kubectl context name

    Returns:
        Command as list of strings for subprocess

    Raises:
        ValueError: If tool_name is unknown
    """
    cmd = ["kubectl", "--kubeconfig", kubeconfig]

    if context:
        cmd.extend(["--context", context])

    logger.debug(f"Building command for {tool_name} with input: {tool_input}")

    if tool_name == "kubectl_get":
        return _build_get_command(cmd, tool_input)
    elif tool_name == "kubectl_describe":
        return _build_describe_command(cmd, tool_input)
    elif tool_name == "kubectl_logs":
        return _build_logs_command(cmd, tool_input)
    elif tool_name == "kubectl_events":
        return _build_events_command(cmd, tool_input)
    elif tool_name == "kubectl_top":
        return _build_top_command(cmd, tool_input)
    elif tool_name == "kubectl_explain":
        return _build_explain_command(cmd, tool_input)
    elif tool_name == "kubectl_api_resources":
        return _build_api_resources_command(cmd, tool_input)
    else:
        logger.error(f"Unknown tool: {tool_name}")
        raise ValueError(f"Unknown tool: {tool_name}")


def _build_get_command(cmd: list[str], tool_input: dict[str, Any]) -> list[str]:
    """
    Build kubectl get command.

    Examples:
        kubectl get pods -n default
        kubectl get pods -A -l app=nginx
        kubectl get pod my-pod -n kube-system -o yaml
    """
    cmd = cmd.copy()
    cmd.append("get")
    cmd.append(tool_input["resource"])

    if tool_input.get("name"):
        cmd.append(tool_input["name"])

    if tool_input.get("all_namespaces"):
        cmd.append("-A")
    elif tool_input.get("namespace"):
        cmd.extend(["-n", tool_input["namespace"]])

    if tool_input.get("selector"):
        cmd.extend(["-l", tool_input["selector"]])

    if tool_input.get("output"):
        cmd.extend(["-o", tool_input["output"]])

    logger.debug(f"Built get command: {cmd}")
    return cmd


def _build_describe_command(cmd: list[str], tool_input: dict[str, Any]) -> list[str]:
    """
    Build kubectl describe command.

    Examples:
        kubectl describe pod my-pod -n default
        kubectl describe deployment my-deploy -n kube-system
    """
    cmd = cmd.copy()
    cmd.append("describe")
    cmd.append(tool_input["resource"])
    cmd.append(tool_input["name"])

    if tool_input.get("namespace"):
        cmd.extend(["-n", tool_input["namespace"]])

    logger.debug(f"Built describe command: {cmd}")
    return cmd


def _build_logs_command(cmd: list[str], tool_input: dict[str, Any]) -> list[str]:
    """
    Build kubectl logs command.

    Examples:
        kubectl logs my-pod -n default --tail 100
        kubectl logs my-pod -c container --previous
        kubectl logs my-pod --since 1h
    """
    cmd = cmd.copy()
    cmd.append("logs")
    cmd.append(tool_input["pod_name"])

    if tool_input.get("namespace"):
        cmd.extend(["-n", tool_input["namespace"]])

    if tool_input.get("container"):
        cmd.extend(["-c", tool_input["container"]])

    # Handle tail parameter
    tail = tool_input.get("tail")
    if tail is not None:
        cmd.extend(["--tail", str(tail)])

    if tool_input.get("previous"):
        cmd.append("--previous")

    if tool_input.get("since"):
        cmd.extend(["--since", tool_input["since"]])

    logger.debug(f"Built logs command: {cmd}")
    return cmd


def _build_events_command(cmd: list[str], tool_input: dict[str, Any]) -> list[str]:
    """
    Build kubectl get events command.

    Examples:
        kubectl get events -n default
        kubectl get events -A --field-selector involvedObject.name=my-pod
        kubectl get events --field-selector type=Warning
    """
    cmd = cmd.copy()
    cmd.append("get")
    cmd.append("events")

    if tool_input.get("all_namespaces"):
        cmd.append("-A")
    elif tool_input.get("namespace"):
        cmd.extend(["-n", tool_input["namespace"]])

    # Build field selector
    field_selectors = []
    if tool_input.get("field_selector"):
        field_selectors.append(tool_input["field_selector"])
    if tool_input.get("types"):
        field_selectors.append(f"type={tool_input['types']}")

    if field_selectors:
        cmd.extend(["--field-selector", ",".join(field_selectors)])

    # Sort by last timestamp for better readability
    cmd.extend(["--sort-by", ".lastTimestamp"])

    logger.debug(f"Built events command: {cmd}")
    return cmd


def _build_top_command(cmd: list[str], tool_input: dict[str, Any]) -> list[str]:
    """
    Build kubectl top command.

    Examples:
        kubectl top pods -n default
        kubectl top nodes
        kubectl top pods --sort-by=cpu
    """
    cmd = cmd.copy()
    cmd.append("top")
    cmd.append(tool_input["resource"])

    if tool_input.get("name"):
        cmd.append(tool_input["name"])

    # Namespace only applies to pods
    if tool_input["resource"] in ["pods", "pod"] and tool_input.get("namespace"):
        cmd.extend(["-n", tool_input["namespace"]])

    if tool_input.get("sort_by"):
        cmd.extend(["--sort-by", tool_input["sort_by"]])

    logger.debug(f"Built top command: {cmd}")
    return cmd


def _build_explain_command(cmd: list[str], tool_input: dict[str, Any]) -> list[str]:
    """
    Build kubectl explain command.

    Examples:
        kubectl explain pod
        kubectl explain pod.spec.containers --recursive
        kubectl explain deployment --api-version=apps/v1
    """
    cmd = cmd.copy()
    cmd.append("explain")
    cmd.append(tool_input["resource"])

    if tool_input.get("recursive"):
        cmd.append("--recursive")

    if tool_input.get("api_version"):
        cmd.extend(["--api-version", tool_input["api_version"]])

    logger.debug(f"Built explain command: {cmd}")
    return cmd


def _build_api_resources_command(cmd: list[str], tool_input: dict[str, Any]) -> list[str]:
    """
    Build kubectl api-resources command.

    Examples:
        kubectl api-resources
        kubectl api-resources --api-group=apps
        kubectl api-resources --namespaced=true
        kubectl api-resources --verbs=get,list
    """
    cmd = cmd.copy()
    cmd.append("api-resources")

    if tool_input.get("api_group"):
        cmd.extend(["--api-group", tool_input["api_group"]])

    # Handle namespaced filter
    namespaced = tool_input.get("namespaced")
    if namespaced is True:
        cmd.append("--namespaced=true")
    elif namespaced is False:
        cmd.append("--namespaced=false")

    if tool_input.get("verbs"):
        cmd.extend(["--verbs", tool_input["verbs"]])

    logger.debug(f"Built api-resources command: {cmd}")
    return cmd
