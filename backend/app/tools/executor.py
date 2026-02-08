"""
Kubectl command executor.

This module handles the safe, async execution of kubectl commands
with proper timeout handling, output truncation, and error management.
"""

import asyncio
import logging
import subprocess
from typing import Any

from .kubectl import build_kubectl_command
from .validation import validate_tool_input

logger = logging.getLogger(__name__)

# Maximum output size in characters
MAX_OUTPUT_CHARS = 50000

# Default command timeout in seconds
DEFAULT_TIMEOUT = 30


async def execute_kubectl(
    tool_name: str,
    tool_input: dict[str, Any],
    kubeconfig: str,
    context: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[str, bool]:
    """
    Execute kubectl command.

    Args:
        tool_name: Name of the kubectl tool (e.g., 'kubectl_get')
        tool_input: Tool input parameters
        kubeconfig: Path to kubeconfig file
        context: Optional kubectl context name
        timeout: Command timeout in seconds

    Returns:
        Tuple of (output, is_error) where is_error indicates if execution failed
    """
    logger.info(f"Executing tool: {tool_name}", extra={"input": tool_input})

    # Validate input first
    validation_error = validate_tool_input(tool_name, tool_input)
    if validation_error:
        logger.warning(f"Validation failed for {tool_name}: {validation_error}")
        return (f"Validation error: {validation_error}", True)

    # Build command
    try:
        cmd = build_kubectl_command(tool_name, tool_input, kubeconfig, context)
    except ValueError as e:
        logger.error(f"Failed to build command: {e}")
        return (f"Command build error: {str(e)}", True)

    logger.debug(f"Command: {' '.join(cmd)}")

    try:
        # Run in thread pool to not block the event loop
        result = await asyncio.wait_for(
            asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=timeout),
            timeout=timeout + 5,  # Extra buffer for thread overhead
        )

        if result.returncode == 0:
            output = result.stdout
            truncated_output = truncate_output(output, tool_name)
            logger.info(
                f"Tool completed: {tool_name}",
                extra={"is_error": False, "output_length": len(output)},
            )
            return (truncated_output, False)
        else:
            # Return stderr as content (not exception) - Claude can interpret
            error_output = result.stderr or f"Command failed with exit code {result.returncode}"
            logger.warning(
                f"Tool failed: {tool_name}", extra={"is_error": True, "stderr": error_output}
            )
            return (f"Command failed:\n{error_output}", True)

    except subprocess.TimeoutExpired:
        logger.error(f"Tool timed out: {tool_name} after {timeout}s")
        return (f"Command timed out after {timeout} seconds", True)

    except TimeoutError:
        logger.error(f"Async timeout for tool: {tool_name}")
        return (f"Command timed out after {timeout} seconds", True)

    except FileNotFoundError:
        logger.error("kubectl not found in PATH")
        return ("kubectl not found in PATH. Ensure kubectl is installed and in PATH.", True)

    except PermissionError:
        logger.error("Permission denied executing kubectl")
        return ("Permission denied executing kubectl", True)

    except Exception as e:
        logger.exception(f"Unexpected error executing {tool_name}")
        return (f"Execution error: {type(e).__name__}: {str(e)}", True)


def truncate_output(output: str, tool_name: str) -> str:
    """
    Intelligently truncate large outputs.

    Different strategies for different command types:
    - Logs: Keep the tail (most recent entries)
    - Other: Keep the head with summary

    Args:
        output: Command output to truncate
        tool_name: Name of the tool for context-aware truncation

    Returns:
        Truncated output string
    """
    if len(output) <= MAX_OUTPUT_CHARS:
        return output

    original_length = len(output)

    # For logs, keep the tail (most recent lines)
    if tool_name == "kubectl_logs":
        truncated = output[-MAX_OUTPUT_CHARS:]
        # Find first complete line
        first_newline = truncated.find("\n")
        if first_newline > 0 and first_newline < 1000:
            truncated = truncated[first_newline + 1 :]

        return (
            f"[Earlier logs truncated - showing last"
            f" {len(truncated)} of {original_length}"
            f" characters]\n...\n" + truncated
        )

    truncated = output[:MAX_OUTPUT_CHARS]
    # Find last complete line
    last_newline = truncated.rfind("\n")
    if last_newline > MAX_OUTPUT_CHARS * 0.8:
        truncated = truncated[:last_newline]

    return (
        truncated
        + f"\n\n[Output truncated - showing {len(truncated)} of {original_length} characters]"
    )


async def execute_tools_parallel(
    tool_calls: list[dict[str, Any]],
    kubeconfig: str,
    context: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict[str, Any]]:
    """
    Execute multiple tools in parallel.

    Args:
        tool_calls: List of tool call dicts with 'id', 'name', and 'input' keys
        kubeconfig: Path to kubeconfig file
        context: Optional kubectl context name
        timeout: Command timeout in seconds

    Returns:
        List of result dicts with 'tool_use_id', 'content', and 'is_error' keys
    """
    logger.info(f"Executing {len(tool_calls)} tools in parallel")

    async def execute_one(tool_call: dict[str, Any]) -> dict[str, Any]:
        """Execute a single tool and format the result."""
        tool_id = tool_call.get("id", "unknown")
        tool_name = tool_call.get("name", "")
        tool_input = tool_call.get("input", {})

        logger.debug(f"Parallel execution: {tool_name} (id={tool_id})")

        output, is_error = await execute_kubectl(
            tool_name, tool_input, kubeconfig, context, timeout
        )

        return {"tool_use_id": tool_id, "content": output, "is_error": is_error}

    # Execute all tools concurrently
    results = await asyncio.gather(*[execute_one(tc) for tc in tool_calls], return_exceptions=True)

    # Handle any exceptions that occurred
    processed = []
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            tool_id = tool_calls[i].get("id", "unknown")
            logger.exception(f"Exception in parallel execution for tool {tool_id}")
            processed.append(
                {
                    "tool_use_id": tool_id,
                    "content": f"Error: {type(result).__name__}: {str(result)}",
                    "is_error": True,
                }
            )
        else:
            processed.append(result)

    logger.info(
        f"Parallel execution complete: {len(processed)} results, "
        f"{sum(1 for r in processed if r.get('is_error'))} errors"
    )

    return processed


async def execute_tool_with_retry(
    tool_name: str,
    tool_input: dict[str, Any],
    kubeconfig: str,
    context: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = 2,
    retry_delay: float = 1.0,
) -> tuple[str, bool]:
    """
    Execute kubectl command with retry logic.

    Useful for transient failures like connection issues.

    Args:
        tool_name: Name of the kubectl tool
        tool_input: Tool input parameters
        kubeconfig: Path to kubeconfig file
        context: Optional kubectl context name
        timeout: Command timeout in seconds
        max_retries: Maximum number of retry attempts
        retry_delay: Delay between retries in seconds

    Returns:
        Tuple of (output, is_error)
    """
    last_error = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(f"Retry attempt {attempt} for {tool_name}")
            await asyncio.sleep(retry_delay * attempt)  # Exponential backoff

        output, is_error = await execute_kubectl(
            tool_name, tool_input, kubeconfig, context, timeout
        )

        if not is_error:
            return (output, False)

        last_error = output

        # Don't retry validation errors or unknown tools
        if "Validation error" in output or "Unknown tool" in output:
            return (output, True)

        # Don't retry if it's a clear kubectl error (not transient)
        if "not found" in output.lower() or "forbidden" in output.lower():
            return (output, True)

    logger.warning(f"All retry attempts failed for {tool_name}")
    return (last_error or "Unknown error after retries", True)
