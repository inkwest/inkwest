"""
Free-form exec tools for the standalone agent: ``bash`` / ``read_file`` / ``write_file``.

The sandboxed ``inkwest-agent`` engine gets arbitrary code/shell execution so a user can
ask it to *write and run* Python/bash for analysis — not just call the fixed connector
tools. Containment is the sandbox itself (squid egress allowlist + ``--internal`` net +
tmpfs creds), **not** this module: the safety argument is "free-form exec exists ONLY
inside the sandbox".

That is why these tools live entirely in the DB-free agent worker and are dispatched from
``loop.py`` BEFORE the shared ``execute_tool_calls`` path. They MUST NOT be added to
``app.tools.dispatch.execute_tool`` — that executor is also driven by the in-portal API
orchestrators, which run **UNSANDBOXED**, so free-form exec there would be RCE on the
backend host.

Mirrors the ``skills.py`` / ``clock.py`` convention: the predicate + executors live here;
the tool *names* and toolSpecs are owned by ``converse.py`` so the loop dispatches on them.

Two safety properties enforced locally (defense in depth — the sandbox is the real boundary):
- ``read_file`` / ``write_file`` are JAILED to ``/workdir`` (``realpath`` + ``commonpath``,
  the same pattern as ``app.services.attachments.resolve_existing``) so a model-chosen path
  can't traverse out or follow a symlink escape.
- ``bash`` runs with the model-call credentials SCRUBBED from its env
  (``AWS_BEARER_TOKEN_BEDROCK`` — the broker grant or the real backend bearer — and
  ``ANTHROPIC_API_KEY``), so a shell step can't replay the backend's privileged model
  identity against Bedrock/the broker. It keeps the connector env (kubectl/aws/gitlab) so
  the model can pipe real read-only commands, mirroring ``connector_exec._projected_aws_env``.
"""

import asyncio
import os
import signal

from app.tools.executor import truncate_output

from .converse import BASH_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL

# In-container bind mount the agent works in (cwd for bash; jail root for file tools).
# The broker mounts the conversation's durable workdir here and already projects @-file
# attachments as ``/workdir/<name>`` — so the path is a fixed in-sandbox constant.
WORKDIR = "/workdir"

# Default line window for read_file when the model omits it — a mandatory cap so a huge
# file can't blow the context in one call (the model pages with offset/limit).
READ_FILE_DEFAULT_LIMIT = 2000

# Fallback bash per-call timeout when the spec carries none (older broker). The broker
# normally sets limits.bash_timeout_seconds from its claude_cli_timeout; this only fires
# pre-v6. Matches the claude_cli_timeout default so behavior is consistent either way.
BASH_DEFAULT_TIMEOUT = 120

# Credentials that authenticate the *model* call (Bedrock backend / Anthropic). Stripped
# from the bash env: the connector `aws` tool has no service allowlist, so leaving the
# Bedrock bearer in a generic shell would let `aws bedrock-runtime ...` (or a curl of the
# broker data-plane) invoke on the backend's privileged identity. The model never needs to
# call the LLM from bash. Mirrors connector_exec._projected_aws_env, extended to a shell.
_STRIP_ENV = frozenset({"AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_API_KEY"})


def is_exec_tool(name: str) -> bool:
    """Whether a tool call targets one of the free-form exec tools."""
    return name in (BASH_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL)


def scrubbed_env() -> dict[str, str]:
    """The process env with the model-call credentials removed (see ``_STRIP_ENV``)."""
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}


def _resolve_in_workdir(workdir_root: str, path: str) -> str:
    """Resolve ``path`` (relative to the workdir, or absolute) confined to the workdir.

    Raises :class:`ValueError` on traversal / symlink escape. ``realpath`` resolves the
    symlinks of any existing prefix (so an escape via a symlinked parent is caught) and
    appends the rest lexically (so a not-yet-created target still resolves), then
    ``commonpath`` confirms it stays under the root — the same jail as
    ``attachments.resolve_existing``.
    """
    if not path:
        raise ValueError("path is required")
    root = os.path.realpath(workdir_root)
    candidate = path if os.path.isabs(path) else os.path.join(root, path)
    resolved = os.path.realpath(candidate)
    if resolved != root and os.path.commonpath([root, resolved]) != root:
        raise ValueError(f"path escapes the {workdir_root} workdir")
    return resolved


