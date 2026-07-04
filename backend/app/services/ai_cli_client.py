"""
Claude CLI Client for Inkwest.

This module provides functions to execute the Claude CLI and parse its output.
It allows using the Claude CLI as an alternative to the Anthropic API,
enabling local development without API costs.
"""

import asyncio
import contextlib
import json
import logging
import os
import shlex
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.services.events import (
    ChatEvent,
    done_event,
    error_event,
    text_event,
    tool_result_event,
    tool_start_event,
)

logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when stopping the agent on an explicit
# cancel: let it shut down cleanly, but don't wait forever if it ignores the signal.
_STOP_GRACE_SECONDS = 5.0

# Large stdout buffer for big tool outputs (file reads, kubectl dumps) — shared by every
# in-container spawn path (claude-cli, inkwest-agent, opencode).
_SUBPROCESS_BUFFER_LIMIT = 100 * 1024 * 1024  # 100MB


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


async def _stream_stdout(
    process: asyncio.subprocess.Process,
    *,
    cancel_event: asyncio.Event | None,
    container: str | None,
    docker_path: str,
    pid_file: str | None,
    idle_timeout: float | None = None,
) -> AsyncIterator[str]:
    """Yield decoded stdout lines; on an explicit cancel, stop the agent and end.

    Each read is raced against ``cancel_event`` so a stop is honored within
    milliseconds instead of blocking until the agent's next line (it may be
    mid-tool or mid-thought, emitting nothing for a while). On cancel the agent
    is killed via the shared cancel-by-kill machinery and the stream ends — the
    caller surfaces the stopped state. Shared by the Claude CLI and the
    ``inkwest-agent`` spawn paths (identical spawn/stream/cancel mechanism).

    ``idle_timeout`` (seconds) guards against a silently-stalled subprocess: a model
    that never produces a first token, or a tool call that wedges (e.g. an MCP server
    that never responds), emits no stdout and never exits, so without this the read
    blocks forever and the single chat process loop is held — the whole session hangs
    in "thinking". The window is per-read, so it *resets on every emitted line*: a live
    turn (streaming tokens, finishing a slow tool) keeps renewing it; only genuine
    silence for the full window trips it. On trip the agent is killed and ``TimeoutError``
    is raised so the orchestrator surfaces a turn error and frees the loop. ``None``
    disables the guard.
    """
    assert process.stdout is not None
    cancel_task = asyncio.ensure_future(cancel_event.wait()) if cancel_event else None
    try:
        while True:
            read_task = asyncio.ensure_future(process.stdout.readline())
            waiters = {read_task} | ({cancel_task} if cancel_task else set())
            done, _ = await asyncio.wait(
                waiters, timeout=idle_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                # Neither a line nor a stop within the window → the subprocess is silent.
                # Kill it and surface a timeout; the deadline having *not* reset (no line
                # arrived this iteration) is exactly the stall signal we want to catch.
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                logger.error("Agent stream idle for %ss — stopping stalled agent", idle_timeout)
                await _stop_agent(
                    process,
                    container=container,
                    docker_path=docker_path,
                    pid_file=pid_file,
                )
                raise TimeoutError(f"agent produced no output for {idle_timeout}s")
            if cancel_task is not None and cancel_task in done:
                read_task.cancel()
                await asyncio.gather(read_task, return_exceptions=True)
                logger.info("Agent stream cancelled — stopping agent")
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
    idle_timeout: float | None = None,
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

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_SUBPROCESS_BUFFER_LIMIT,
        env=proc_env,
        cwd=run_cwd,
    )

    try:
        # Read stdout line by line, racing each read against an explicit stop (shared
        # streaming helper); the kill-on-cancel happens inside it.
        try:
            async for line in _stream_stdout(
                process,
                cancel_event=cancel_event,
                container=container,
                docker_path=docker_path,
                pid_file=pid_file,
                idle_timeout=idle_timeout,
            ):
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
    # Prompt-cache tokens (Anthropic usage shape). Claude Code caches aggressively, so on
    # Bedrock these dominate a turn's real cost — dropping them caused a ~10x undercount.
    total_cache_read_tokens = 0
    total_cache_write_tokens = 0
    # 1h-TTL portion of the writes (billed 2x, not 1.25x) — the Anthropic usage block
    # carries the split under cache_creation.ephemeral_1h_input_tokens.
    total_cache_write_1h_tokens = 0
    done_emitted = False

    def _usage_meta() -> dict[str, int]:
        return {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "cache_read_tokens": total_cache_read_tokens,
            "cache_write_tokens": total_cache_write_tokens,
            "cache_write_1h_tokens": total_cache_write_1h_tokens,
        }

    lines = aiter(output_stream)
    while True:
        try:
            line = await anext(lines)
        except StopAsyncIteration:
            break
        except (RuntimeError, TimeoutError):
            # Stream cut by a CLI crash/timeout (non-zero exit raises here, before any
            # terminal `done` could arrive). Surface the usage accumulated so far on an
            # internal usage_stats event — a failed turn still burned Bedrock spend — then
            # re-raise so the orchestrator handles the failure. The orchestrator consumes
            # usage_stats (never forwarded to the client). Same GOAL as
            # parse_opencode_response's cut-stream handling, different mechanism (opencode
            # attaches the usage to its client-visible error event instead).
            yield ChatEvent(type="usage_stats", metadata=_usage_meta())
            raise
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
                total_cache_read_tokens += usage.get("cache_read_input_tokens", 0) or 0
                total_cache_write_tokens += usage.get("cache_creation_input_tokens", 0) or 0
                cache_creation = usage.get("cache_creation") or {}
                total_cache_write_1h_tokens += (
                    cache_creation.get("ephemeral_1h_input_tokens", 0) or 0
                )

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
            # Result event carries final usage from CLI. Override per-field on key
            # PRESENCE, not truthiness: the result block is a cumulative authority, so a
            # present-but-0 value (e.g. a fully-cached turn's input) must replace the
            # accumulated per-assistant sum — mixing the two counting bases skews the turn.
            # A key entirely absent keeps the accumulated value (older CLIs omit cache keys).
            # `or {}`: a present-but-null "usage" must not crash the membership tests below.
            usage = parsed.get("usage") or {}
            if "input_tokens" in usage:
                total_input_tokens = usage["input_tokens"] or 0
            if "output_tokens" in usage:
                total_output_tokens = usage["output_tokens"] or 0
            if "cache_read_input_tokens" in usage:
                total_cache_read_tokens = usage["cache_read_input_tokens"] or 0
            if "cache_creation_input_tokens" in usage:
                total_cache_write_tokens = usage["cache_creation_input_tokens"] or 0
            if "ephemeral_1h_input_tokens" in (usage.get("cache_creation") or {}):
                total_cache_write_1h_tokens = (
                    usage["cache_creation"]["ephemeral_1h_input_tokens"] or 0
                )

            subtype = parsed.get("subtype")
            if subtype == "success":
                done_emitted = True
                yield ChatEvent(type="done", metadata=_usage_meta())

        elif event_type in ("message_stop", "done", "end"):
            done_emitted = True
            yield ChatEvent(type="done", metadata=_usage_meta())

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

    # Clean stream end without a terminal done (user Stop kills the CLI mid-turn and the
    # stream just ends): surface whatever accumulated so the orchestrator's stop path can
    # persist the burned tokens. Cache counts join the gate — a fully-cached turn reports
    # 0 fresh in/out yet burned billable tokens (same rule as turn_usage's persist gate).
    # Internal event — the orchestrator consumes it.
    if not done_emitted and (
        total_input_tokens
        or total_output_tokens
        or total_cache_read_tokens
        or total_cache_write_tokens
    ):
        yield ChatEvent(type="usage_stats", metadata=_usage_meta())


