#!/usr/bin/env python3
"""
MCP Server for kubectl tools.

This server exposes kubectl commands as MCP tools for Claude CLI.
Run this as a subprocess or configure in Claude's MCP settings.

Usage:
    python -m app.mcp_server

Or configure in ~/.claude/mcp.json or project .mcp.json
"""

import json
import logging
import os
import subprocess
import sys
from typing import Any

from app.tools.validation import validate_tool_input

# Configure logging to stderr (stdout is for MCP protocol)
logging.basicConfig(
    level=logging.DEBUG if os.environ.get("MCP_DEBUG") else logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("inkwest-mcp")


def read_message() -> dict[str, Any] | None:
    """
    Read a JSON-RPC message from stdin.

    Returns:
        Parsed JSON message, or None if EOF
    """
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        result: dict[str, Any] = json.loads(line)
        return result
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        return None


def write_message(msg: dict[str, Any]) -> None:
    """
    Write a JSON-RPC message to stdout.

    Args:
        msg: Message to send
    """
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle_list_tools() -> list[dict[str, Any]]:
    """
    Return list of available kubectl tools.

    Returns:
        List of tool definitions in MCP format
    """
    return [
        {
            "name": "kubectl_get",
            "description": (
                "Get Kubernetes resources. Lists or retrieves resources from the cluster."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "description": "Resource type (e.g., pods, deployments, services, nodes)",
                    },
                    "name": {
                        "type": "string",
                        "description": "Optional resource name to get a specific resource",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace (default: 'default')",
                    },
                    "output": {
                        "type": "string",
                        "enum": ["yaml", "json", "wide", "name"],
                        "description": "Output format",
                    },
                    "selector": {
                        "type": "string",
                        "description": "Label selector (e.g., 'app=nginx')",
                    },
                    "all_namespaces": {
                        "type": "boolean",
                        "description": "List resources across all namespaces",
                    },
                },
                "required": ["resource"],
            },
        },
        {
            "name": "kubectl_describe",
            "description": (
                "Describe a Kubernetes resource in detail."
                " Shows events, conditions,"
                " and configuration."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "description": "Resource type (e.g., pod, deployment, service)",
                    },
                    "name": {"type": "string", "description": "Resource name"},
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace (default: 'default')",
                    },
                },
                "required": ["resource", "name"],
            },
        },
        {
            "name": "kubectl_logs",
            "description": (
                "Get container logs from a pod. Useful for debugging application issues."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pod_name": {"type": "string", "description": "Name of the pod"},
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace (default: 'default')",
                    },
                    "container": {
                        "type": "string",
                        "description": "Container name (for multi-container pods)",
                    },
                    "tail": {
                        "type": "integer",
                        "description": "Number of lines from end of logs (default: 100)",
                    },
                    "previous": {
                        "type": "boolean",
                        "description": (
                            "Get logs from previous container instance (for crash debugging)"
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": "Only show logs since duration (e.g., '5m', '1h')",
                    },
                },
                "required": ["pod_name"],
            },
        },
        {
            "name": "kubectl_events",
            "description": (
                "Get cluster events. Shows recent events like pod scheduling, errors, and warnings."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "namespace": {"type": "string", "description": "Kubernetes namespace"},
                    "all_namespaces": {
                        "type": "boolean",
                        "description": "Get events from all namespaces",
                    },
                    "field_selector": {
                        "type": "string",
                        "description": "Field selector (e.g., 'involvedObject.name=my-pod')",
                    },
                },
            },
        },
        {
            "name": "kubectl_top",
            "description": (
                "Show resource usage (CPU/memory) for pods or nodes. Requires metrics-server."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["pods", "nodes"],
                        "description": "Type of resource to show metrics for",
                    },
                    "name": {"type": "string", "description": "Optional resource name"},
                    "namespace": {
                        "type": "string",
                        "description": "Kubernetes namespace (for pods)",
                    },
                    "all_namespaces": {
                        "type": "boolean",
                        "description": "Show pods from all namespaces",
                    },
                    "containers": {
                        "type": "boolean",
                        "description": "Show container-level metrics (for pods)",
                    },
                },
                "required": ["resource_type"],
            },
        },
        {
            "name": "kubectl_api_resources",
            "description": (
                "List available API resources in the"
                " cluster. Useful for discovering"
                " resource types."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "namespaced": {
                        "type": "boolean",
                        "description": (
                            "If true, show only namespaced"
                            " resources. If false, only"
                            " non-namespaced."
                        ),
                    },
                    "api_group": {
                        "type": "string",
                        "description": "Limit to specific API group (e.g., 'apps', 'batch')",
                    },
                },
            },
        },
    ]


