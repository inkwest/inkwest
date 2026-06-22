"""
Kubectl tool definitions for Claude API.

This module defines all available kubectl tools that Claude can use
to interact with Kubernetes clusters.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

KUBECTL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "kubectl_get",
        "description": (
            "Get Kubernetes resources. Lists or retrieves specific"
            " resources like pods, services, deployments,"
            " configmaps, etc. Use this as a first step to"
            " understand what exists in the cluster."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "description": (
                        "Resource type (pods, services,"
                        " deployments, configmaps, secrets,"
                        " nodes, namespaces, events,"
                        " ingresses, pvc, etc.)"
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Specific resource name. Omit to list all.",
                },
                "namespace": {
                    "type": "string",
                    "description": "Namespace to query. Defaults to 'default'.",
                    "default": "default",
                },
                "selector": {
                    "type": "string",
                    "description": (
                        "Label selector to filter resources (e.g., 'app=nginx,env=prod')"
                    ),
                },
                "output": {
                    "type": "string",
                    "enum": ["wide", "yaml", "json", "name"],
                    "description": (
                        "Output format. Use 'wide' for more columns, 'yaml' for full"
                        " spec, 'name' for just resource names."
                    ),
                },
                "all_namespaces": {
                    "type": "boolean",
                    "description": "Search across all namespaces.",
                    "default": False,
                },
            },
            "required": ["resource"],
        },
    },
    {
        "name": "kubectl_describe",
        "description": (
            "Show detailed information about a specific resource"
            " including events, conditions, and status."
            " Essential for debugging why a resource is"
            " in a bad state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "description": "Resource type (pod, service, deployment, node, etc.)",
                },
                "name": {"type": "string", "description": "Resource name to describe"},
                "namespace": {
                    "type": "string",
                    "description": "Namespace of the resource. Defaults to 'default'.",
                    "default": "default",
                },
            },
            "required": ["resource", "name"],
        },
    },
    {
        "name": "kubectl_logs",
        "description": (
            "Get container logs from a pod. Critical for"
            " debugging application crashes, errors,"
            " and unexpected behavior."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pod_name": {"type": "string", "description": "Name of the pod"},
                "namespace": {
                    "type": "string",
                    "description": "Namespace of the pod. Defaults to 'default'.",
                    "default": "default",
                },
                "container": {
                    "type": "string",
                    "description": "Container name (required for multi-container pods)",
                },
                "tail": {
                    "type": "integer",
                    "description": "Number of recent lines to show",
                    "default": 100,
                },
                "previous": {
                    "type": "boolean",
                    "description": "Get logs from previous (crashed) container instance",
                    "default": False,
                },
                "since": {
                    "type": "string",
                    "description": "Show logs since duration (e.g., '1h', '30m')",
                },
            },
            "required": ["pod_name"],
        },
    },
    {
        "name": "kubectl_events",
        "description": (
            "Get cluster events. Events reveal what Kubernetes"
            " is doing: scheduling, pulling images, mounting"
            " volumes, health check failures, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {
                    "type": "string",
                    "description": (
                        "Namespace to get events from. If not specified, uses default namespace."
                    ),
                },
                "all_namespaces": {
                    "type": "boolean",
                    "description": "Get events from all namespaces.",
                    "default": False,
                },
                "field_selector": {
                    "type": "string",
                    "description": "Filter events (e.g., 'involvedObject.name=my-pod')",
                },
                "types": {
                    "type": "string",
                    "enum": ["Normal", "Warning"],
                    "description": "Filter by event type",
                },
            },
        },
    },
    {
        "name": "kubectl_top",
        "description": (
            "Show CPU and memory usage for pods or nodes. Requires metrics-server in the cluster."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "enum": ["pods", "nodes"],
                    "description": "Resource type to get metrics for",
                },
                "name": {"type": "string", "description": "Specific pod or node name"},
                "namespace": {"type": "string", "description": "Namespace for pod metrics"},
                "sort_by": {
                    "type": "string",
                    "enum": ["cpu", "memory"],
                    "description": "Sort by CPU or memory usage",
                },
            },
            "required": ["resource"],
        },
    },
    {
        "name": "kubectl_explain",
        "description": (
            "Get documentation for resource fields."
            " Useful for understanding what fields are"
            " available and their meanings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource": {
                    "type": "string",
                    "description": (
                        "Resource type or field path (e.g., 'pod', 'pod.spec.containers')"
                    ),
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Show all nested fields recursively",
                    "default": False,
                },
                "api_version": {
                    "type": "string",
                    "description": "API version to explain (e.g., 'apps/v1')",
                },
            },
            "required": ["resource"],
        },
    },
    {
        "name": "kubectl_api_resources",
        "description": (
            "List all available API resources in the cluster."
            " Useful for discovering what resource types"
            " are available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "api_group": {
                    "type": "string",
                    "description": ("Filter by API group (e.g., 'apps', 'networking.k8s.io')"),
                },
                "namespaced": {
                    "type": "boolean",
                    "description": (
                        "If true, show only namespaced"
                        " resources. If false, show only"
                        " cluster-scoped."
                    ),
                },
                "verbs": {
                    "type": "string",
                    "description": (
                        "Filter by supported verbs (comma-separated, e.g., 'get,list,watch')"
                    ),
                },
            },
        },
    },
]


# =============================================================================
# External Access Connector tools
#
# These tools carry ONLY query parameters — never a credential, token, account or
# key. The backend injects the temporary scoped credential from the RAM store at
# execution time. If the connector is not connected, the tool returns a re-auth
# message so the model can ask the user to click "Give access".
# =============================================================================

CONNECTOR_TOOLS: list[dict[str, Any]] = [
    {
        "name": "aws_call",
        "description": (
            "Run an AWS CLI call using the temporary credentials the user granted."
            " The user may grant several accounts/roles at once — each is an AWS"
            " profile. Omit 'profile' to use the default (first) profile, or pass a"
            " profile name to target a specific account. Permissions are capped by"
            " the selected read-only role. Use to inspect infrastructure (EKS, EC2,"
            " CloudWatch, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "AWS service, e.g. 'eks', 'ec2', 'cloudwatch', 'logs'.",
                },
                "operation": {
                    "type": "string",
                    "description": (
                        "Read-only operation in kebab-case, e.g. 'describe-cluster',"
                        " 'list-clusters', 'describe-instances'."
                    ),
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Additional CLI arguments, e.g. ['--name', 'my-cluster']."
                        " Provide flags and values as separate items."
                    ),
                },
                "profile": {
                    "type": "string",
                    "description": (
                        "Optional AWS profile (one of the granted account/role"
                        " profiles). Defaults to the first granted profile."
                    ),
                },
            },
            "required": ["service", "operation"],
        },
    },
    {
        "name": "gitlab_api",
        "description": (
            "Make a READ-ONLY (GET) call to the GitLab REST API v4 using the user's"
            " granted read_api/read_repository token. Use to analyze the code of the"
            " hosted apps. Provide the path after '/api/v4'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "API path after /api/v4, e.g."
                        " '/projects/123/repository/files/main.py?ref=main'."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": "Optional query parameters as key/value pairs.",
                },
            },
            "required": ["path"],
        },
    },
]

CONNECTOR_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in CONNECTOR_TOOLS)
KUBECTL_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in KUBECTL_TOOLS)
# Every native tool name (gating-independent). Used to clamp the raw model-supplied name
# before it labels the duration HISTOGRAM, so a hallucinated name can't multiply bucket
# series (the counter keeps the raw name on purpose — to observe what the model attempts).
ALL_TOOL_NAMES: frozenset[str] = KUBECTL_TOOL_NAMES | CONNECTOR_TOOL_NAMES


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return the tool definitions advertised to the model.

    Connector tools are included only when connectors are enabled, keeping the
    prompt (and its cache) stable for deployments that don't use them.
    """
    from app.core.config import get_settings

    if get_settings().connectors_enabled:
        return [*KUBECTL_TOOLS, *CONNECTOR_TOOLS]
    return list(KUBECTL_TOOLS)


def get_tool_by_name(name: str) -> dict[str, Any] | None:
    """
    Get a tool definition by its name.

    Args:
        name: The tool name (e.g., 'kubectl_get')

    Returns:
        Tool definition dict or None if not found
    """
    for tool in KUBECTL_TOOLS:
        if tool["name"] == name:
            return tool
    logger.warning(f"Tool not found: {name}")
    return None


def get_all_tool_names() -> list[str]:
    """
    Get a list of all available tool names.

    Returns:
        List of tool names
    """
    return [tool["name"] for tool in KUBECTL_TOOLS]


def validate_tool_schema(tool: dict[str, Any]) -> bool:
    """
    Validate that a tool definition has required fields.

    Args:
        tool: Tool definition dict

    Returns:
        True if valid, False otherwise
    """
    required_fields = ["name", "description", "input_schema"]
    for field in required_fields:
        if field not in tool:
            logger.error(f"Tool missing required field: {field}")
            return False

    schema = tool.get("input_schema", {})
    if schema.get("type") != "object":
        logger.error(f"Tool {tool['name']} input_schema must have type 'object'")
        return False

    if "properties" not in schema:
        logger.error(f"Tool {tool['name']} input_schema must have 'properties'")
        return False

    return True
