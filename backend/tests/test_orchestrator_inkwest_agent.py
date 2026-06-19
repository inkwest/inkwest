"""Tests for the broker side of the sandboxed inkwest-agent engine (P1.3).

Covers the spawn/stream/cancel primitives in ai_cli_client (execute_agent,
parse_agent_response) and the broker orchestrator (TurnSpec assembly, event
passthrough, role-correct persistence, cancel-by-kill) plus the get_orchestrator
engine dispatch.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import orchestrator_inkwest_agent as oia
from app.services.ai_cli_client import execute_agent, parse_agent_response

SESSION_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


async def _collect(gen):
    return [e async for e in gen]


async def _aiter(lines):
    for line in lines:
        yield line


def _proc(lines: list[bytes], *, returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """A fake agent subprocess: stdin sink + queued stdout lines + exit status."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.readline = AsyncMock(side_effect=[*lines, b""])
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=stderr)
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.terminate = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# execute_agent
# ---------------------------------------------------------------------------


class TestExecuteAgent:
    @pytest.mark.asyncio
    async def test_spawns_docker_exec_with_pidfile_and_feeds_stdin(self):
        captured: dict = {}
        proc = _proc([b'{"type": "text_delta", "content": "hi"}\n', b'{"type": "done"}\n'])

        def factory(*cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            out = [
                line
                async for line in execute_agent(
                    SESSION_ID,
                    '{"session_id": "x"}',
                    container="inkwest_conv_x",
                    docker_path="docker",
                )
            ]

        cmd = captured["cmd"]
        # docker exec -i into the conversation container, running the baked console script
        assert cmd[:4] == ["docker", "exec", "-i", "inkwest_conv_x"]
        assert "inkwest-agent" in cmd
        # PID recorded before exec so an explicit stop can target the in-container agent
        assert any(f"/tmp/inkwest-agent-{SESSION_ID}.pid" in part for part in cmd)
        # turn-spec piped on stdin, then stdin closed so the agent's read() returns
        proc.stdin.write.assert_called_once_with(b'{"session_id": "x"}')
        proc.stdin.close.assert_called_once()
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_extra_env_forwarded_as_value_less_e_flags(self):
        """Connector creds forwarded via `-e KEY` (value-less, read from client env)."""
        captured: dict = {}
        proc = _proc([b'{"type": "done"}\n'])

        def factory(*cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = kwargs.get("env")
            return proc

        env = {
            "GITLAB_TOKEN": "glpat-xxx",
            "AWS_SHARED_CREDENTIALS_FILE": "/home/agent/.aws/credentials",
        }
        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            async for _ in execute_agent(SESSION_ID, "{}", container="c", extra_env=env):
                pass

        cmd = captured["cmd"]
        # value-less -e KEY (key only, never the value → stays out of argv/logs)
        assert "-e" in cmd and "GITLAB_TOKEN" in cmd and "AWS_SHARED_CREDENTIALS_FILE" in cmd
        assert "glpat-xxx" not in cmd
        # docker reads the values from the client subprocess env
        assert captured["env"]["GITLAB_TOKEN"] == "glpat-xxx"

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_runtimeerror(self):
        proc = _proc([], returncode=1, stderr=b"boom")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="inkwest-agent failed: boom"):
                async for _ in execute_agent(SESSION_ID, "{}", container="c"):
                    pass

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        proc = _proc([b'{"type": "done"}\n'])
        # The post-stream wait_for times out (the agent didn't exit); the except path
        # kills it then re-raises with the descriptive message.
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", AsyncMock(side_effect=TimeoutError())),
        ):
            with pytest.raises(TimeoutError, match="timed out"):
                async for _ in execute_agent(SESSION_ID, "{}", container="c", timeout=1):
                    pass

    @pytest.mark.asyncio
    async def test_cancel_by_kill_signals_in_container_agent(self):
        """An explicit stop SIGTERMs the in-container agent via its recorded PID."""
        cancel_event = asyncio.Event()
        cancel_event.set()  # stop already requested before the first read

        never = asyncio.Event()

        async def hang():
            await never.wait()
            return b""

        main = MagicMock()
        main.stdin = MagicMock()
        main.stdin.write = MagicMock()
        main.stdin.drain = AsyncMock()
        main.stdin.close = MagicMock()
        main.stdout = MagicMock()
        main.stdout.readline = AsyncMock(side_effect=hang)
        main.returncode = None
        main.terminate = MagicMock()
        main.kill = MagicMock()
        main.wait = AsyncMock(return_value=0)

        scripts: list[str] = []
        calls = {"n": 0}

        async def factory(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return main
            scripts.append(args[-1])
            p = MagicMock()
            p.wait = AsyncMock(return_value=1)  # rc!=0 -> agent gone, no SIGKILL needed
            p.kill = MagicMock()
            return p

        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            out = [
                line
                async for line in execute_agent(
                    SESSION_ID, "{}", container="inkwest_conv_x", cancel_event=cancel_event
                )
            ]

        assert out == []  # stop won the race against readline
        assert any("kill -TERM" in s for s in scripts)
        main.terminate.assert_called()  # local docker-exec client dropped too


# ---------------------------------------------------------------------------
# parse_agent_response
# ---------------------------------------------------------------------------


class TestParseAgentResponse:
    @pytest.mark.asyncio
    async def test_reconstructs_chat_events(self):
        lines = [
            json.dumps({"type": "text_delta", "content": "hello"}),
            json.dumps(
                {
                    "type": "tool_start",
                    "tool_name": "kubectl_get",
                    "tool_input": {"r": "pods"},
                    "tool_use_id": "t1",
                }
            ),
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_name": "kubectl_get",
                    "content": "out",
                    "tool_use_id": "t1",
                    "is_error": False,
                }
            ),
            json.dumps({"type": "done", "metadata": {"input_tokens": 5, "output_tokens": 3}}),
        ]
        events = await _collect(parse_agent_response(_aiter(lines)))
        assert [e.type for e in events] == ["text_delta", "tool_start", "tool_result", "done"]
        assert events[0].content == "hello"
        assert events[1].tool_name == "kubectl_get"
        assert events[1].tool_input == {"r": "pods"}
        assert events[3].metadata == {"input_tokens": 5, "output_tokens": 3}

    @pytest.mark.asyncio
    async def test_skips_non_json_and_shapeless(self):
        lines = [
            "not json",
            json.dumps(["a", "list"]),
            json.dumps({"no": "type"}),
            json.dumps({"type": "done"}),
        ]
        events = await _collect(parse_agent_response(_aiter(lines)))
        assert [e.type for e in events] == ["done"]