def execute_kubectl(tool_name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """
    Execute a kubectl command and return output.

    Args:
        tool_name: Name of the kubectl tool to execute
        args: Arguments for the tool

    Returns:
        Tuple of (output string, is_error bool)
    """
    # Security: reject flag-shaped / malformed free-string args before they reach
    # the kubectl argv, otherwise a value like '--kubeconfig=...' or '--raw=...' smuggled as a
    # resource pivots clusters or bypasses the read-only output allow-list. Shared with the API
    # path so both kubectl entry points enforce the same boundary.
    validation_error = validate_tool_input(tool_name, args)
    if validation_error:
        logger.warning(f"Validation failed for {tool_name}: {validation_error}")
        return f"Validation error: {validation_error}", True

    kubeconfig = os.environ.get("KUBECONFIG", "")
    context = os.environ.get("KUBECTL_CONTEXT", "")

    cmd = ["kubectl"]
    if kubeconfig:
        cmd.extend(["--kubeconfig", kubeconfig])
    if context:
        cmd.extend(["--context", context])

    if tool_name == "kubectl_get":
        cmd.append("get")
        cmd.append(args["resource"])
        if args.get("name"):
            cmd.append(args["name"])
        if args.get("all_namespaces"):
            cmd.append("-A")
        elif args.get("namespace"):
            cmd.extend(["-n", args["namespace"]])
        if args.get("selector"):
            cmd.extend(["-l", args["selector"]])
        if args.get("output"):
            cmd.extend(["-o", args["output"]])

    elif tool_name == "kubectl_describe":
        cmd.extend(["describe", args["resource"], args["name"]])
        if args.get("namespace"):
            cmd.extend(["-n", args["namespace"]])

    elif tool_name == "kubectl_logs":
        cmd.extend(["logs", args["pod_name"]])
        if args.get("namespace"):
            cmd.extend(["-n", args["namespace"]])
        if args.get("container"):
            cmd.extend(["-c", args["container"]])
        tail = args.get("tail", 100)
        cmd.extend(["--tail", str(tail)])
        if args.get("previous"):
            cmd.append("--previous")
        if args.get("since"):
            cmd.extend(["--since", args["since"]])

    elif tool_name == "kubectl_events":
        cmd.extend(["get", "events", "--sort-by=.lastTimestamp"])
        if args.get("all_namespaces"):
            cmd.append("-A")
        elif args.get("namespace"):
            cmd.extend(["-n", args["namespace"]])
        if args.get("field_selector"):
            cmd.extend(["--field-selector", args["field_selector"]])

    elif tool_name == "kubectl_top":
        cmd.extend(["top", args["resource_type"]])
        if args.get("name"):
            cmd.append(args["name"])
        if args["resource_type"] == "pods":
            if args.get("all_namespaces"):
                cmd.append("-A")
            elif args.get("namespace"):
                cmd.extend(["-n", args["namespace"]])
            if args.get("containers"):
                cmd.append("--containers")

    elif tool_name == "kubectl_api_resources":
        cmd.append("api-resources")
        if "namespaced" in args:
            cmd.extend(["--namespaced=" + str(args["namespaced"]).lower()])
        if args.get("api_group"):
            cmd.extend(["--api-group", args["api_group"]])

    else:
        return f"Unknown tool: {tool_name}", True

    logger.debug(f"Executing: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            output = result.stdout
            if not output.strip():
                output = "No resources found."
            return output, False
        else:
            error_msg = result.stderr or f"Command failed with code {result.returncode}"
            return f"Error: {error_msg}", True

    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 30 seconds", True
    except FileNotFoundError:
        return "Error: kubectl not found. Please ensure kubectl is installed and in PATH.", True
    except Exception as e:
        return f"Error: {str(e)}", True


def handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    """
    Handle a tools/call request.

    Args:
        params: Request parameters including tool name and arguments

    Returns:
        Response with tool execution result
    """
    tool_name = params.get("name", "")
    tool_args = params.get("arguments", {})

    logger.info(f"Executing tool: {tool_name}")
    logger.debug(f"Tool arguments: {tool_args}")

    output, is_error = execute_kubectl(tool_name, tool_args)

    return {"content": [{"type": "text", "text": output}], "isError": is_error}


def handle_initialize() -> dict[str, Any]:
    """
    Handle the initialize request.

    Returns:
        Server capabilities and info
    """
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "inkwest-kubectl", "version": "0.1.0"},
    }


def main() -> None:
    """Main MCP server loop."""
    logger.info("Starting Inkwest MCP server")

    while True:
        msg = read_message()
        if msg is None:
            logger.info("EOF received, shutting down")
            break

        method = msg.get("method", "")
        msg_id = msg.get("id")

        logger.debug(f"Received: {method}")

        try:
            if method == "initialize":
                result = handle_initialize()
                write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})

            elif method == "notifications/initialized":
                # Client acknowledged initialization, no response needed
                logger.info("Client initialized")

            elif method == "tools/list":
                tools = handle_list_tools()
                write_message({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})

            elif method == "tools/call":
                params = msg.get("params", {})
                result = handle_tools_call(params)
                write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})

            elif method == "ping":
                write_message({"jsonrpc": "2.0", "id": msg_id, "result": {}})

            else:
                # Unknown method
                logger.warning(f"Unknown method: {method}")
                if msg_id is not None:
                    write_message(
                        {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {"code": -32601, "message": f"Method not found: {method}"},
                        }
                    )

        except Exception as e:
            logger.exception(f"Error handling {method}")
            if msg_id is not None:
                write_message(
                    {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(e)}}
                )


if __name__ == "__main__":
    main()
