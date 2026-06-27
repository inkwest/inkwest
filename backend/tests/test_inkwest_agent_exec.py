"""Tests for the inkwest-agent free-form exec tools (bash / read_file / write_file).

Covers the /workdir path-jail (traversal + symlink escape), read/write semantics, the
bash credential-env scrub, output truncation/timeout, the Converse tool specs, the
protocol bash_timeout field, and the loop routing (a bash call must hit the exec branch,
never the shared — unsandboxed — execute_tool_calls).
"""

import os

import pytest

from app.services.events import ChatEvent
from inkwest_agent import converse as converse_mod
from inkwest_agent import exec as exec_mod
from inkwest_agent import loop as loop_mod
from inkwest_agent.converse import (
    BASH_TOOL,
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    exec_tool_specs,
    to_converse_tools,
)
from inkwest_agent.protocol import PROTOCOL_VERSION, TurnSpecError, parse_turn_spec

SESSION_ID = "11111111-1111-1111-1111-111111111111"
_BASE_SPEC = {
    "session_id": SESSION_ID,
    "user_message": "x",
    "model": "m",
    "system_prompt": "s",
    "region": "eu-west-1",
}


# --------------------------------------------------------------------------- #
# predicate + env scrub
# --------------------------------------------------------------------------- #


def test_is_exec_tool():
    assert exec_mod.is_exec_tool(BASH_TOOL)
    assert exec_mod.is_exec_tool(READ_FILE_TOOL)
    assert exec_mod.is_exec_tool(WRITE_FILE_TOOL)
    assert not exec_mod.is_exec_tool("kubectl_get")
    assert not exec_mod.is_exec_tool("read_skill")


def test_scrubbed_env_strips_model_credentials(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "grant-xyz")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-keep")
    monkeypatch.setenv("KUBECONFIG", "/tmp/kc")
    env = exec_mod.scrubbed_env()
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert "ANTHROPIC_API_KEY" not in env
    # Connector creds the model legitimately pipes via bash must survive.
    assert env["GITLAB_TOKEN"] == "glpat-keep"
    assert env["KUBECONFIG"] == "/tmp/kc"


# --------------------------------------------------------------------------- #
# path jail
# --------------------------------------------------------------------------- #


def test_resolve_in_workdir_accepts_relative_and_absolute(tmp_path):
    root = str(tmp_path)
    assert exec_mod._resolve_in_workdir(root, "a.txt") == os.path.join(root, "a.txt")
    assert exec_mod._resolve_in_workdir(root, os.path.join(root, "b.txt")) == os.path.join(
        root, "b.txt"
    )