# ---------------------------------------------------------------------------
# orchestrate_chat_inkwest_agent
# ---------------------------------------------------------------------------


def _patch_broker(
    *,
    history=None,
    entry=None,
    sandbox_enabled=True,
    model="meta.llama3-1-70b-instruct-v1:0",
    connector_env=None,
    injected=None,
):
    """Patch every DB/sandbox/model boundary the broker touches; return the stack + spies."""
    db = AsyncMock()
    db.commit = AsyncMock()
    spies = SimpleNamespace(
        save_message=AsyncMock(),
        ensure_title=AsyncMock(),
        update_stats=AsyncMock(),
        record_usage=AsyncMock(),
        ensure=AsyncMock(return_value="inkwest_conv_x"),
        release=AsyncMock(),
        build_connector_env=AsyncMock(return_value=(connector_env or {}, injected or [])),
        db=db,
    )
    settings = SimpleNamespace(
        sandbox_enabled=sandbox_enabled,
        sandbox_docker_path="docker",
        aws_region="eu-west-1",
        sandbox_home="/home/agent",
        claude_cli_timeout=120,
    )
    patches = [
        patch.object(oia, "get_settings", return_value=settings),
        patch.object(oia, "get_session_messages", AsyncMock(return_value=history or [])),
        patch.object(oia, "save_message", spies.save_message),
        patch.object(oia, "ensure_session_title", spies.ensure_title),
        patch.object(oia, "update_session_stats", spies.update_stats),
        patch.object(oia, "record_usage", spies.record_usage),
        patch.object(oia, "resolve_session_system_prompt", AsyncMock(return_value="base")),
        patch.object(oia, "get_system_prompt", MagicMock(return_value="SYSTEM")),
        patch.object(oia, "resolve_session_model", AsyncMock(return_value=model)),
        patch.object(oia.model_registry, "get_entry", MagicMock(return_value=entry)),
        patch.object(oia, "build_connector_env", spies.build_connector_env),
        patch.object(
            oia,
            "get_tool_definitions",
            MagicMock(return_value=[{"name": "aws_call"}, {"name": "kubectl_get"}]),
        ),
        # The broker does `from app.services import sandbox; sandbox.ensure(...)`. Patch the
        # real module's attributes (not sys.modules) so it works regardless of whether
        # another test already imported the module — `from ... import` resolves the cached
        # package attribute, which a sys.modules swap would not override.
        patch("app.services.sandbox.ensure", spies.ensure),
        patch("app.services.sandbox.release", spies.release),
    ]
    return patches, spies


