"""
Claude CLI Client for Inkwest.

This module provides functions to execute the Claude CLI and parse its output.
It allows using the Claude CLI as an alternative to the Anthropic API,
enabling local development without API costs.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.services.events import ChatEvent

logger = logging.getLogger(__name__)


@dataclass
class CLIResponse:
    """Parsed response from Claude CLI."""

    text: str
    tool_calls: list[dict]
    tool_results: list[dict]
    is_complete: bool


async def execute_claude_cli(
    session_id: str,
    prompt: str,
    cli_path: str = "claude",
    timeout: int = 120,
    resume_session_id: str | None = None,
) -> AsyncIterator[str]:
    """
    Execute Claude CLI and stream output.

    Uses --resume to maintain conversation state.
    Uses --output-format stream-json for structured output.

    Args:
        session_id: Inkwest session ID (for logging)
        prompt: The prompt to send to Claude
        cli_path: Path to the claude binary
        timeout: Timeout in seconds for the CLI command
        resume_session_id: Claude CLI session ID for --resume (from previous init event)

    Yields:
        Lines of JSON output from the CLI

    Raises:
        TimeoutError: If the CLI times out
        RuntimeError: If the CLI returns a non-zero exit code
    """
    # Build command - use -p for print mode with streaming JSON output
    # Note: --verbose is required for stream-json with -p
    # --dangerously-skip-permissions bypasses workspace trust prompt for non-interactive usage
    cmd = [
        cli_path,
        "--dangerously-skip-permissions",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
    ]

    # Add --resume if we have a CLI session ID from a previous interaction
    if resume_session_id:
        cmd.insert(2, "--resume")
        cmd.insert(3, resume_session_id)
        logger.info(f"Resuming Claude CLI session {resume_session_id}")

    logger.info(f"Executing Claude CLI for Inkwest session {session_id}")
    logger.info(f"CLI command: {' '.join(cmd)}")
    logger.debug(f"CLI command: {' '.join(cmd)}")

    # Use a much larger buffer limit to handle large tool outputs
    # Default is 64KB which can be exceeded by file reads, kubectl outputs, etc.
    buffer_limit = 100 * 1024 * 1024  # 100MB

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=buffer_limit,
    )

    try:
        # Create a task to handle the timeout
        async def read_stdout() -> AsyncIterator[str]:
            """Read stdout line by line."""
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                decoded = line.decode().strip()
                if decoded:
                    yield decoded

        # Stream stdout line by line with timeout
        try:
            async for line in read_stdout():
                yield line
        except asyncio.CancelledError:
            logger.warning("CLI stream cancelled")
            raise

        # Wait for process to complete
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"Claude CLI timed out after {timeout}s")

        if process.returncode != 0:
            assert process.stderr is not None
            stderr = await process.stderr.read()
            stderr_text = stderr.decode().strip() if stderr else ""

            assert process.stdout is not None
            remaining_stdout = await process.stdout.read()
            stdout_text = remaining_stdout.decode().strip() if remaining_stdout else ""

            error_msg = stderr_text or stdout_text or f"Exit code {process.returncode}"
            logger.error(f"Claude CLI failed with code {process.returncode}")
            logger.error(f"CLI stderr: {stderr_text}")
            logger.error(f"CLI stdout (remaining): {stdout_text}")
            raise RuntimeError(f"Claude CLI failed: {error_msg}")

    except TimeoutError:
        raise
    except Exception:
        # Ensure process is killed on any error
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise


def parse_cli_stream_line(line: str) -> dict | None:
    """
    Parse a line of stream-json output from Claude CLI.

    Expected format varies by event type:
    - {"type": "assistant", "message": {"content": [...]}}
    - {"type": "content_block_start", "content_block": {...}}
    - {"type": "content_block_delta", "delta": {...}}
    - {"type": "message_stop"}

    Args:
        line: A line of JSON output from the CLI

    Returns:
        Parsed JSON object, or None if parsing fails
    """
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        # Non-JSON output (status messages, progress, etc.)
        logger.debug(f"Non-JSON CLI output: {line[:100]}")
        return None


async def parse_cli_response(output_stream: AsyncIterator[str]) -> AsyncIterator[ChatEvent]:
    """
    Convert CLI stream output to ChatEvents.

    Parses the stream-json output from Claude CLI and yields
    corresponding ChatEvent objects for the frontend.

    Args:
        output_stream: Async iterator of JSON lines from CLI

    Yields:
        ChatEvent objects for each significant event
    """
    current_tool_id = None
    current_tool_name = None
    has_sent_text = False
    total_input_tokens = 0
    total_output_tokens = 0

    async for line in output_stream:
        parsed = parse_cli_stream_line(line)
        if not parsed:
            continue

        event_type = parsed.get("type")

        # Handle system init event - contains CLI session ID for --resume
        if event_type == "system" and parsed.get("subtype") == "init":
            cli_session_id = parsed.get("session_id")
            if cli_session_id:
                logger.info(f"Claude CLI session ID: {cli_session_id}")
                yield ChatEvent(type="session_info", metadata={"cli_session_id": cli_session_id})

        elif event_type == "assistant":
            message = parsed.get("message", {})
            # Accumulate usage from assistant events
            usage = message.get("usage") or parsed.get("usage")
            if usage:
                total_input_tokens += usage.get("input_tokens", 0)
                total_output_tokens += usage.get("output_tokens", 0)

            content = message.get("content", [])
            for block in content:
                block_type = block.get("type")
                if block_type == "text":
                    text = block.get("text", "")
                    if text:
                        if has_sent_text:
                            text = "\n\n" + text
                        yield ChatEvent(type="text_delta", content=text)
                        has_sent_text = True
                elif block_type == "tool_use":
                    yield ChatEvent(
                        type="tool_start",
                        tool_name=block.get("name"),
                        tool_input=block.get("input", {}),
                        tool_use_id=block.get("id"),
                    )

        elif event_type == "content_block_start":
            block = parsed.get("content_block", {})
            block_type = block.get("type")
            if block_type == "tool_use":
                current_tool_id = block.get("id")
                current_tool_name = block.get("name")
                yield ChatEvent(
                    type="tool_start",
                    tool_name=current_tool_name,
                    tool_input={},
                    tool_use_id=current_tool_id,
                )

        elif event_type == "content_block_delta":
            delta = parsed.get("delta", {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                yield ChatEvent(type="text_delta", content=delta.get("text", ""))

        elif event_type == "tool_result":
            yield ChatEvent(
                type="tool_result",
                tool_name=parsed.get("name", current_tool_name or ""),
                content=parsed.get("content", ""),
                tool_use_id=parsed.get("tool_use_id", current_tool_id),
                is_error=parsed.get("is_error", False),
            )

        elif event_type == "result":
            result_text = parsed.get("result", "")
            if result_text:
                yield ChatEvent(type="text_delta", content=result_text)
            # Result event carries final usage from CLI
            usage = parsed.get("usage", {})
            if usage.get("input_tokens"):
                total_input_tokens = usage["input_tokens"]
            if usage.get("output_tokens"):
                total_output_tokens = usage["output_tokens"]

            subtype = parsed.get("subtype")
            if subtype == "success":
                yield ChatEvent(
                    type="done",
                    metadata={
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                )

        elif event_type in ("message_stop", "done", "end"):
            yield ChatEvent(
                type="done",
                metadata={
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
            )

        elif event_type == "error":
            error_msg = parsed.get("message", parsed.get("error", "Unknown error"))
            yield ChatEvent(type="error", content=str(error_msg), is_error=True)

        # Handle legacy/simple format for compatibility
        elif event_type == "text":
            yield ChatEvent(type="text_delta", content=parsed.get("content", ""))

        elif event_type == "tool_use":
            yield ChatEvent(
                type="tool_start",
                tool_name=parsed.get("name"),
                tool_input=parsed.get("input", {}),
                tool_use_id=parsed.get("id"),
            )
