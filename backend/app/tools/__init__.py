# Tools module
"""
Kubectl tools for Inkwest.

This module provides kubectl tool definitions, input validation,
command building, and execution capabilities.
"""

from .definitions import (
    CONNECTOR_TOOLS,
    KUBECTL_TOOLS,
    get_all_tool_names,
    get_tool_by_name,
    get_tool_definitions,
    validate_tool_schema,
)
from .dispatch import execute_tool
from .dispatch import execute_tools_parallel as execute_tools_parallel_ctx
from .exec_context import ToolExecContext
from .executor import (
    DEFAULT_TIMEOUT,
    MAX_OUTPUT_CHARS,
    execute_kubectl,
    execute_tool_with_retry,
    execute_tools_parallel,
    truncate_output,
)
from .kubectl import build_kubectl_command
from .validation import (
    ALLOWED_TOP_RESOURCES,
    validate_label_selector,
    validate_tool_input,
)

__all__ = [
    # Definitions
    "KUBECTL_TOOLS",
    "CONNECTOR_TOOLS",
    "get_tool_by_name",
    "get_all_tool_names",
    "get_tool_definitions",
    "validate_tool_schema",
    # Dispatch (connector-aware)
    "execute_tool",
    "execute_tools_parallel_ctx",
    "ToolExecContext",
    # Validation
    "ALLOWED_TOP_RESOURCES",
    "validate_tool_input",
    "validate_label_selector",
    # Command builder
    "build_kubectl_command",
    # Executor
    "execute_kubectl",
    "execute_tools_parallel",
    "execute_tool_with_retry",
    "truncate_output",
    "MAX_OUTPUT_CHARS",
    "DEFAULT_TIMEOUT",
]
