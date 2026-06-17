"""
Claude CLI Client for Inkwest.

This module provides functions to execute the Claude CLI and parse its output.
It allows using the Claude CLI as an alternative to the Anthropic API,
enabling local development without API costs.
"""

import asyncio
import json
import logging
import os
import shlex
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.services.events import ChatEvent

logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when stopping the agent on an explicit
# cancel: let it shut down cleanly, but don't wait forever if it ignores the signal.
_STOP_GRACE_SECONDS = 5.0


def _terminate_quiet(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass


def _kill_quiet(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _container_signal(docker_path: str, container: str, pid_file: str, sig: str) -> bool:
    """Send `sig` to the in-container agent via its recorded PID (best-effort).

    Uses `sh`'s builtin `kill` so it works without procps in the toolbox image. Never
    raises — stopping is best-effort; a missing pid file / already-dead process is fine.

    Returns True if a PID was present to signal, False if the pid file was missing/empty
    (the kill itself is still best-effort and not reflected in the result), so the caller
    can flag a stop that had nothing to target.
    """
    pf = shlex.quote(pid_file)
    # exit 3 distinguishes "no PID recorded" from a kill that simply found the process
    # already gone — only the former means the stop signal hit nothing.
    script = (
        f'p=$(cat {pf} 2>/dev/null); [ -n "$p" ] || exit 3; kill -{sig} "$p" 2>/dev/null; exit 0'
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_path,
            "exec",
            container,
            "sh",
            "-c",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(proc.wait(), timeout=10) != 3
    except Exception:
        logger.warning("Failed to signal in-container agent (%s)", sig, exc_info=True)
        return False


async def _container_alive(docker_path: str, container: str, pid_file: str) -> bool:
    """True if the in-container agent PID is still alive (`kill -0`)."""
    pf = shlex.quote(pid_file)
    script = f'p=$(cat {pf} 2>/dev/null); [ -n "$p" ] && kill -0 "$p" 2>/dev/null'
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_path,
            "exec",
            container,
            "sh",
            "-c",
            script,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await asyncio.wait_for(proc.wait(), timeout=10) == 0
    except Exception:
        return False


async def _stop_agent(
    process: asyncio.subprocess.Process,
    *,
    container: str | None,
    docker_path: str,
    pid_file: str | None,
) -> None:
    """Stop the running agent: SIGTERM, then SIGKILL after a grace period.

    In sandbox mode the agent runs INSIDE the container; `docker exec` does not forward
    signals to it, so it's signaled directly by its recorded PID (the local client is
    just torn down alongside). In local mode `process` IS the agent, so it's signaled
    directly.
    """
    if container and pid_file:
        signaled = await _container_signal(docker_path, container, pid_file, "TERM")
        if not signaled:
            # No PID recorded inside the container (e.g. the launch wrapper couldn't
            # write the pid file). The agent may still be running and we can't target
            # it — surface it instead of silently regressing to "stop does nothing".
            logger.warning(
                "In-container stop found no agent PID to signal (container=%s); "
                "the agent may still be running",
                container,
            )
        _terminate_quiet(process)  # drop the local docker-exec client too
        waited = 0.0
        while waited < _STOP_GRACE_SECONDS:
            if not await _container_alive(docker_path, container, pid_file):
                break
            await asyncio.sleep(0.5)
            waited += 0.5
        else:
            await _container_signal(docker_path, container, pid_file, "KILL")
    else:
        _terminate_quiet(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=_STOP_GRACE_SECONDS)
        except TimeoutError:
            _kill_quiet(process)
    # Reap the local client so no handle/zombie lingers.
    _kill_quiet(process)
    try:
        await asyncio.wait_for(process.wait(), timeout=_STOP_GRACE_SECONDS)
    except TimeoutError:
        pass


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
    extra_env: dict[str, str] | None = None,
    cwd: str | None = None,
    system_prompt: str | None = None,
    container: str | None = None,
    docker_path: str = "docker",
    model: str | None = None,
    cancel_event: asyncio.Event | None = None,
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
        extra_env: Per-session connector credentials to expose to the agent's shell
            (e.g. AWS_*, GITLAB_TOKEN). Merged over the parent env for THIS subprocess
            only — never set process-globally, so conversations stay isolated.
        cwd: Working directory for the CLI process. Use a per-session scratch dir so
            the agent doesn't see the backend source (`/app`) and so Claude Code keeps
            its per-project transcripts isolated between conversations.
        system_prompt: The Inkwest/custom system prompt for the session, appended to
            Claude Code's built-in prompt via --append-system-prompt (so the managed
            persona is honored without dropping Claude Code's tool-use framing).
        container: When set, run `claude` INSIDE this per-conversation toolbox
            container via `docker exec` (the sandboxed path) instead of as a local
            subprocess. The streaming/timeout/parse behavior is identical either way.
        docker_path: Path to the `docker` CLI used for the `docker exec` wrapper.
        model: Explicit model for this turn (`claude --model`). None = let the
            CLI use its own configured default.
        cancel_event: Set by an explicit user stop. When it fires, the agent is
            stopped promptly (SIGTERM then SIGKILL after a grace period) instead of
            waiting for its next output line.

    Yields:
        Lines of JSON output from the CLI

    Raises:
        TimeoutError: If the CLI times out
        RuntimeError: If the CLI returns a non-zero exit code
    """
    # Build the inner `claude` argv — identical whether run locally or in a container.
    # Note: --verbose is required for stream-json with -p; --dangerously-skip-permissions
    # bypasses the workspace trust prompt for non-interactive usage.
    inner = [
        cli_path,
        "--dangerously-skip-permissions",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
    ]

    # Layer the session's managed system prompt over Claude Code's default.
    if system_prompt:
        inner += ["--append-system-prompt", system_prompt]

    # Explicit per-session/admin model choice; omitted = CLI's own default.
    if model:
        inner += ["--model", model]

    # Add --resume if we have a CLI session ID from a previous interaction
    if resume_session_id:
        inner.insert(2, "--resume")
        inner.insert(3, resume_session_id)
        logger.info(f"Resuming Claude CLI session {resume_session_id}")

    # Inject per-session connector creds into THIS subprocess's env only — never set
    # process-globally, so conversations stay isolated.
    proc_env = {**os.environ, **extra_env} if extra_env else None

    # PID file (container mode only) lets an explicit stop signal the agent directly:
    # `docker exec` does NOT forward signals to the in-container process, so killing the
    # local client alone would leave the agent running and burning tokens.
    pid_file: str | None = None
    if container:
        # Run `claude` inside the per-conversation toolbox container. Each cred is
        # passed as a value-less `-e KEY`: docker reads the value from this client's
        # own env (proc_env), so secrets never appear in argv — same guarantee as the
        # local path. `-w` sets the dir inside the container (don't chdir the client).
        env_flags = [flag for key in (extra_env or {}) for flag in ("-e", key)]
        workdir_flags = ["-w", cwd] if cwd else []
        # Record the agent's own PID before handing off: `exec "$@"` replaces the shell
        # with `claude`, so the `$$` written here is claude's PID inside the container —
        # the target an explicit stop signals (see _stop_agent). The file is overwritten
        # at each turn start; one turn per session (turn_runner) means no concurrent
        # writers, and Stop is only reachable once streaming has begun — by which point
        # this write has long completed — so a stop can't race ahead of the PID record.
        pid_file = f"/tmp/inkwest-agent-{session_id}.pid"
        launch = ["sh", "-c", f'echo $$ > {shlex.quote(pid_file)}; exec "$@"', "sh", *inner]
        # No `-i`: `claude -p` reads the prompt from argv, not stdin; attaching stdin is
        # unnecessary surface. stdout is still streamed back to us as the inner process runs.
        cmd = [docker_path, "exec", *env_flags, *workdir_flags, container, *launch]
        run_cwd: str | None = None
    else:
        cmd = inner
        run_cwd = cwd

    logger.info(f"Executing Claude CLI for Inkwest session {session_id}")
    logger.debug(f"CLI command: {' '.join(cmd)}")

    # Use a much larger buffer limit to handle large tool outputs
    # Default is 64KB which can be exceeded by file reads, kubectl outputs, etc.
    buffer_limit = 100 * 1024 * 1024  # 100MB

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=buffer_limit,
        env=proc_env,
        cwd=run_cwd,
    )

    try:
        # Read stdout line by line, but race each read against an explicit stop so a
        # cancel is honored within milliseconds instead of blocking until the agent's
        # next line (it may be mid-tool or mid-thought, emitting nothing for a while).
        async def read_stdout() -> AsyncIterator[str]:
            """Read stdout line by line; stop the agent promptly on cancel."""
            assert process.stdout is not None
            cancel_task = asyncio.ensure_future(cancel_event.wait()) if cancel_event else None
            try:
                while True:
                    read_task = asyncio.ensure_future(process.stdout.readline())
                    waiters = {read_task} | ({cancel_task} if cancel_task else set())
                    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                    if cancel_task is not None and cancel_task in done:
                        read_task.cancel()
                        await asyncio.gather(read_task, return_exceptions=True)
                        logger.info("CLI stream cancelled — stopping agent")
                        await _stop_agent(
                            process,
                            container=container,
                            docker_path=docker_path,
                            pid_file=pid_file,
                        )
                        return
                    line = read_task.result()
                    if not line:
                        break
                    decoded = line.decode().strip()
                    if decoded:
                        yield decoded
            finally:
                if cancel_task is not None and not cancel_task.done():
                    cancel_task.cancel()

        try:
            async for line in read_stdout():
                yield line
        except asyncio.CancelledError:
            logger.warning("CLI stream cancelled")
            raise

        # _stop_agent already terminated and reaped the process above; don't fall through
        # to the returncode check, which would mis-report the kill as a CLI failure.
        if cancel_event and cancel_event.is_set():
            return

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

        # Handle system init event - contains CLI session ID for --resume and
        # the model the CLI actually runs with (for accurate cost attribution).
        if event_type == "system" and parsed.get("subtype") == "init":
            cli_session_id = parsed.get("session_id")
            if cli_session_id:
                logger.info(f"Claude CLI session ID: {cli_session_id}")
                yield ChatEvent(
                    type="session_info",
                    metadata={
                        "cli_session_id": cli_session_id,
                        "model": parsed.get("model"),
                    },
                )

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
            # The `result` field duplicates the final assistant text already streamed
            # via `assistant` events. Only emit it as a fallback when nothing was sent
            # yet — otherwise the whole answer is rendered twice.
            result_text = parsed.get("result", "")
            if result_text and not has_sent_text:
                yield ChatEvent(type="text_delta", content=result_text)
                has_sent_text = True
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
