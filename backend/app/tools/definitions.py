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
                    "enum": ["wide", "yaml", "json"],
                    "description": (
                        "Output format. Use 'wide' for more columns, 'yaml' for full spec."
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