async def _run_broker(spies, fake_lines, *, cancel_event=None, **kwargs):
    # execute_agent is an async-generator function (called, not awaited), so the fake is
    # a plain def returning an async iterator — an `async def` would yield a coroutine.
    def fake_execute_agent(session_id, turn_spec, **kw):
        spies.turn_spec = turn_spec  # capture for assertions
        spies.execute_kwargs = kw
        return _aiter(fake_lines)

    base = dict(
        db=spies.db,
        session_id=SESSION_ID,
        cluster_name=None,
        kubeconfig_path="/etc/inkwest/kubeconfigs/c.yaml",
        context_name="ctx",
        user_message="why crash?",
        user_id=USER_ID,
        cancel_event=cancel_event,
    )
    base.update(kwargs)
    # Collect inside the patch so it stays active while the generator is consumed.
    with patch.object(oia, "execute_agent", side_effect=fake_execute_agent):
        return await _collect(oia.orchestrate_chat_inkwest_agent(**base))


class TestOrchestrateInkwestAgent:
    @pytest.mark.asyncio
    async def test_turn_spec_assembly(self):
        entry = SimpleNamespace(arn="arn:aws:bedrock:::model/x", options={"max_tokens": 2048})
        patches, spies = _patch_broker(
            history=[{"role": "user", "content": "earlier"}], entry=entry
        )
        lines = [json.dumps({"type": "done", "metadata": {}})]
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, lines)
        finally:
            for p in reversed(patches):
                p.stop()

        spec = json.loads(spies.turn_spec)
        assert spec["session_id"] == SESSION_ID
        assert spec["user_message"] == "why crash?"
        assert spec["model"] == "meta.llama3-1-70b-instruct-v1:0"
        assert spec["model_target"] == "arn:aws:bedrock:::model/x"
        assert spec["system_prompt"] == "SYSTEM"
        assert spec["region"] == "eu-west-1"
        assert spec["user_id"] == str(USER_ID)
        assert spec["kubeconfig_path"] == "/etc/inkwest/kubeconfigs/c.yaml"
        assert spec["context_name"] == "ctx"
        # nested contract (P1.4)
        assert spec["engine"] == "inkwest-agent"
        assert spec["context"]["mode"] == "replay"
        assert spec["context"]["history"] == [{"role": "user", "content": "earlier"}]
        assert spec["limits"]["max_tokens"] == 2048
        assert spec["tools"] == [
            {"name": "aws_call", "exec_profile": "agent"},
            {"name": "kubectl_get", "exec_profile": "agent"},
        ]
        assert spec["creds_path"] == "/home/agent/.aws"
        # sandbox lease taken and released
        spies.ensure.assert_awaited_once_with(SESSION_ID)
        spies.release.assert_awaited_once_with(SESSION_ID)

    @pytest.mark.asyncio
    async def test_retry_drops_trailing_user_message_from_history(self):
        # On retry the errored user row is already stored as the last message.
        history = [
            {"role": "assistant", "content": "prev"},
            {"role": "user", "content": "why crash?"},
        ]
        patches, spies = _patch_broker(history=history)
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, [json.dumps({"type": "done"})], persist_user_message=False)
        finally:
            for p in reversed(patches):
                p.stop()
        spec = json.loads(spies.turn_spec)
        # trailing user turn removed so it isn't sent twice (history + user_message)
        assert spec["context"]["history"] == [{"role": "assistant", "content": "prev"}]
        # retry must not persist the user message again
        assert all(c.args[2] != "user" for c in spies.save_message.await_args_list)

    @pytest.mark.asyncio
    async def test_connector_env_projected_and_forwarded(self):
        """Broker pre-provides creds: build_connector_env is projected, forwarded as extra_env."""
        env = {
            "AWS_SHARED_CREDENTIALS_FILE": "/home/agent/.aws/credentials",
            "GITLAB_TOKEN": "glpat-xxx",
        }
        patches, spies = _patch_broker(connector_env=env, injected=["aws", "gitlab"])
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, [json.dumps({"type": "done"})])
        finally:
            for p in reversed(patches):
                p.stop()
        spies.build_connector_env.assert_awaited_once_with(USER_ID, uuid.UUID(SESSION_ID))
        # creds forwarded to the agent spawn as extra_env (one-way handoff)
        assert spies.execute_kwargs["extra_env"] == env

    @pytest.mark.asyncio
    async def test_no_connector_env_forwards_none(self):
        """No granted connectors → extra_env is None (no -e flags)."""
        patches, spies = _patch_broker(connector_env={}, injected=[])
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, [json.dumps({"type": "done"})])
        finally:
            for p in reversed(patches):
                p.stop()
        assert spies.execute_kwargs["extra_env"] is None

    @pytest.mark.asyncio
    async def test_connector_env_failure_degrades_gracefully(self):
        """A cred-projection error must not crash the turn — degrade to no connector env."""
        patches, spies = _patch_broker()
        spies.build_connector_env.side_effect = RuntimeError("SSO down")
        for p in patches:
            p.start()
        try:
            events = await _run_broker(spies, [json.dumps({"type": "done"})])
        finally:
            for p in reversed(patches):
                p.stop()
        # turn still completes; no creds forwarded; sandbox still leased+released
        assert events[-1].type == "done"
        assert spies.execute_kwargs["extra_env"] is None
        spies.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_user_id_skips_connector_env(self):
        patches, spies = _patch_broker()
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, [json.dumps({"type": "done"})], user_id=None)
        finally:
            for p in reversed(patches):
                p.stop()
        spies.build_connector_env.assert_not_awaited()
        assert spies.execute_kwargs["extra_env"] is None

    @pytest.mark.asyncio
    async def test_passthrough_and_persistence(self):
        patches, spies = _patch_broker()
        lines = [
            json.dumps({"type": "text_delta", "content": "looking"}),
            json.dumps(
                {
                    "type": "tool_start",
                    "tool_name": "kubectl_get",
                    "tool_input": {"r": "pods"},
                    "tool_use_id": "t1",
                }
            ),
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_name": "kubectl_get",
                    "content": "crashloop",
                    "tool_use_id": "t1",
                    "is_error": False,
                }
            ),
            json.dumps({"type": "text_delta", "content": "found it"}),
            json.dumps(
                {
                    "type": "done",
                    "metadata": {
                        "input_tokens": 12,
                        "output_tokens": 7,
                        "tool_calls": 1,
                        "iterations": 2,
                    },
                }
            ),
        ]
        for p in patches:
            p.start()
        try:
            events = await _run_broker(spies, lines)
        finally:
            for p in reversed(patches):
                p.stop()

        # every agent event reaches the client unchanged
        assert [e.type for e in events] == [
            "text_delta",
            "tool_start",
            "tool_result",
            "text_delta",
            "done",
        ]
        # persistence: user + role-correct assistant/tool_result messages
        roles = [c.args[2] for c in spies.save_message.await_args_list]
        assert roles == ["user", "assistant", "tool_result", "assistant"]
        # first assistant message carries the text + tool_use, second the trailing text
        first_assistant = spies.save_message.await_args_list[1].args[3]
        assert [b["type"] for b in first_assistant] == ["text", "tool_use"]
        tool_msg = spies.save_message.await_args_list[2].args[3]
        assert tool_msg[0]["type"] == "tool_result" and tool_msg[0]["tool_use_id"] == "t1"
        # usage from the done meta
        spies.update_stats.assert_awaited_once()
        assert spies.update_stats.await_args.kwargs["input_tokens"] == 12
        assert spies.update_stats.await_args.kwargs["tool_calls"] == 1
        spies.record_usage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_emits_stopped_done(self):
        patches, spies = _patch_broker()
        cancel_event = asyncio.Event()
        cancel_event.set()
        for p in patches:
            p.start()
        try:
            events = await _run_broker(spies, [], cancel_event=cancel_event)
        finally:
            for p in reversed(patches):
                p.stop()
        assert events[-1].type == "done"
        assert events[-1].metadata.get("stopped") is True
        # no usage persisted on a cancelled turn
        spies.record_usage.assert_not_awaited()
        spies.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_discards_partial_to_avoid_dangling_tool_use(self):
        """Kill mid-tool: a tool_use with no tool_result must NOT be persisted.

        Otherwise the next Converse turn's history has a dangling toolUse and Bedrock
        rejects it. On cancel the broker discards the partial assistant content.
        """
        patches, spies = _patch_broker()
        cancel_event = asyncio.Event()
        cancel_event.set()
        # stream delivered text + a tool_start, but the kill landed before tool_result
        lines = [
            json.dumps({"type": "text_delta", "content": "checking"}),
            json.dumps(
                {
                    "type": "tool_start",
                    "tool_name": "kubectl_get",
                    "tool_input": {"r": "pods"},
                    "tool_use_id": "t1",
                }
            ),
        ]
        for p in patches:
            p.start()
        try:
            events = await _run_broker(spies, lines, cancel_event=cancel_event)
        finally:
            for p in reversed(patches):
                p.stop()
        assert events[-1].type == "done" and events[-1].metadata.get("stopped") is True
        # only the user message was persisted — no assistant/tool_result partial
        roles = [c.args[2] for c in spies.save_message.await_args_list]
        assert roles == ["user"]
        spies.release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_streamed_error_then_nonzero_exit_not_double_reported(self):
        patches, spies = _patch_broker()
        lines = [json.dumps({"type": "error", "content": "boom"})]

        def failing(session_id, turn_spec, **kw):
            spies.turn_spec = turn_spec

            async def gen():
                for line in lines:
                    yield line
                raise RuntimeError("inkwest-agent failed: exit 1")

            return gen()

        for p in patches:
            p.start()
        try:
            with patch.object(oia, "execute_agent", side_effect=failing):
                events = await _collect(
                    oia.orchestrate_chat_inkwest_agent(
                        db=spies.db,
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="hi",
                        user_id=USER_ID,
                    )
                )
        finally:
            for p in reversed(patches):
                p.stop()
        # exactly one error (the streamed one); the redundant raise is swallowed
        assert [e.type for e in events] == ["error"]
        assert events[0].content == "boom"

    @pytest.mark.asyncio
    async def test_silent_crash_surfaces_error(self):
        patches, spies = _patch_broker()

        def crashing(session_id, turn_spec, **kw):
            spies.turn_spec = turn_spec

            async def gen():
                raise RuntimeError("inkwest-agent failed: Exit code 137")
                yield  # pragma: no cover

            return gen()

        for p in patches:
            p.start()
        try:
            with patch.object(oia, "execute_agent", side_effect=crashing):
                events = await _collect(
                    oia.orchestrate_chat_inkwest_agent(
                        db=spies.db,
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="hi",
                        user_id=USER_ID,
                    )
                )
        finally:
            for p in reversed(patches):
                p.stop()
        assert [e.type for e in events] == ["error"]
        assert "137" in events[0].content

    @pytest.mark.asyncio
    async def test_sandbox_disabled_errors(self):
        patches, spies = _patch_broker(sandbox_enabled=False)
        for p in patches:
            p.start()
        try:
            events = await _run_broker(spies, [])
        finally:
            for p in reversed(patches):
                p.stop()
        assert events[0].type == "error"
        spies.ensure.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_orchestrator engine dispatch