def read_file(tool_input: dict[str, object], workdir_root: str = WORKDIR) -> tuple[str, bool]:
    """Read a UTF-8 text file under the workdir as raw content (no line-number prefixes).

    ``{"path": str, "offset"?: int, "limit"?: int}`` — ``offset`` is a 0-based start line,
    ``limit`` caps the lines returned (default ``READ_FILE_DEFAULT_LIMIT``). Returns
    ``(content, is_error)``; a truncation marker is appended when more lines remain.
    """
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return ("Error: 'path' (the file to read) is required.", True)
    offset = tool_input.get("offset", 0)
    limit = tool_input.get("limit", READ_FILE_DEFAULT_LIMIT)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return ("Error: 'offset' must be a non-negative integer.", True)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return ("Error: 'limit' must be a positive integer.", True)

    try:
        resolved = _resolve_in_workdir(workdir_root, path)
    except ValueError as e:
        return (f"Error: {e}", True)

    if os.path.isdir(resolved):
        return (f"Error: '{path}' is a directory, not a file.", True)
    try:
        with open(resolved, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return (f"Error: file '{path}' not found.", True)
    except UnicodeDecodeError:
        return (f"Error: '{path}' is not UTF-8 text; use bash (e.g. xxd/file) for binary.", True)
    except OSError as e:
        return (f"Error reading '{path}': {e}", True)

    window = lines[offset : offset + limit]
    content = "".join(window)
    remaining = len(lines) - (offset + len(window))
    if remaining > 0:
        content += f"\n[Truncated — {remaining} more line(s); re-read with offset={offset + limit}]"
    # Backstop a pathological single huge line so one call can't blow the context window.
    return (truncate_output(content, READ_FILE_TOOL), False)


def write_file(tool_input: dict[str, object], workdir_root: str = WORKDIR) -> tuple[str, bool]:
    """Write a UTF-8 text file under the workdir. Overwrites; creates parent dirs in-jail.

    ``{"path": str, "content": str}``. Returns a terse confirmation (path + byte count),
    never echoing the content back (it's already in the model's context).
    """
    path = tool_input.get("path")
    if not isinstance(path, str) or not path:
        return ("Error: 'path' (the file to write) is required.", True)
    content = tool_input.get("content")
    if not isinstance(content, str):
        return ("Error: 'content' (a string) is required.", True)

    try:
        resolved = _resolve_in_workdir(workdir_root, path)
    except ValueError as e:
        return (f"Error: {e}", True)

    if os.path.isdir(resolved):
        return (f"Error: '{path}' is a directory.", True)
    try:
        # Parent dirs are re-checked by the jail: the resolved target is already confined,
        # so its dirname is too — mkdir can't create anything outside the workdir.
        os.makedirs(os.path.dirname(resolved) or workdir_root, exist_ok=True)
        data = content.encode("utf-8")
        with open(resolved, "wb") as f:
            f.write(data)
    except OSError as e:
        return (f"Error writing '{path}': {e}", True)

    return (f"Wrote {len(data)} bytes to {path}.", False)


async def bash(
    tool_input: dict[str, object],
    *,
    workdir_root: str = WORKDIR,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Run a shell command in the workdir; return ``(combined_output, is_error)``.

    cwd is the workdir; the env is the credential-scrubbed process env (so kubectl/aws/
    gitlab connector commands still resolve, but the model-call bearer cannot be replayed).
    stdout+stderr are merged and truncated like the kubectl executor. On timeout the
    process is killed (cancellation otherwise is by-kill: the broker terminates the agent).
    """
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return ("Error: 'command' (a shell command) is required.", True)

    run_env = env if env is not None else scrubbed_env()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge so the model sees errors inline
            cwd=workdir_root,
            env=run_env,
            # Own process group so a timeout kills the whole tree, not just /bin/sh: a
            # backgrounded grandchild (`sleep 1000 &`) would otherwise orphan and persist in
            # the durable, turn-reused sandbox, creeping toward the container --pids-limit.
            start_new_session=True,
        )
    except OSError as e:
        return (f"Error launching shell: {e}", True)

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        _kill_process_group(proc)
        await proc.wait()
        return (f"Command timed out after {timeout} seconds.", True)

    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    is_error = proc.returncode != 0
    if is_error:
        output = (
            f"[exit code {proc.returncode}]\n{output}"
            if output
            else f"[exit code {proc.returncode}]"
        )
    return (truncate_output(output, BASH_TOOL), is_error)


def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL the subprocess's whole process group (best-effort).

    The shell ran with ``start_new_session=True`` so its pid is the group leader; killing
    the group reaps backgrounded children too. Already-dead group ⇒ ProcessLookupError,
    swallowed. Falls back to killing just the shell if the group call isn't available.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run_exec_tool(
    name: str,
    tool_input: dict[str, object],
    *,
    workdir_root: str = WORKDIR,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Dispatch a free-form exec tool call to its handler. Returns ``(output, is_error)``."""
    if name == BASH_TOOL:
        return await bash(tool_input, workdir_root=workdir_root, timeout=timeout, env=env)
    if name == READ_FILE_TOOL:
        return read_file(tool_input, workdir_root)
    if name == WRITE_FILE_TOOL:
        return write_file(tool_input, workdir_root)
    return (f"Error: '{name}' is not an exec tool.", True)
