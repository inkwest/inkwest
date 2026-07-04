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

from app.services import llm_broker
from app.services import llm_proxy_token as token
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
    skills=None,
    enabled_mcp=None,
    mcp_config_path=None,
    mcp_dead=None,
    broker_settings=None,
):
    """Patch every DB/sandbox/model boundary the broker touches; return the stack + spies.

    ``broker_settings`` pins ``llm_broker``'s view of settings — defaulting the LLM auth broker
    OFF so these tests are hermetic against the developer's ``.env`` (which may set
    ``LLM_BROKER_ENABLED``). The broker-on tests pass an enabled SimpleNamespace.
    """
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
        apply_session_skills=MagicMock(),
        # OAuth2-aware materializer returns (in-container path, dead oauth keys).
        write_agent_mcp_config_with_oauth=AsyncMock(return_value=(mcp_config_path, mcp_dead or [])),
        remove_agent_mcp_config=MagicMock(),
        db=db,
    )
    settings = SimpleNamespace(
        sandbox_enabled=sandbox_enabled,
        sandbox_docker_path="docker",
        aws_region="eu-west-1",
        sandbox_home="/home/agent",
        claude_cli_timeout=120,
        agent_stream_idle_timeout=120,
        app_env="development",
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
        # P1.7 skills + MCP boundaries. Default: no skills, no MCP servers — the
        # skill/MCP-specific tests below override the return values.
        patch.object(oia, "get_effective_skills", AsyncMock(return_value=skills or [])),
        patch.object(oia, "get_enabled_mcp_servers", AsyncMock(return_value=enabled_mcp or [])),
        patch.object(oia, "apply_session_skills", spies.apply_session_skills),
        patch.object(
            oia, "skills_container_root", MagicMock(return_value="/home/agent/.claude/skills")
        ),
        patch(
            "app.services.agent_mcp_config.write_agent_mcp_config_with_oauth",
            spies.write_agent_mcp_config_with_oauth,
        ),
        patch(
            "app.services.agent_mcp_config.remove_agent_mcp_config", spies.remove_agent_mcp_config
        ),
        # The broker does `from app.services import sandbox; sandbox.ensure(...)`. Patch the
        # real module's attributes (not sys.modules) so it works regardless of whether
        # another test already imported the module — `from ... import` resolves the cached
        # package attribute, which a sys.modules swap would not override.
        patch("app.services.sandbox.ensure", spies.ensure),
        patch("app.services.sandbox.release", spies.release),
        # Pin the LLM auth broker's settings (default OFF) so connector_env assertions are
        # hermetic against the developer's .env (LLM_BROKER_ENABLED). Broker-on tests override.
        patch.object(
            llm_broker,
            "get_settings",
            return_value=broker_settings
            or SimpleNamespace(llm_broker_enabled=False, mcp_proxy_base_url=""),
        ),
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
    async def test_tool_results_routed_to_metrics(self):
        """The agent relay counts streamed tool_result events: native → tool_calls_total,
        mcp__<server>__<tool> → mcp_tool_calls_total (server/tool parsed). Locks the relay
        wiring, not just record_tool_call's routing in isolation."""
        from prometheus_client import REGISTRY

        patches, spies = _patch_broker(history=[], entry=None)
        lines = [
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_name": "l1_native_probe",
                    "content": "x",
                    "tool_use_id": "a",
                    "is_error": False,
                }
            ),
            json.dumps(
                {
                    "type": "tool_result",
                    "tool_name": "mcp__l1srv__l1tool",
                    "content": "y",
                    "tool_use_id": "b",
                    "is_error": False,
                }
            ),
            json.dumps({"type": "done", "metadata": {}}),
        ]
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, lines)
        finally:
            for p in reversed(patches):
                p.stop()

        # No dispatcher ran, so engine/model context defaults to "unknown".
        common = {"engine": "unknown", "model": "unknown", "is_error": "False"}
        assert (
            REGISTRY.get_sample_value(
                "inkwest_tool_calls_total", {"tool": "l1_native_probe", **common}
            )
            or 0
        ) >= 1
        assert (
            REGISTRY.get_sample_value(
                "inkwest_mcp_tool_calls_total", {"server": "l1srv", "tool": "l1tool", **common}
            )
            or 0
        ) >= 1

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
        # The broker appends a runtime-identity block so the agent knows its own model.
        assert spec["system_prompt"].startswith("SYSTEM")
        assert "Runtime identity" in spec["system_prompt"]
        assert "meta.llama3-1-70b-instruct-v1:0" in spec["system_prompt"]
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
        # sandbox lease taken (with the owner, for the per-user cap) and released
        spies.ensure.assert_awaited_once_with(SESSION_ID, str(USER_ID))
        spies.release.assert_awaited_once_with(SESSION_ID)
        # P1.7: no skills, no MCP servers in the default fixture → empty skills, no root,
        # no mcp_config_path; the MCP config is still removed (cleanup is unconditional —
        # it no-ops when there is no file).
        assert spec["skills"] == []
        # Unset optional scalars are OMITTED from the wire spec (forward-compat); the agent
        # re-applies the None default for absent keys.
        assert "skills_root" not in spec
        assert "mcp_config_path" not in spec
        # P1.8: no attachments + the SimpleNamespace entry has no modality data → empty
        # defaults (back-compat — the loop sends no image blocks for either).
        assert spec["attachments"] == []
        assert spec["input_modalities"] == []
        # remove runs twice: defensive prune after ensure (orphan from a SIGKILLed prior
        # turn) + turn-end cleanup in finally. Both target this session.
        assert spies.remove_agent_mcp_config.call_count == 2
        assert all(c.args == (SESSION_ID,) for c in spies.remove_agent_mcp_config.call_args_list)

    async def test_refuses_turn_at_hard_cap(self):
        """When enforce_user_sandbox_cap raises, the turn is refused with a user-facing
        error and no sandbox is provisioned."""
        from app.services.sandbox import SandboxCapExceededError

        patches, spies = _patch_broker()
        patches.append(
            patch(
                "app.services.sandbox.enforce_user_sandbox_cap",
                AsyncMock(side_effect=SandboxCapExceededError(active=8, hard_cap=7)),
            )
        )
        for p in patches:
            p.start()
        try:
            events = await _run_broker(spies, [json.dumps({"type": "done"})])
        finally:
            for p in reversed(patches):
                p.stop()

        assert any(
            e.type == "error" and "Too many conversations" in (e.content or "") for e in events
        )
        spies.ensure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_attachments_and_modalities_projected_to_turn_spec(self):
        # P1.8: the broker maps each upload descriptor to an in-container /workdir path +
        # metadata (never bytes), and forwards the resolved model's input modalities so the
        # agent can gate IMAGE translation. A malformed descriptor (no filename) is dropped.
        entry = SimpleNamespace(
            arn=None, options={}, input_modalities=["TEXT", "IMAGE"], provider="anthropic"
        )
        patches, spies = _patch_broker(entry=entry)
        # resolve_existing confines a descriptor to this session's workdir (rejects path
        # components / traversal, confirms the file exists). Stub it to the basename-only
        # validation so the test doesn't need real files on disk, but a traversal name is
        # still refused — proving the broker drops it before building an in-sandbox path.
        import os as _os

        def _fake_resolve(session_id, filename):
            if filename != _os.path.basename(filename) or filename in ("", ".", ".."):
                raise oia.attachments_svc.AttachmentError("Invalid attachment name")
            return f"/host/workdir/{filename}"

        patches.append(
            patch.object(oia.attachments_svc, "resolve_existing", side_effect=_fake_resolve)
        )
        lines = [json.dumps({"type": "done", "metadata": {}})]
        attachments = [
            {"filename": "diagram.png", "content_type": "image/png", "size": 1234},
            {"filename": "notes.txt", "content_type": "text/plain", "size": 7},
            {"content_type": "image/png"},  # no filename → dropped
            {
                "filename": "../../.aws/credentials",
                "content_type": "image/png",
            },  # traversal → dropped
        ]
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, lines, attachments=attachments)
        finally:
            for p in reversed(patches):
                p.stop()

        spec = json.loads(spies.turn_spec)
        assert spec["input_modalities"] == ["TEXT", "IMAGE"]
        assert spec["attachments"] == [
            {
                "path": "/workdir/diagram.png",
                "content_type": "image/png",
                "filename": "diagram.png",
                "size": 1234,
            },
            {
                "path": "/workdir/notes.txt",
                "content_type": "text/plain",
                "filename": "notes.txt",
                "size": 7,
            },
        ]
        # Attachment descriptors are persisted on the user row (history thumbnails).
        user_save = next(c for c in spies.save_message.await_args_list if c.args[2] == "user")
        assert user_save.kwargs["attachments"] == attachments

    @pytest.mark.asyncio
    async def test_skills_projected_to_turn_spec(self):
        # Broker advertises name+description ONLY (progressive disclosure) and materializes
        # the bodies into the sandbox via apply_session_skills (parity with claude-cli).
        skills = [
            SimpleNamespace(name="triage-pod", description="diagnose a crashlooping pod"),
            SimpleNamespace(name="audit-iam", description=None),  # null desc tolerated
        ]
        patches, spies = _patch_broker(skills=skills)
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, [json.dumps({"type": "done"})])
        finally:
            for p in reversed(patches):
                p.stop()
        spec = json.loads(spies.turn_spec)
        assert spec["skills"] == [
            {"name": "triage-pod", "description": "diagnose a crashlooping pod"},
            {"name": "audit-iam", "description": ""},
        ]
        assert spec["skills_root"] == "/home/agent/.claude/skills"
        # bodies materialized into THIS conversation's sandbox
        spies.apply_session_skills.assert_called_once_with(SESSION_ID, skills)

    @pytest.mark.asyncio
    async def test_mcp_config_projected_and_cleaned_up(self):
        # Broker writes the enabled MCP servers to a sandbox file and passes its in-container
        # path; the file (which may embed creds) is removed on turn end.
        patches, spies = _patch_broker(
            enabled_mcp=["coralogix-prod"],
            mcp_config_path="/home/agent/.claude/inkwest-agent-mcp.json",
        )
        for p in patches:
            p.start()
        try:
            await _run_broker(spies, [json.dumps({"type": "done"})])
        finally:
            for p in reversed(patches):
                p.stop()
        spec = json.loads(spies.turn_spec)
        assert spec["mcp_config_path"] == "/home/agent/.claude/inkwest-agent-mcp.json"
        spies.write_agent_mcp_config_with_oauth.assert_called_once_with(
            SESSION_ID, ["coralogix-prod"], user_id=USER_ID, db=spies.db
        )
        # prune-after-ensure + turn-end cleanup
        assert spies.remove_agent_mcp_config.call_count == 2

    @pytest.mark.asyncio
    async def test_mcp_dead_grant_emits_auth_required(self):
        # An enabled OAuth2 MCP server with no active grant surfaces as mcp_auth_required
        # on the inkwest-agent engine too (parity with claude-cli), so its tools don't
        # silently vanish from the agent's tool surface.
        patches, spies = _patch_broker(
            enabled_mcp=["notion"],
            mcp_config_path=None,
            mcp_dead=["notion"],
        )
        for p in patches:
            p.start()
        try:
            events = await _run_broker(spies, [json.dumps({"type": "done"})])
        finally:
            for p in reversed(patches):
                p.stop()
        auth_events = [e for e in events if e.type == "mcp_auth_required"]
        assert len(auth_events) == 1
        assert auth_events[0].metadata["mcp_key"] == "notion"

    @pytest.mark.asyncio
    async def test_skills_load_failure_degrades(self):
        # A skills/MCP DB hiccup must not crash the turn — degrade to none.
        patches, spies = _patch_broker()
        for p in patches:
            p.start()
        # Re-point get_effective_skills (already patched by the fixture) at a raising mock.
        oia.get_effective_skills.side_effect = RuntimeError("db down")
        try:
            events = await _run_broker(spies, [json.dumps({"type": "done"})])
        finally:
            for p in reversed(patches):
                p.stop()
        spec = json.loads(spies.turn_spec)
        assert spec["skills"] == []
        assert [e.type for e in events] == ["done"]

    async def _run_mismatch(self, raw: str):
        patches, spies = _patch_broker(history=[], entry=None)
        lines = [json.dumps({"type": "error", "content": raw, "is_error": True})]
        for p in patches:
            p.start()
        try:
            events = await _run_broker(spies, lines)
        finally:
            for p in reversed(patches):
                p.stop()
        errs = [e for e in events if e.type == "error"]
        assert len(errs) == 1
        return errs[0]

    @pytest.mark.asyncio
    async def test_unknown_field_error_rewritten_to_protocol_mismatch(self):
        """A pre-versioned (old) sandbox agent rejects a broker field with an opaque
        ``unknown field(s)`` error and no version stamp. The broker rewrites it to name
        the real cause (rebuild), the offending field(s), and both parties' protocols —
        the agent shown as pre-versioned — while stashing the raw text in metadata."""
        from inkwest_agent.protocol import PROTOCOL_VERSION

        raw = "Invalid turn-spec: turn-spec 'limits' has unknown field(s): bash_timeout_seconds"
        err = await self._run_mismatch(raw)
        assert "out of date" in (err.content or "")
        assert "Rebuild the sandbox image" in (err.content or "")
        # offending field surfaced in the header so the lag is diagnosable at a glance.
        assert "bash_timeout_seconds" in (err.content or "")
        # both parties' protocols shown; old image has no stamp -> pre-versioned.
        assert f"v{PROTOCOL_VERSION}" in (err.content or "")
        assert "pre-versioned" in (err.content or "")
        # raw rejection still preserved verbatim in metadata for full debugging context.
        assert err.metadata.get("original_error") == raw

    @pytest.mark.asyncio
    async def test_protocol_mismatch_shows_agent_version_when_stamped(self):
        """A version-stamped agent names its own protocol in the rejection; the broker
        parses it and shows both versions, with the field list cleanly separated from
        the stamp."""
        from inkwest_agent.protocol import PROTOCOL_VERSION

        raw = (
            "Invalid turn-spec: turn-spec 'limits' has unknown field(s): "
            "bash_timeout_seconds (agent protocol v3)"
        )
        err = await self._run_mismatch(raw)
        content = err.content or ""
        assert f"v{PROTOCOL_VERSION}" in content  # broker version
        assert "v3" in content  # parsed agent version
        assert "pre-versioned" not in content
        # the version stamp must not leak into the field list.
        assert "bash_timeout_seconds" in content
        assert "agent protocol" not in content.split("field(s)", 1)[1].split(".")[0]

    @pytest.mark.asyncio
    async def test_protocol_mismatch_roundtrips_real_rejection(self):
        """Contract guard: feed an ACTUAL ``TurnSpecError`` (produced by the agent's own
        ``parse_turn_spec``, wrapped exactly as ``__main__`` does) through the broker's
        rewrite. If either side's wording drifts out of sync, the rewrite stops firing and
        these asserts fail loudly — the cross-module string contract stays honest."""
        from inkwest_agent.protocol import PROTOCOL_VERSION, TurnSpecError, parse_turn_spec

        try:
            parse_turn_spec(
                {
                    "session_id": "s",
                    "user_message": "u",
                    "model": "m",
                    "system_prompt": "p",
                    "limits": {"future_knob": 1},
                }
            )
            raise AssertionError("parse_turn_spec should reject the unknown limits field")
        except TurnSpecError as e:
            raw = f"Invalid turn-spec: {e}"  # exactly how inkwest_agent.__main__ emits it

        err = await self._run_mismatch(raw)
        content = err.content or ""
        assert "out of date" in content
        assert "Rebuild the sandbox image" in content
        # The agent stamped its own (current) version, so both versions resolve to it.
        assert f"v{PROTOCOL_VERSION}" in content
        assert "pre-versioned" not in content
        # The sub-object location and the field both surface; the stamp doesn't leak in.
        assert "in 'limits'" in content
        assert "future_knob" in content
        assert "agent protocol" not in content.split("field(s)", 1)[1]
        assert err.metadata.get("original_error") == raw

    @pytest.mark.asyncio
    async def test_non_protocol_error_passes_through_untouched(self):
        """A normal agent error (not a turn-spec rejection) must NOT be rewritten —
        guards against the marker check over-matching."""
        raw = "AI service unavailable: throttled by Bedrock"
        err = await self._run_mismatch(raw)
        assert (err.content or "") == raw
        assert "out of date" not in (err.content or "")
        assert "original_error" not in (err.metadata or {})

    @pytest.mark.asyncio
    async def test_protocol_mismatch_rewritten_on_stderr_only_path(self):
        """A rejection that escapes via the agent's non-zero exit (stderr → RuntimeError)
        instead of a streamed error event is still rewritten — the broker must not leak the
        opaque message on the silent-crash path. received_terminal stays False here."""
        from inkwest_agent.protocol import PROTOCOL_VERSION

        patches, spies = _patch_broker(history=[], entry=None)
        for p in patches:
            p.start()

        def boom(session_id, turn_spec, **kw):
            raise RuntimeError(
                "inkwest-agent failed: Invalid turn-spec: turn-spec 'limits' has "
                "unknown field(s): bash_timeout_seconds (agent protocol v3)"
            )

        try:
            with patch.object(oia, "execute_agent", side_effect=boom):
                events = await _collect(
                    oia.orchestrate_chat_inkwest_agent(
                        db=spies.db,
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="why crash?",
                        user_id=USER_ID,
                    )
                )
        finally:
            for p in reversed(patches):
                p.stop()
        errs = [e for e in events if e.type == "error"]
        assert len(errs) == 1
        assert "out of date" in (errs[0].content or "")
        assert "v3" in (errs[0].content or "")
        assert f"v{PROTOCOL_VERSION}" in (errs[0].content or "")

    def test_rewrite_remedy_is_environment_specific(self):
        """Dev → "rebuild" (the operator builds the local image); prod → "redeploy", and
        the word "rebuild" must NOT appear (there is no manual rebuild in prod — the image
        ships via CI). The cause/version/field reporting is identical in both."""
        from app.services.events import error_event

        raw = "Invalid turn-spec: turn-spec 'limits' has unknown field(s): foo (agent protocol v3)"
        dev = oia._rewrite_protocol_mismatch(error_event(raw), "development")
        prod = oia._rewrite_protocol_mismatch(error_event(raw), "production")
        assert "Rebuild the sandbox image" in (dev.content or "")
        assert "Redeploy" in (prod.content or "")
        assert "ebuild" not in (prod.content or "")  # neither "Rebuild" nor "rebuild"
        # Same diagnosis on both paths.
        for ev in (dev, prod):
            assert "out of date" in (ev.content or "")
            assert "foo" in (ev.content or "")
            assert "v3" in (ev.content or "")

    def test_rewrite_handles_top_level_field_and_multiple_fields(self):
        """A top-level rejection has no quoted sub-object → no "in '...'" fragment; a
        comma-separated field list is preserved verbatim (not truncated at the first comma)."""
        from app.services.events import error_event

        raw = (
            "Invalid turn-spec: turn-spec top level has unknown field(s): alpha, beta "
            "(agent protocol v6)"
        )
        out = oia._rewrite_protocol_mismatch(error_event(raw), "development").content or ""
        assert "alpha, beta" in out
        assert "in 'top level'" not in out  # 'top level' is unquoted → no location fragment
        assert " in '" not in out

    def test_rewrite_empty_field_list_falls_back_and_none_content_passes_through(self):
        """Degenerate inputs: a marker with no field text degrades to "?" rather than
        raising; an error event with content=None is left untouched."""
        from app.services.events import ChatEvent, error_event

        raw = "turn-spec 'limits' has unknown field(s): (agent protocol v6)"
        out = oia._rewrite_protocol_mismatch(error_event(raw), "development").content or ""
        assert "out of date" in out
        assert ": ?." in out  # empty field list → "?" sentinel
        # None content: guarded by `or ""`, no crash, no rewrite.
        passthrough = oia._rewrite_protocol_mismatch(ChatEvent(type="error", content=None))
        assert passthrough.content is None

    @pytest.mark.asyncio
    async def test_mcp_config_cleaned_up_on_error_path(self):
        # Security: even if the turn blows up AFTER write_agent_mcp_config, the cred-bearing
        # config must still be removed (finally). Force execute_agent to raise unexpectedly.
        patches, spies = _patch_broker(
            enabled_mcp=["coralogix-prod"],
            mcp_config_path="/home/agent/.claude/inkwest-agent-mcp.json",
        )
        for p in patches:
            p.start()

        def boom(session_id, turn_spec, **kw):
            spies.turn_spec = turn_spec
            raise ValueError("unexpected explosion mid-turn")

        try:
            with patch.object(oia, "execute_agent", side_effect=boom):
                events = await _collect(
                    oia.orchestrate_chat_inkwest_agent(
                        db=spies.db,
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="why crash?",
                        user_id=USER_ID,
                    )
                )
        finally:
            for p in reversed(patches):
                p.stop()
        # The error surfaced to the client, and the cred file was still cleaned up.
        assert any(e.type == "error" for e in events)
        # prune-after-ensure + turn-end cleanup both ran (write succeeded before the boom).
        assert spies.remove_agent_mcp_config.call_count == 2
        spies.write_agent_mcp_config_with_oauth.assert_called_once_with(
            SESSION_ID, ["coralogix-prod"], user_id=USER_ID, db=spies.db
        )

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
    async def _dispatch(
        self,
        *,
        model,
        entry,
        agent_enabled,
        sandbox_enabled,
        pinned=None,
        default_engine=None,
        return_events=False,
    ):
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
            patch("app.services.ai_client.resolve_session_engine", AsyncMock(return_value=pinned)),
            # default_model() == model makes the session count as "on the default", so the
            # admin default engine applies when the session itself pins no engine.
            patch("app.services.ai_client.default_model", MagicMock(return_value=model)),
            patch("app.services.engines.configured_default_engine", return_value=default_engine),
            patch("app.services.model_registry.get_entry", MagicMock(return_value=entry)),
            patch("app.services.engines.inkwest_agent_enabled", return_value=agent_enabled),
            patch("app.services.orchestrator_cli.orchestrate_chat_cli", fake_cli),
            patch(
                "app.services.orchestrator_inkwest_agent.orchestrate_chat_inkwest_agent",
                fake_agent,
            ),
        ):
            gs.return_value.sandbox_enabled = sandbox_enabled
            orchestrator = get_orchestrator()
            events = [
                ev
                async for ev in orchestrator(
                    db=AsyncMock(), session_id=SESSION_ID, user_message="x"
                )
            ]
        if return_events:
            return seen.get("engine"), events
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
    async def test_claude_model_pinned_to_agent_routes_to_agent(self):
        # an explicit inkwest-agent pin sends a Claude model through the
        # sandboxed Converse loop instead of its default CLI path.
        entry = SimpleNamespace(provider="anthropic", arn=None, options={})
        assert (
            await self._dispatch(
                model="anthropic.claude-3-5-sonnet",
                entry=entry,
                agent_enabled=True,
                sandbox_enabled=True,
                pinned="inkwest-agent",
            )
            == "agent"
        )

    @pytest.mark.asyncio
    async def test_default_engine_sends_default_claude_model_to_agent(self):
        # the admin set the default model's engine to inkwest-agent; an
        # unpinned session on the default routes there even though it's a Claude model.
        entry = SimpleNamespace(provider="anthropic", arn=None, options={})
        assert (
            await self._dispatch(
                model="anthropic.claude-3-5-sonnet",
                entry=entry,
                agent_enabled=True,
                sandbox_enabled=True,
                pinned=None,
                default_engine="inkwest-agent",
            )
            == "agent"
        )

    @pytest.mark.asyncio
    async def test_non_claude_errors_when_agent_disabled(self):
        # A non-Claude model needs the agent; with the agent disabled it must NOT fall back
        # to the CLI (which can't serve it) — it surfaces a clean error and runs neither.
        entry = SimpleNamespace(provider="meta", arn=None, options={})
        engine, events = await self._dispatch(
            model="meta.llama3-1-70b-instruct-v1:0",
            entry=entry,
            agent_enabled=False,
            sandbox_enabled=True,
            return_events=True,
        )
        assert engine is None  # neither orchestrator ran
        assert [e for e in events if e.type == "error"], "expected a clean error event"
        err = next(e for e in events if e.type == "error")
        assert err.metadata.get("reason") == "engine_unavailable"

    @pytest.mark.asyncio
    async def test_non_claude_errors_when_sandbox_off(self):
        entry = SimpleNamespace(provider="meta", arn=None, options={})
        engine, events = await self._dispatch(
            model="meta.llama3-1-70b-instruct-v1:0",
            entry=entry,
            agent_enabled=True,
            sandbox_enabled=False,
            return_events=True,
        )
        assert engine is None
        assert any(
            e.type == "error" and e.metadata.get("reason") == "engine_unavailable" for e in events
        )