# ---------------------------------------------------------------------------


class TestEngineDispatch:
    async def _dispatch(self, *, model, entry, agent_enabled, sandbox_enabled):
        from app.services.ai_client import get_orchestrator

        seen = {}

        async def fake_cli(**kwargs):
            seen["engine"] = "cli"
            if False:
                yield  # pragma: no cover

        async def fake_agent(**kwargs):
            seen["engine"] = "agent"
            if False:
                yield  # pragma: no cover

        with (
            patch("app.services.ai_client.get_settings") as gs,
            patch("app.services.ai_client.resolve_session_model", AsyncMock(return_value=model)),
            patch("app.services.ai_client.get_entry", MagicMock(return_value=entry)),
            patch("app.services.engines.inkwest_agent_enabled", return_value=agent_enabled),
            patch("app.services.orchestrator_cli.orchestrate_chat_cli", fake_cli),
            patch(
                "app.services.orchestrator_inkwest_agent.orchestrate_chat_inkwest_agent",
                fake_agent,
            ),
        ):
            gs.return_value.ai_provider = "claude-cli"
            gs.return_value.sandbox_enabled = sandbox_enabled
            orchestrator = get_orchestrator()
            async for _ in orchestrator(db=AsyncMock(), session_id=SESSION_ID, user_message="x"):
                pass
        return seen.get("engine")

    @pytest.mark.asyncio
    async def test_non_claude_model_routes_to_agent(self):
        entry = SimpleNamespace(provider="meta", arn=None, options={})
        assert (
            await self._dispatch(
                model="meta.llama3-1-70b-instruct-v1:0",
                entry=entry,
                agent_enabled=True,
                sandbox_enabled=True,
            )
            == "agent"
        )

    @pytest.mark.asyncio
    async def test_claude_model_routes_to_cli(self):
        entry = SimpleNamespace(provider="anthropic", arn=None, options={})
        assert (
            await self._dispatch(
                model="anthropic.claude-3-5-sonnet",
                entry=entry,
                agent_enabled=True,
                sandbox_enabled=True,
            )
            == "cli"
        )

    @pytest.mark.asyncio
    async def test_non_claude_falls_back_to_cli_when_agent_disabled(self):
        entry = SimpleNamespace(provider="meta", arn=None, options={})
        assert (
            await self._dispatch(
                model="meta.llama3-1-70b-instruct-v1:0",
                entry=entry,
                agent_enabled=False,
                sandbox_enabled=True,
            )
            == "cli"
        )

    @pytest.mark.asyncio
    async def test_non_claude_falls_back_to_cli_when_sandbox_off(self):
        entry = SimpleNamespace(provider="meta", arn=None, options={})
        assert (
            await self._dispatch(
                model="meta.llama3-1-70b-instruct-v1:0",
                entry=entry,
                agent_enabled=True,
                sandbox_enabled=False,
            )
            == "cli"
        )
