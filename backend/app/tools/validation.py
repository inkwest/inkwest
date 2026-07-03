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

# Resource kinds denied outright: `kubectl get secret -o yaml` returns base64 secret
# material verbatim, and a granted read-only role commonly can read Secrets. On a public
# deployment that is a direct credential-exfil path, so block the kind here regardless of
# RBAC. Matched on the first dot-segment so `secret`, `secrets`, `secrets.v1.core` are all
# covered. (Onboarding note: granted cluster roles should also exclude Secret read.)
DENIED_RESOURCE_KINDS: set[str] = {"secret", "secrets"}

# Enum allow-lists. These MUST stay in sync with the matching ``enum`` in the tool
# schema (app/tools/definitions.py); test_tools.py pins them together so any future
# drift fails CI rather than silently rejecting a schema-advertised value.
ALLOWED_OUTPUT_FORMATS: set[str] = {"wide", "yaml", "json", "name"}
ALLOWED_SORT_BY: set[str] = {"cpu", "memory"}
ALLOWED_EVENT_TYPES: set[str] = {"Normal", "Warning"}

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

# Resource kind / field path (e.g., 'pods', 'pod.spec.containers', 'networkpolicies').
# Must start with a lowercase letter so a flag-shaped value (leading '-') can never
# reach the kubectl argv as a positional argument. Security: a leading '-'
# lets a prompt-injected tool call smuggle flags (--kubeconfig=, --raw=) past the
# read-only proxy boundary.
RESOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9.-]*$")

# API group (e.g., 'apps', 'networking.k8s.io'). Empty string is the core group but the
# builder skips empty values, so we only validate non-empty ones here.
API_GROUP_PATTERN = re.compile(r"^[a-z][a-z0-9.-]*$")

# Comma-separated verb list for api-resources (e.g., 'get,list,watch').
VERBS_PATTERN = re.compile(r"^[a-z]+(,[a-z]+)*$")

# Container name follows the same DNS-label rules as a resource name.
CONTAINER_PATTERN = NAME_PATTERN

# kubectl context name (lands in argv after '--context'). EKS context aliases are
# '<account>--<region>--<cluster>' with each segment sanitized to [A-Za-z0-9_.-] (see
# eks_refresh._safe_seg), optionally '--<hash>' on collision; a legacy cluster's
# context_name shares that charset. Must start with an alphanumeric so a flag-shaped
# value ('-x') can never smuggle a kubectl flag past the read-only boundary.
CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


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

    # Validate resource type. RBAC handles access control, so we don't allow-list kinds, but the
    # value lands in the kubectl argv as a positional argument and MUST be flag-shape-safe.
    # The MCP server names this field 'resource_type' for kubectl_top, so accept both keys.
    resource = (tool_input.get("resource") or tool_input.get("resource_type") or "").lower()
    if resource:
        if not RESOURCE_PATTERN.match(resource):
            logger.warning(f"Invalid resource value: {resource}")
            return f"Invalid resource: {resource}"
        # Security: block Secret reads (base64 credential material) regardless of RBAC.
        if resource.split(".", 1)[0] in DENIED_RESOURCE_KINDS:
            logger.warning(f"Denied secret resource kind: {resource}")
            return "Reading Secrets is not permitted."
        if tool_name == "kubectl_top" and resource not in ALLOWED_TOP_RESOURCES:
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

    # Validate context (lands in argv after '--context'); reject flag-shaped values.
    context = tool_input.get("context")
    if context and not CONTEXT_PATTERN.match(context):
        logger.warning(f"Invalid context value: {context}")
        return f"Invalid context: {context}"

    # Validate container name (lands in argv after '-c')
    container = tool_input.get("container")
    if container and not CONTAINER_PATTERN.match(container):
        logger.warning(f"Invalid container name: {container}")
        return f"Invalid container name: {container}"

    # Validate api_group (lands in argv after '--api-group')
    api_group = tool_input.get("api_group")
    if api_group and not API_GROUP_PATTERN.match(api_group):
        logger.warning(f"Invalid API group: {api_group}")
        return f"Invalid API group: {api_group}"

    # Validate verbs (lands in argv after '--verbs')
    verbs = tool_input.get("verbs")
    if verbs and not VERBS_PATTERN.match(verbs):
        logger.warning(f"Invalid verbs value: {verbs}")
        return f"Invalid verbs: {verbs}"

    # Validate output format
    output = tool_input.get("output")
    if output and output not in ALLOWED_OUTPUT_FORMATS:
        logger.warning(f"Invalid output format: {output}")
        allowed = ", ".join(sorted(ALLOWED_OUTPUT_FORMATS))
        return f"Invalid output format: {output}. Use one of: {allowed}"

    # Validate sort_by for kubectl_top
    sort_by = tool_input.get("sort_by")
    if sort_by and sort_by not in ALLOWED_SORT_BY:
        logger.warning(f"Invalid sort_by value: {sort_by}")
        return f"Invalid sort_by: {sort_by}. Use 'cpu' or 'memory'"

    # Validate event types
    types = tool_input.get("types")
    if types and types not in ALLOWED_EVENT_TYPES:
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