# ---------------------------------------------------------------------------
# LLM auth broker wiring (endpoint_url on the spec + grant injected + burned)
# ---------------------------------------------------------------------------


def test_make_bedrock_client_passes_endpoint_url() -> None:
    from inkwest_agent.converse import make_bedrock_client

    with patch("inkwest_agent.converse.boto3.client") as mk:
        make_bedrock_client("eu-west-1", endpoint_url="http://gw:8000/api/llm-proxy")
        kwargs = mk.call_args.kwargs
        assert kwargs["endpoint_url"] == "http://gw:8000/api/llm-proxy"
    with patch("inkwest_agent.converse.boto3.client") as mk:
        make_bedrock_client("eu-west-1")  # default = direct Bedrock, no override
        assert "endpoint_url" not in mk.call_args.kwargs


async def _run_agent_with_broker(broker_settings):
    """Run one broker turn with a custom execute_agent fake; capture spec + grant status."""
    captured: dict = {}

    def fake_execute_agent(session_id, turn_spec, **kw):
        captured["spec"] = json.loads(turn_spec)
        env = kw.get("extra_env") or {}
        tok = env.get("AWS_BEARER_TOKEN_BEDROCK")
        captured["token"] = tok
        captured["live_during_turn"] = bool(tok) and bool(token.verify(tok))
        return _aiter([json.dumps({"type": "done", "input_tokens": 1, "output_tokens": 1})])

    patches, spies = _patch_broker(broker_settings=broker_settings)
    for p in patches:
        p.start()
    try:
        with patch.object(oia, "execute_agent", side_effect=fake_execute_agent):
            await _collect(
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
        for p in patches:
            p.stop()
    return captured


@pytest.mark.asyncio
async def test_broker_on_sets_endpoint_injects_and_burns_grant() -> None:
    captured = await _run_agent_with_broker(
        SimpleNamespace(llm_broker_enabled=True, mcp_proxy_base_url="http://gw:8000")
    )
    assert captured["spec"]["bedrock_endpoint_url"] == "http://gw:8000/api/llm-proxy"
    assert captured["live_during_turn"] is True  # grant resolvable while the agent ran
    with pytest.raises(token.ProxyAuthError):
        token.verify(captured["token"])  # burned in the finally — no replay after the turn


@pytest.mark.asyncio
async def test_broker_off_leaves_endpoint_none_and_no_grant() -> None:
    captured = await _run_agent_with_broker(
        SimpleNamespace(llm_broker_enabled=False, mcp_proxy_base_url="http://gw:8000")
    )
    # Broker off ⇒ the unset field is OMITTED from the wire spec (forward-compat: an agent image
    # that predates bedrock_endpoint_url still parses the spec; parse_turn_spec re-applies None).
    assert "bedrock_endpoint_url" not in captured["spec"]
    assert captured["token"] is None  # no AWS_BEARER_TOKEN_BEDROCK injected


@pytest.mark.asyncio
async def test_grant_burned_when_agent_execute_raises() -> None:
    """Burn runs on the agent orchestrator's ERROR exit path too — a leaked grant is dead even
    when the turn blows up, not only on the clean stream."""
    captured: dict = {}

    def boom(session_id, turn_spec, **kw):
        env = kw.get("extra_env") or {}
        captured["token"] = env.get("AWS_BEARER_TOKEN_BEDROCK")
        assert captured["token"] and token.verify(captured["token"])  # live during the turn
        raise RuntimeError("agent crashed mid-turn")

    patches, spies = _patch_broker(
        broker_settings=SimpleNamespace(
            llm_broker_enabled=True, mcp_proxy_base_url="http://gw:8000"
        )
    )
    for p in patches:
        p.start()
    try:
        with patch.object(oia, "execute_agent", side_effect=boom):
            await _collect(
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
        for p in patches:
            p.stop()
    assert captured["token"]
    with pytest.raises(token.ProxyAuthError):  # burned in the finally despite the crash
        token.verify(captured["token"])


# --- P2 authority switch: broker-on skips the engine-side budget row ----------------------


@pytest.mark.asyncio
async def test_broker_on_skips_engine_usage_row_keeps_stats():
    patches, spies = _patch_broker(
        broker_settings=SimpleNamespace(
            llm_broker_enabled=True, mcp_proxy_base_url="http://gw:8000"
        )
    )
    lines = [json.dumps({"type": "done", "metadata": {"input_tokens": 5, "output_tokens": 3}})]
    for p in patches:
        p.start()
    try:
        await _run_broker(spies, lines)
    finally:
        for p in reversed(patches):
            p.stop()
    spies.update_stats.assert_awaited_once()
    spies.record_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_off_engine_usage_row_carries_turn_id():
    patches, spies = _patch_broker()
    lines = [json.dumps({"type": "done", "metadata": {"input_tokens": 5, "output_tokens": 3}})]
    for p in patches:
        p.start()
    try:
        await _run_broker(spies, lines)
    finally:
        for p in reversed(patches):
            p.stop()
    spies.update_stats.assert_awaited_once()
    spies.record_usage.assert_awaited_once()
    assert isinstance(spies.record_usage.await_args.kwargs["turn_id"], uuid.UUID)


@pytest.mark.asyncio
async def test_errored_turn_persists_accumulated_usage():
    """P3: an agent error event carries the usage burned before the failure — persist it."""
    patches, spies = _patch_broker()
    lines = [
        json.dumps(
            {
                "type": "error",
                "content": "AI service error: throttled",
                "is_error": True,
                "metadata": {
                    "input_tokens": 8,
                    "output_tokens": 4,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 12608,
                    "tool_calls": 1,
                    "iterations": 1,
                },
            }
        )
    ]
    for p in patches:
        p.start()
    try:
        events = await _run_broker(spies, lines)
    finally:
        for p in reversed(patches):
            p.stop()
    assert any(e.type == "error" for e in events)
    spies.update_stats.assert_awaited_once()
    spies.record_usage.assert_awaited_once()
    kwargs = spies.record_usage.await_args.kwargs
    assert kwargs["input_tokens"] == 8
    assert kwargs["cache_write_tokens"] == 12608