def test_resolve_in_workdir_rejects_traversal(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        exec_mod._resolve_in_workdir(str(tmp_path), "../escape.txt")


def test_resolve_in_workdir_rejects_absolute_outside(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        exec_mod._resolve_in_workdir(str(tmp_path), "/etc/passwd")


def test_resolve_in_workdir_rejects_symlink_escape(tmp_path):
    # A symlink inside the workdir pointing out must not let a read/write follow it out.
    outside = tmp_path.parent / "outside"
    outside.mkdir()
    (outside / "secret").write_text("top secret")
    link = tmp_path / "link"
    os.symlink(str(outside), str(link))
    with pytest.raises(ValueError, match="escapes"):
        exec_mod._resolve_in_workdir(str(tmp_path), "link/secret")


def test_resolve_in_workdir_requires_path(tmp_path):
    with pytest.raises(ValueError, match="required"):
        exec_mod._resolve_in_workdir(str(tmp_path), "")


# --------------------------------------------------------------------------- #
# read_file
# --------------------------------------------------------------------------- #


def test_read_file_basic(tmp_path):
    (tmp_path / "f.txt").write_text("line1\nline2\n")
    content, is_error = exec_mod.read_file({"path": "f.txt"}, str(tmp_path))
    assert is_error is False
    assert content == "line1\nline2\n"  # raw content, no line-number prefixes


def test_read_file_offset_and_limit_window(tmp_path):
    (tmp_path / "f.txt").write_text("".join(f"l{i}\n" for i in range(10)))
    content, is_error = exec_mod.read_file(
        {"path": "f.txt", "offset": 2, "limit": 3}, str(tmp_path)
    )
    assert is_error is False
    assert content.startswith("l2\nl3\nl4\n")
    # More lines remain → a truncation marker pointing at the next page.
    assert "offset=5" in content


def test_read_file_default_limit_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(exec_mod, "READ_FILE_DEFAULT_LIMIT", 5)
    (tmp_path / "f.txt").write_text("".join(f"l{i}\n" for i in range(20)))
    content, is_error = exec_mod.read_file({"path": "f.txt"}, str(tmp_path))
    assert is_error is False
    assert "Truncated" in content and "15 more line" in content


def test_read_file_missing(tmp_path):
    content, is_error = exec_mod.read_file({"path": "nope.txt"}, str(tmp_path))
    assert is_error is True and "not found" in content


def test_read_file_directory(tmp_path):
    (tmp_path / "sub").mkdir()
    content, is_error = exec_mod.read_file({"path": "sub"}, str(tmp_path))
    assert is_error is True and "directory" in content


def test_read_file_binary_rejected(tmp_path):
    (tmp_path / "bin").write_bytes(b"\xff\xfe\x00\x01")
    content, is_error = exec_mod.read_file({"path": "bin"}, str(tmp_path))
    assert is_error is True and "UTF-8" in content


def test_read_file_requires_path(tmp_path):
    content, is_error = exec_mod.read_file({}, str(tmp_path))
    assert is_error is True and "path" in content


def test_read_file_rejects_bad_offset_limit(tmp_path):
    (tmp_path / "f.txt").write_text("x\n")
    assert exec_mod.read_file({"path": "f.txt", "offset": -1}, str(tmp_path))[1] is True
    assert exec_mod.read_file({"path": "f.txt", "limit": 0}, str(tmp_path))[1] is True
    # bool is an int subclass — must be rejected, not treated as 1/0.
    assert exec_mod.read_file({"path": "f.txt", "limit": True}, str(tmp_path))[1] is True


def test_read_file_escape_rejected(tmp_path):
    content, is_error = exec_mod.read_file({"path": "../x"}, str(tmp_path))
    assert is_error is True and "escapes" in content


# --------------------------------------------------------------------------- #
# write_file
# --------------------------------------------------------------------------- #


def test_write_file_writes_and_confirms_terse(tmp_path):
    out, is_error = exec_mod.write_file({"path": "f.txt", "content": "hello"}, str(tmp_path))
    assert is_error is False
    assert (tmp_path / "f.txt").read_text() == "hello"
    # Terse confirmation: path + byte count, never the content echoed back.
    assert out == "Wrote 5 bytes to f.txt."
    assert "hello" not in out


def test_write_file_overwrites(tmp_path):
    (tmp_path / "f.txt").write_text("old")
    exec_mod.write_file({"path": "f.txt", "content": "new"}, str(tmp_path))
    assert (tmp_path / "f.txt").read_text() == "new"


def test_write_file_creates_parent_dirs_in_jail(tmp_path):
    out, is_error = exec_mod.write_file({"path": "a/b/f.txt", "content": "x"}, str(tmp_path))
    assert is_error is False
    assert (tmp_path / "a" / "b" / "f.txt").read_text() == "x"


def test_write_file_escape_rejected(tmp_path):
    out, is_error = exec_mod.write_file({"path": "../evil", "content": "x"}, str(tmp_path))
    assert is_error is True and "escapes" in out
    assert not (tmp_path.parent / "evil").exists()


def test_write_file_requires_path_and_content(tmp_path):
    assert exec_mod.write_file({"content": "x"}, str(tmp_path))[1] is True
    assert exec_mod.write_file({"path": "f.txt"}, str(tmp_path))[1] is True
    # Non-string content is rejected (not coerced).
    assert exec_mod.write_file({"path": "f.txt", "content": 5}, str(tmp_path))[1] is True


# --------------------------------------------------------------------------- #
# bash
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bash_runs_in_workdir(tmp_path):
    out, is_error = await exec_mod.bash({"command": "pwd"}, workdir_root=str(tmp_path), timeout=10)
    assert is_error is False
    assert os.path.realpath(out.strip()) == os.path.realpath(str(tmp_path))


@pytest.mark.asyncio
async def test_bash_merges_stderr_and_flags_exit_code(tmp_path):
    out, is_error = await exec_mod.bash(
        {"command": "echo oops >&2; exit 3"}, workdir_root=str(tmp_path), timeout=10
    )
    assert is_error is True
    assert "oops" in out and "exit code 3" in out


@pytest.mark.asyncio
async def test_bash_scrubs_credentials_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "grant-xyz")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-keep")
    out, is_error = await exec_mod.bash(
        {"command": "printenv | sort"}, workdir_root=str(tmp_path), timeout=10
    )
    assert is_error is False
    assert "grant-xyz" not in out and "sk-ant" not in out
    assert "glpat-keep" in out  # connector creds still piped through


@pytest.mark.asyncio
async def test_bash_timeout_kills(tmp_path):
    out, is_error = await exec_mod.bash(
        {"command": "sleep 5"}, workdir_root=str(tmp_path), timeout=1
    )
    assert is_error is True and "timed out" in out


@pytest.mark.asyncio
async def test_bash_timeout_kills_backgrounded_children(tmp_path):
    # A timeout must reap the whole process group, not just /bin/sh: a backgrounded child
    # that would touch a marker AFTER the timeout must never get the chance.
    import asyncio as _asyncio

    out, is_error = await exec_mod.bash(
        {"command": "(sleep 2; touch alive.txt) & sleep 30"},
        workdir_root=str(tmp_path),
        timeout=1,
    )
    assert is_error is True and "timed out" in out
    await _asyncio.sleep(2.5)  # past when the orphan would have fired
    assert not (tmp_path / "alive.txt").exists()


@pytest.mark.asyncio
async def test_bash_requires_command(tmp_path):
    out, is_error = await exec_mod.bash({"command": "  "}, workdir_root=str(tmp_path), timeout=10)
    assert is_error is True and "command" in out


@pytest.mark.asyncio
async def test_bash_output_truncated(tmp_path, monkeypatch):
    # Reuse the kubectl truncation helper; a huge stdout is capped, not streamed whole.
    monkeypatch.setattr(exec_mod, "truncate_output", lambda o, n: o[:100] + "[CUT]")
    out, is_error = await exec_mod.bash(
        {"command": "yes x | head -n 1000"}, workdir_root=str(tmp_path), timeout=10
    )
    assert out.endswith("[CUT]")


def test_write_file_directory_target(tmp_path):
    (tmp_path / "d").mkdir()
    out, is_error = exec_mod.write_file({"path": "d", "content": "x"}, str(tmp_path))
    assert is_error is True and "directory" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_read_file_unreadable(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    f.chmod(0o000)
    try:
        content, is_error = exec_mod.read_file({"path": "f.txt"}, str(tmp_path))
        assert is_error is True and "Error reading" in content
    finally:
        f.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_write_file_oserror(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)  # read+exec only → write fails
    try:
        out, is_error = exec_mod.write_file({"path": "ro/f.txt", "content": "x"}, str(tmp_path))
        assert is_error is True and "Error writing" in out
    finally:
        ro.chmod(0o755)


@pytest.mark.asyncio
async def test_bash_launch_error_bad_cwd(tmp_path):
    # A non-existent cwd makes the subprocess launch raise — surfaced, not crashed.
    out, is_error = await exec_mod.bash(
        {"command": "ls"}, workdir_root=str(tmp_path / "nope"), timeout=5
    )
    assert is_error is True


def test_kill_process_group_noop_when_already_exited():
    from unittest.mock import MagicMock

    proc = MagicMock()
    proc.returncode = 0  # already dead → must not touch killpg
    exec_mod._kill_process_group(proc)
    proc.kill.assert_not_called()


def test_kill_process_group_swallows_lookup(monkeypatch):
    from unittest.mock import MagicMock

    proc = MagicMock(returncode=None, pid=999999)

    def _raise(*_a):
        raise ProcessLookupError

    monkeypatch.setattr(exec_mod.os, "killpg", _raise)
    exec_mod._kill_process_group(proc)  # must not raise


def test_kill_process_group_falls_back_to_proc_kill(monkeypatch):
    from unittest.mock import MagicMock

    proc = MagicMock(returncode=None, pid=1)

    def _raise(*_a):
        raise OSError("not permitted")

    monkeypatch.setattr(exec_mod.os, "killpg", _raise)
    exec_mod._kill_process_group(proc)
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_run_exec_tool_dispatch(tmp_path):
    assert (await exec_mod.run_exec_tool("nope", {}, timeout=5))[1] is True
    out, is_error = await exec_mod.run_exec_tool(
        WRITE_FILE_TOOL, {"path": "f", "content": "y"}, workdir_root=str(tmp_path), timeout=5
    )
    assert is_error is False and (tmp_path / "f").read_text() == "y"
    # read_file dispatch path too.
    r, rerr = await exec_mod.run_exec_tool(
        READ_FILE_TOOL, {"path": "f"}, workdir_root=str(tmp_path), timeout=5
    )
    assert rerr is False and r == "y"
    # bash dispatch path.
    b, berr = await exec_mod.run_exec_tool(
        BASH_TOOL, {"command": "echo hi"}, workdir_root=str(tmp_path), timeout=5
    )
    assert berr is False and "hi" in b


# --------------------------------------------------------------------------- #
# write -> bash-run -> read_file (DoD behavioral roundtrip)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_write_bash_read_roundtrip(tmp_path):
    root = str(tmp_path)
    w, werr = exec_mod.write_file({"path": "compute.py", "content": "print(6 * 7)"}, root)
    assert werr is False
    out, berr = await exec_mod.bash(
        {"command": "python3 compute.py > result.txt"}, workdir_root=root, timeout=15
    )
    assert berr is False
    r, rerr = exec_mod.read_file({"path": "result.txt"}, root)
    assert rerr is False and r.strip() == "42"


# --------------------------------------------------------------------------- #
# converse tool specs
# --------------------------------------------------------------------------- #


def test_exec_tool_specs_shape():
    specs = exec_tool_specs()
    names = [s["toolSpec"]["name"] for s in specs]
    assert names == [BASH_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL]
    by_name = {s["toolSpec"]["name"]: s["toolSpec"] for s in specs}
    assert by_name[BASH_TOOL]["inputSchema"]["json"]["required"] == ["command"]
    assert by_name[WRITE_FILE_TOOL]["inputSchema"]["json"]["required"] == ["path", "content"]
    assert by_name[READ_FILE_TOOL]["inputSchema"]["json"]["required"] == ["path"]


def test_to_converse_tools_includes_exec_tools(monkeypatch):
    monkeypatch.setattr(converse_mod, "get_tool_definitions", lambda: [])
    cfg = to_converse_tools()
    names = {t["toolSpec"]["name"] for t in cfg["tools"]}
    assert {BASH_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL} <= names


# --------------------------------------------------------------------------- #
# protocol — bash_timeout_seconds
# --------------------------------------------------------------------------- #


def test_protocol_version_bumped():
    assert PROTOCOL_VERSION == 6


def test_parse_bash_timeout_seconds():
    spec = parse_turn_spec({**_BASE_SPEC, "limits": {"bash_timeout_seconds": 90}})
    assert spec.limits.bash_timeout_seconds == 90


def test_bash_timeout_defaults_none():
    spec = parse_turn_spec(_BASE_SPEC)
    assert spec.limits.bash_timeout_seconds is None


def test_bash_timeout_rejects_non_int():
    with pytest.raises(TurnSpecError, match="bash_timeout_seconds"):
        parse_turn_spec({**_BASE_SPEC, "limits": {"bash_timeout_seconds": "90"}})
    with pytest.raises(TurnSpecError, match="bash_timeout_seconds"):
        parse_turn_spec({**_BASE_SPEC, "limits": {"bash_timeout_seconds": True}})


# --------------------------------------------------------------------------- #
# loop routing — exec branch, not the shared executor
# --------------------------------------------------------------------------- #


def _tool_use(tool_use_id: str, name: str, tool_input: dict) -> dict:
    return {
        "usage": {"inputTokens": 8, "outputTokens": 4},
        "output": {
            "message": {
                "content": [
                    {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}
                ]
            }
        },
        "stopReason": "tool_use",
    }


def _final(text: str) -> dict:
    return {
        "usage": {"inputTokens": 1, "outputTokens": 1},
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": "end_turn",
    }


@pytest.mark.asyncio
async def test_loop_routes_bash_to_exec_branch(monkeypatch):
    # A bash call must be dispatched to run_exec_tool, NEVER the shared (unsandboxed)
    # execute_tool_calls — that is the whole safety argument of these tools.
    from unittest.mock import MagicMock

    spec = parse_turn_spec({**_BASE_SPEC, "limits": {"bash_timeout_seconds": 77}})

    seen_timeout = {}

    async def fake_run_exec_tool(name, tool_input, *, timeout, **kw):
        seen_timeout["t"] = timeout
        return ("exec ran", False)

    monkeypatch.setattr(loop_mod, "run_exec_tool", fake_run_exec_tool)
    no_base = MagicMock(side_effect=AssertionError("base executor must not run for bash"))
    monkeypatch.setattr(loop_mod, "execute_tool_calls", no_base)

    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[_tool_use("b1", BASH_TOOL, {"command": "ls"}), _final("done")]
    )
    events: list[ChatEvent] = [ev async for ev in loop_mod.run_turn(spec, client)]
    result = next(e for e in events if e.type == "tool_result")
    assert result.tool_name == BASH_TOOL and result.content == "exec ran"
    # The broker-carried claude_cli_timeout reaches the bash call as its per-call timeout.
    assert seen_timeout["t"] == 77


@pytest.mark.asyncio
async def test_loop_bash_falls_back_to_default_timeout(monkeypatch):
    # No bash_timeout on the spec (older broker) → the agent's own default applies.
    from unittest.mock import MagicMock

    spec = parse_turn_spec(_BASE_SPEC)
    seen = {}

    async def fake_run_exec_tool(name, tool_input, *, timeout, **kw):
        seen["t"] = timeout
        return ("ok", False)

    monkeypatch.setattr(loop_mod, "run_exec_tool", fake_run_exec_tool)
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[_tool_use("b1", BASH_TOOL, {"command": "ls"}), _final("done")]
    )
    [ev async for ev in loop_mod.run_turn(spec, client)]
    assert seen["t"] == exec_mod.BASH_DEFAULT_TIMEOUT
