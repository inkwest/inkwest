"""
Input validation for kubectl tools.

This module provides validation functions to ensure all tool inputs
are safe and conform to Kubernetes naming conventions.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Resources allowed for kubectl_top
ALLOWED_TOP_RESOURCES: set[str] = {"pods", "nodes", "pod", "node"}

# Validation patterns based on Kubernetes naming conventions
# Namespace: lowercase alphanumeric with hyphens, must start/end with alphanumeric
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

# Resource name: lowercase alphanumeric with hyphens and dots
NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")

# Label key: alphanumeric with hyphens, underscores, dots
LABEL_KEY_PATTERN = re.compile(r"^([a-zA-Z0-9][-a-zA-Z0-9_.]*)?[a-zA-Z0-9]$")

# Label value: similar to key but can be empty
LABEL_VALUE_PATTERN = re.compile(r"^([a-zA-Z0-9][-a-zA-Z0-9_.]*)?[a-zA-Z0-9]$|^$")

# Duration pattern for --since flag (e.g., '1h', '30m', '5s')
DURATION_PATTERN = re.compile(r"^[0-9]+[smh]$")

# Field selector pattern (e.g., 'involvedObject.name=my-pod')
FIELD_SELECTOR_PATTERN = re.compile(r"^[\w.]+[=!]=?[\w.-]*$")

# API version pattern (e.g., 'v1', 'apps/v1', 'networking.k8s.io/v1')
API_VERSION_PATTERN = re.compile(r"^([a-z0-9.-]+/)?v[0-9]+(alpha[0-9]+|beta[0-9]+)?$")


def validate_tool_input(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """
    Validate tool input. Returns error message or None if valid.

    Args:
        tool_name: Name of the tool being called
        tool_input: Input parameters for the tool

    Returns:
        Error message string if validation fails, None if valid
    """
    logger.debug(f"Validating input for {tool_name}: {tool_input}")

    # Validate resource type — RBAC handles access control, we only check kubectl_top constraints
    resource = tool_input.get("resource", "").lower()
    if resource:
        if tool_name == "kubectl_top":
            if resource not in ALLOWED_TOP_RESOURCES:
                logger.warning(f"Invalid resource for top command: {resource}")
                return f"kubectl_top only supports 'pods' or 'nodes', got: {resource}"

    # Validate namespace format
    namespace = tool_input.get("namespace")
    if namespace and not NAMESPACE_PATTERN.match(namespace):
        logger.warning(f"Invalid namespace format: {namespace}")
        return f"Invalid namespace: {namespace}"

    # Validate name format (handles both 'name' and 'pod_name')
    name = tool_input.get("name") or tool_input.get("pod_name")
    if name and not NAME_PATTERN.match(name):
        logger.warning(f"Invalid resource name format: {name}")
        return f"Invalid resource name: {name}"

    # Validate label selector
    selector = tool_input.get("selector")
    if selector:
        error = validate_label_selector(selector)
        if error:
            return error

    # Validate tail (log lines)
    tail = tool_input.get("tail")
    if tail is not None:
        if not isinstance(tail, int) or tail < 1 or tail > 10000:
            logger.warning(f"Invalid tail value: {tail}")
            return "tail must be an integer between 1 and 10000"

    # Validate since duration
    since = tool_input.get("since")
    if since and not DURATION_PATTERN.match(since):
        logger.warning(f"Invalid since duration: {since}")
        return f"Invalid duration format: {since}. Use format like '1h', '30m', '5s'"

    # Validate field selector
    field_selector = tool_input.get("field_selector")
    if field_selector:
        # Split by comma and validate each part
        for part in field_selector.split(","):
            part = part.strip()
            if part and not FIELD_SELECTOR_PATTERN.match(part):
                logger.warning(f"Invalid field selector: {field_selector}")
                return f"Invalid field selector format: {part}"

    # Validate api_version
    api_version = tool_input.get("api_version")
    if api_version and not API_VERSION_PATTERN.match(api_version):
        logger.warning(f"Invalid API version: {api_version}")
        return f"Invalid API version format: {api_version}"

    # Validate output format
    output = tool_input.get("output")
    if output and output not in ["wide", "yaml", "json"]:
        logger.warning(f"Invalid output format: {output}")
        return f"Invalid output format: {output}. Use 'wide', 'yaml', or 'json'"

    # Validate sort_by for kubectl_top
    sort_by = tool_input.get("sort_by")
    if sort_by and sort_by not in ["cpu", "memory"]:
        logger.warning(f"Invalid sort_by value: {sort_by}")
        return f"Invalid sort_by: {sort_by}. Use 'cpu' or 'memory'"

    # Validate event types
    types = tool_input.get("types")
    if types and types not in ["Normal", "Warning"]:
        logger.warning(f"Invalid event type: {types}")
        return f"Invalid event type: {types}. Use 'Normal' or 'Warning'"

    logger.debug(f"Validation passed for {tool_name}")
    return None


def validate_label_selector(selector: str) -> str | None:
    """
    Validate label selector format.

    Args:
        selector: Label selector string (e.g., 'app=nginx,env=prod')

    Returns:
        Error message string if validation fails, None if valid
    """
    if not selector:
        return None

    for part in selector.split(","):
        part = part.strip()
        if not part:
            continue

        # Handle different selector operators
        if "!=" in part:
            key, value = part.split("!=", 1)
        elif "=" in part:
            key, value = part.split("=", 1)
        else:
            # Could be just a key (existence check) or invalid
            logger.warning(f"Invalid selector format: {part}")
            return f"Invalid selector format: {part}"

        key = key.strip()
        value = value.strip()

        # Validate key
        if not key:
            return "Label key cannot be empty"
        if not LABEL_KEY_PATTERN.match(key):
            logger.warning(f"Invalid label key: {key}")
            return f"Invalid label key: {key}"

        # Validate value (can be empty for existence checks)
        if value and not LABEL_VALUE_PATTERN.match(value):
            logger.warning(f"Invalid label value: {value}")
            return f"Invalid label value: {value}"

    return None


def sanitize_input(value: str, max_length: int = 253) -> str:
    """
    Sanitize a string input by removing potentially dangerous characters.

    Args:
        value: Input string to sanitize
        max_length: Maximum allowed length (default 253 for K8s names)

    Returns:
        Sanitized string
    """
    if not value:
        return ""

    # Truncate to max length
    value = value[:max_length]

    # Remove any shell metacharacters (extra safety)
    dangerous_chars = [
        ";",
        "|",
        "&",
        "$",
        "`",
        "(",
        ")",
        "{",
        "}",
        "[",
        "]",
        "<",
        ">",
        "!",
        "*",
        "?",
        "~",
        '"',
        "'",
        "\\",
        "\n",
        "\r",
    ]
    for char in dangerous_chars:
        value = value.replace(char, "")

    return value