async def execute_agent(
    session_id: str,
    turn_spec: str,
    *,
    container: str,
    docker_path: str = "docker",
    agent_cmd: str = "inkwest-agent",
    extra_env: dict[str, str] | None = None,
    timeout: int = 300,
    cancel_event: asyncio.Event | None = None,
    idle_timeout: float | None = None,
) -> AsyncIterator[str]:
    """Spawn the in-sandbox ``inkwest-agent`` for one turn; stream its NDJSON stdout.

    The custom agentic loop runs INSIDE the conversation's toolbox container (parity
    with Claude Code), so the portal stays a thin broker. The fully-resolved turn-spec
    (JSON) is piped on stdin; each stdout line is one NDJSON ``ChatEvent`` (parsed by
    :func:`parse_agent_response`); the exit code is the terminal status.

    Args:
        session_id: Inkwest session ID (for logging + the PID-file name).
        turn_spec: The serialized turn-spec JSON handed to the agent on stdin.
        container: The per-conversation toolbox container to ``docker exec`` into.
        docker_path: Path to the ``docker`` CLI.
        agent_cmd: The in-container entrypoint (console script baked by P1.2).
        extra_env: Per-session connector credentials to expose to the agent (AWS_*
            file paths, GITLAB_TOKEN, KUBECONFIG). Each is passed as a value-less
            ``-e KEY``: docker reads the value from THIS client's env (never argv), so
            secrets stay out of command logs — the same one-way handoff as the CLI path.
        timeout: Seconds to wait for the agent to exit after its stream ends.
        cancel_event: Set by an explicit user stop. Cancel is by-kill (``docker exec``
            does not forward signals): the agent is signaled via its recorded PID.

    Yields:
        Lines of NDJSON output from the agent.

    Raises:
        TimeoutError: If the agent does not exit within ``timeout`` after its stream ends.
        RuntimeError: If the agent exits non-zero without an explicit cancel.
    """
    # Record the agent's own PID before handing off: `exec "$@"` replaces the shell with
    # the agent, so `$$` is the agent's PID inside the container — the target an explicit
    # stop signals (docker exec does NOT forward signals). One turn per session
    # (turn_runner) means no concurrent writers and Stop is only reachable once streaming
    # has begun, so a stop can't race ahead of this write. `-i` keeps stdin open so the
    # turn-spec reaches the agent (inherited through `sh -c ... exec "$@"`).
    pid_file = f"/tmp/inkwest-agent-{session_id}.pid"
    launch = ["sh", "-c", f'echo $$ > {shlex.quote(pid_file)}; exec "$@"', "sh", agent_cmd]
    # Value-less `-e KEY` forwards the connector creds from this client's env into the
    # container without ever putting the values in argv (same guarantee as the CLI path).
    env_flags = [flag for key in (extra_env or {}) for flag in ("-e", key)]
    cmd = [docker_path, "exec", "-i", *env_flags, container, *launch]

    logger.info(f"Spawning inkwest-agent for Inkwest session {session_id}")

    # Large buffer for big tool outputs (kubectl dumps, file reads), like the CLI path.

    # Connector creds are injected into THIS subprocess's env only (docker reads the
    # value-less `-e KEY` values from here) — never set process-globally, so sessions
    # stay isolated.
    proc_env = {**os.environ, **extra_env} if extra_env else None

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_SUBPROCESS_BUFFER_LIMIT,
        env=proc_env,
    )

    async def _feed_stdin() -> None:
        """Write the turn-spec then close stdin so the agent's read() returns.

        Done concurrently with reading stdout so a large turn-spec (history) can't
        deadlock against a full stdout pipe before we start draining it.
        """
        assert process.stdin is not None
        try:
            process.stdin.write(turn_spec.encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # Agent exited/closed stdin early (e.g. bad spec) — its error is on stdout.
            pass
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

    feed_task = asyncio.ensure_future(_feed_stdin())

    try:
        try:
            async for line in _stream_stdout(
                process,
                cancel_event=cancel_event,
                container=container,
                docker_path=docker_path,
                pid_file=pid_file,
                idle_timeout=idle_timeout,
            ):
                yield line
        except asyncio.CancelledError:
            logger.warning("Agent stream cancelled")
            raise
        finally:
            await asyncio.gather(feed_task, return_exceptions=True)

        # _stop_agent already terminated/reaped the process on an explicit cancel; don't
        # fall through to the returncode check, which would mis-report the kill as failure.
        if cancel_event and cancel_event.is_set():
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError(f"inkwest-agent timed out after {timeout}s")

        if process.returncode != 0:
            # Exit 1 is the agent's own error terminal status — it has already emitted an
            # `error` ChatEvent on stdout, so the broker surfaces that and treats this
            # raise as redundant. A non-zero exit with NO terminal event means the agent
            # crashed silently; the broker turns this into an error for the client.
            assert process.stderr is not None
            stderr = await process.stderr.read()
            stderr_text = stderr.decode().strip() if stderr else ""
            error_msg = stderr_text or f"Exit code {process.returncode}"
            logger.error(f"inkwest-agent failed with code {process.returncode}: {stderr_text}")
            raise RuntimeError(f"inkwest-agent failed: {error_msg}")

    except TimeoutError:
        raise
    except Exception:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise


def _chat_event_from_dict(data: dict) -> ChatEvent:
    """Reconstruct a ChatEvent from one decoded NDJSON line (the agent emits to_dict)."""
    return ChatEvent(
        type=data["type"],
        content=data.get("content"),
        tool_name=data.get("tool_name"),
        tool_input=data.get("tool_input"),
        tool_use_id=data.get("tool_use_id"),
        is_error=data.get("is_error", False),
        metadata=data.get("metadata") or {},
    )


async def parse_agent_response(output_stream: AsyncIterator[str]) -> AsyncIterator[ChatEvent]:
    """Convert the agent's NDJSON stdout into ChatEvents.

    Each line is one ``ChatEvent.to_dict()`` JSON object, so reconstruction is a
    straight ``json.loads`` + rebuild (no per-type parsing like the CLI's
    stream-json). Non-JSON or shapeless lines are skipped defensively.
    """
    async for line in output_stream:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(f"Non-JSON agent output: {line[:100]}")
            continue
        if not isinstance(data, dict) or "type" not in data:
            logger.debug(f"Skipping shapeless agent line: {line[:100]}")
            continue
        yield _chat_event_from_dict(data)


# =============================================================================
# opencode engine (anomalyco/opencode) — non-interactive `opencode run --format json`
#
# A second in-sandbox coding-agent engine, parity with the claude-cli path: spawn in
# the conversation container, stream NDJSON stdout, cancel-by-kill via a recorded PID.
# opencode emits its OWN JSONL schema (NOT our ChatEvent), so the parser translates it.
# The wire shapes below were captured live against Bedrock (opencode 1.17.10) — see
# tests/fixtures/opencode_stream_*.jsonl. Payload nests under ``part``:
#   step_start  {part:{type:"step-start"}}                          -> (dropped)
#   text        {part:{type:"text", text}}                          -> text_delta
#   tool_use    {part:{type:"tool", tool, callID, state:{          -> tool_start + tool_result
#                  status, input, output, metadata:{exit}}}}           (emitted post-completion)
#   step_finish {part:{reason, tokens:{input,output}, cost}}        -> accumulate; done on "stop"
#   error       {error:{name, message|data:{message}}}             -> error
# =============================================================================


def _as_dict(x: object) -> dict[str, Any]:
    """Narrow an arbitrary JSON value to a dict (empty when it isn't one)."""
    return x if isinstance(x, dict) else {}


def _opencode_error_message(data: dict[str, Any]) -> str:
    """Pull a human-readable message out of an opencode ``error`` event."""
    err = _as_dict(data.get("error"))
    inner = _as_dict(err.get("data"))
    return inner.get("message") or err.get("message") or err.get("name") or "opencode error"


async def parse_opencode_response(output_stream: AsyncIterator[str]) -> AsyncIterator[ChatEvent]:
    """Translate ``opencode run --format json`` NDJSON into the canonical ChatEvent stream.

    A turn is one or more *steps* (assistant message → optional tool calls → next step).
    ``tool_use`` arrives once, already completed, so we synthesize both ``tool_start`` and
    ``tool_result`` from it. Token counts + cost are per-step → accumulated and surfaced on
    the terminal ``done`` (emitted when a ``step_finish`` reports ``reason == "stop"``).
    """
    in_tokens = out_tokens = tool_calls = iterations = 0
    cache_read = cache_write = 0
    cost_usd = 0.0
    emitted_done = False
    emitted_error = False

    def _usage_meta() -> dict[str, Any]:
        return {
            "iterations": iterations,
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "tool_calls": tool_calls,
            "cost_usd": cost_usd,
        }

    lines = aiter(output_stream)
    while True:
        try:
            line = await anext(lines)
        except StopAsyncIteration:
            break
        except (RuntimeError, TimeoutError) as e:
            # Stream cut by a crash/timeout (non-zero exit raises here, before the
            # trailing block below could run): surface the reason WITH the usage
            # accumulated so far — a failed turn still burned Bedrock spend — then
            # re-raise so the broker marks the turn abnormal.
            if not emitted_done and not emitted_error:
                yield error_event(str(e), metadata=_usage_meta())
            raise
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(f"Non-JSON opencode output: {line[:120]}")
            continue
        if not isinstance(data, dict):
            continue
        etype = data.get("type")
        part = _as_dict(data.get("part"))

        if etype == "text":
            text = part.get("text")
            if text:
                yield text_event(text)
        elif etype == "tool_use":
            name = part.get("tool") or "tool"
            call_id = part.get("callID") or part.get("id") or ""
            state = _as_dict(part.get("state"))
            tool_input = _as_dict(state.get("input"))
            # A crashed (non-completed) tool carries its message in ``state.error``, not
            # ``output`` — fall through so the failure reason reaches the transcript (and
            # the orchestrator's AWS-expiry sniffing) instead of an empty result.
            output = state.get("output") or state.get("error") or ""
            meta = _as_dict(state.get("metadata"))
            exit_code = meta.get("exit")
            is_error = state.get("status") != "completed" or (exit_code not in (0, None))
            tool_calls += 1
            yield tool_start_event(name, tool_input, call_id)
            yield tool_result_event(name, str(output), call_id, is_error)
        elif etype == "step_finish":
            iterations += 1
            tokens = _as_dict(part.get("tokens"))
            cache = _as_dict(tokens.get("cache"))
            # FRESH input only — cache tokens ride their own counters (parity with the
            # claude-cli and inkwest-agent paths, and with the broker's wire metering).
            # They used to be folded into in_tokens "so stats reflect real prompt size";
            # now that every surface shows the cache splits explicitly, folding would
            # double-count them (once in the input figure, again in the cache columns).
            in_tokens += int(tokens.get("input") or 0)
            cache_read += int(cache.get("read") or 0)
            cache_write += int(cache.get("write") or 0)
            out_tokens += int(tokens.get("output") or 0)
            cost = part.get("cost")
            if isinstance(cost, (int, float)):
                cost_usd += float(cost)
            # A stop after an error must not add a second terminal (the error won and its
            # metadata carries the usage known at that point; these counters stay ≥ it).
            if part.get("reason") == "stop" and not emitted_done and not emitted_error:
                emitted_done = True
                yield done_event(metadata=_usage_meta())
        elif etype == "error":
            # Carry the usage accumulated so far on the error: a failed turn still burned
            # Bedrock spend that must be recorded. Keep DRAINING rather than return —
            # returning would abandon the suspended execute_opencode generator (its
            # process cleanup / exit-code check would then be GC-timed). Only the FIRST
            # error is terminal for the client; later ones are logged, not re-yielded.
            if emitted_error or emitted_done:
                logger.warning(f"suppressing extra opencode error: {_opencode_error_message(data)}")
                continue
            emitted_error = True
            yield error_event(_opencode_error_message(data), metadata=_usage_meta())
        # step_start and any unknown event types are intentionally ignored.

    # Stream ended without a terminal "stop" (max steps / abrupt end / killed). Still close
    # the turn so the broker persists usage instead of hanging — marked ``truncated`` so the
    # broker can tell it apart from a genuine ``stop``. Skipped when an ``error`` was already
    # surfaced: the turn already has its terminal event (usage rode its metadata).
    if not emitted_done and not emitted_error:
        yield done_event(metadata={**_usage_meta(), "truncated": True})


async def execute_opencode(
    session_id: str,
    user_message: str,
    *,
    container: str,
    model: str,
    resume: bool,
    docker_path: str = "docker",
    opencode_cmd: str = "opencode",
    workdir: str = "/workdir",
    extra_env: dict[str, str] | None = None,
    timeout: float = 300,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[str]:
    """Spawn ``opencode run`` in the conversation sandbox; stream its NDJSON stdout.

    Mirrors :func:`execute_agent` (spawn-in-container, stdout stream, cancel-by-kill via a
    recorded PID) but builds the opencode command instead of piping a turn-spec: the new
    user message rides as the final argv element and the prior transcript is resumed from
    opencode's own per-conversation session store (``--continue``) — the durable data dir
    is bind-mounted per conversation (see sandbox.py), so ``--continue`` always resolves to
    THIS conversation's single session. ``extra_env`` (connector creds) is forwarded as
    value-less ``-e KEY`` so secrets stay out of argv, exactly like the CLI/agent paths.
    """
    # Per-TURN pid file: the runner frees the session slot on the terminal event while the
    # previous turn's generator tail may still run — a session-keyed name would let that
    # tail's timeout/stop path signal the NEXT turn's opencode.
    pid_file = f"/tmp/inkwest-opencode-{session_id}-{uuid.uuid4().hex[:8]}.pid"
    oc_cmd = [
        opencode_cmd,
        "run",
        "--format",
        "json",
        "--dir",
        workdir,
        # opencode needs `<provider>/<model>`; Inkwest only routes Bedrock to this engine. The
        # broker passes a bare, catalog-resolvable id (ARN-pinned models are rejected upstream),
        # so we prefix unconditionally here.
        "-m",
        f"amazon-bedrock/{model}",
        # Isolated sandbox: no TTY to approve, so never leave a tool on "ask". The
        # environment (squid allowlist + scoped creds) is the boundary, not in-loop prompts.
        "--dangerously-skip-permissions",
        # Security: no external plugins. A prompt-injected turn could otherwise drop a
        # plugin under the durable /workdir that auto-executes on every later turn.
        "--pure",
    ]
    if resume:
        oc_cmd.append("--continue")
    # `--` terminates flag parsing so a message starting with `-` (a pasted diff hunk,
    # "--help", "--model=...") rides as the prompt instead of injecting opencode flags.
    oc_cmd += ["--", user_message]

    # `exec "$@"` replaces the shell with opencode so $$ (written to the PID file) IS the
    # process an explicit stop signals — docker exec does not forward signals.
    launch = ["sh", "-c", f'echo $$ > {shlex.quote(pid_file)}; exec "$@"', "sh", *oc_cmd]
    env_flags = [flag for key in (extra_env or {}) for flag in ("-e", key)]
    cmd = [docker_path, "exec", "-i", *env_flags, container, *launch]

    logger.info(f"Spawning opencode for Inkwest session {session_id}")
    proc_env = {**os.environ, **extra_env} if extra_env else None

    process = await asyncio.create_subprocess_exec(
        *cmd,
        # The prompt rides argv, not stdin → DEVNULL gives opencode an immediate EOF instead
        # of a dangling pipe (no _feed_stdin task needed, unlike the turn-spec agent path).
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_SUBPROCESS_BUFFER_LIMIT,
        env=proc_env,
    )

    try:
        try:
            async for line in _stream_stdout(
                process,
                cancel_event=cancel_event,
                container=container,
                docker_path=docker_path,
                pid_file=pid_file,
                # Bound the gap between lines too, not just the post-EOF wait: a wedged opencode
                # holding stdout open must not hang the turn (pinning slot + lease) forever.
                idle_timeout=timeout,
            ):
                yield line
        except asyncio.CancelledError:
            logger.warning("opencode stream cancelled")
            raise
        except TimeoutError:
            # Inactivity stall raised by _stream_stdout (agent already killed there).
            raise TimeoutError(f"opencode timed out after {timeout}s of inactivity")

        if cancel_event and cancel_event.is_set():
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            # `docker exec` doesn't forward signals, so killing only the local client would
            # leave opencode running in the container. Route through the shared stop machinery
            # (signals the in-container PID via its pid file) — same path as an explicit cancel.
            await _stop_agent(
                process, container=container, docker_path=docker_path, pid_file=pid_file
            )
            raise TimeoutError(f"opencode timed out after {timeout}s")

        if process.returncode != 0:
            assert process.stderr is not None
            stderr = await process.stderr.read()
            stderr_text = stderr.decode().strip() if stderr else ""
            error_msg = stderr_text or f"Exit code {process.returncode}"
            logger.error(f"opencode failed with code {process.returncode}: {stderr_text}")
            raise RuntimeError(f"opencode failed: {error_msg}")
    except TimeoutError:
        raise
    except Exception:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise
    finally:
        # Per-turn pid file would otherwise accumulate one-per-turn in the warm container's
        # /tmp for the life of the conversation. Best-effort reap (the container may already be
        # gone on an abnormal-teardown path — suppressed).
        with contextlib.suppress(Exception):
            rm = await asyncio.create_subprocess_exec(
                docker_path,
                "exec",
                container,
                "rm",
                "-f",
                pid_file,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(rm.wait(), timeout=5)
