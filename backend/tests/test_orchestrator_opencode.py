"""Tests for the opencode engine: spawn primitive, broker, and dispatch routing.

Mirrors test_orchestrator_inkwest_agent.py's harness for the sibling engine. The
JSONL→ChatEvent parser itself is covered separately (test_opencode_parser.py) against
live-captured fixtures; here we cover the spawn argv/resume/env, the broker's
resume/guard/cancel behavior, and that get_orchestrator routes to opencode.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import orchestrator_opencode as oo
from app.services.ai_cli_client import execute_opencode

SESSION_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


async def _collect(gen):
    return [e async for e in gen]


def _proc(lines: list[bytes], *, returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.stdin = None  # execute_opencode uses stdin=DEVNULL
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
# execute_opencode
# ---------------------------------------------------------------------------


class TestExecuteOpencode:
    @pytest.mark.asyncio
    async def test_argv_shape_and_model_prefix_first_turn(self):
        captured: dict = {}
        proc = _proc([b'{"type":"step_finish","part":{"reason":"stop"}}\n'])

        def factory(*cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["stdin"] = kwargs.get("stdin")
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            async for _ in execute_opencode(
                SESSION_ID,
                "investigate the outage",
                container="inkwest_conv_x",
                model="amazon.nova-micro-v1:0",
                resume=False,
            ):
                pass

        cmd = captured["cmd"]
        assert cmd[:4] == ["docker", "exec", "-i", "inkwest_conv_x"]
        assert "opencode" in cmd and "run" in cmd
        assert "--format" in cmd and "json" in cmd
        assert "amazon-bedrock/amazon.nova-micro-v1:0" in cmd  # provider prefix applied
        assert "--dangerously-skip-permissions" in cmd
        assert "--continue" not in cmd  # first turn → no resume
        assert cmd[-1] == "investigate the outage"  # prompt is the final argv element
        assert any(f"/tmp/inkwest-opencode-{SESSION_ID}.pid" in p for p in cmd)
        # prompt rides argv, not stdin
        import asyncio as _a

        assert captured["stdin"] == _a.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_resume_appends_continue(self):
        captured: dict = {}
        proc = _proc([b'{"type":"step_finish","part":{"reason":"stop"}}\n'])

        def factory(*cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            async for _ in execute_opencode(
                SESSION_ID, "next", container="c", model="m", resume=True
            ):
                pass
        assert "--continue" in captured["cmd"]

    @pytest.mark.asyncio
    async def test_extra_env_forwarded_as_value_less_e_flags(self):
        captured: dict = {}
        proc = _proc([b'{"type":"step_finish","part":{"reason":"stop"}}\n'])

        def factory(*cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["env"] = kwargs.get("env")
            return proc

        env = {"GITLAB_TOKEN": "glpat-xxx", "KUBECONFIG": "/x/kube"}
        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            async for _ in execute_opencode(
                SESSION_ID, "x", container="c", model="m", resume=False, extra_env=env
            ):
                pass

        cmd = captured["cmd"]
        assert "-e" in cmd and "GITLAB_TOKEN" in cmd and "KUBECONFIG" in cmd
        assert "glpat-xxx" not in cmd  # value stays out of argv
        assert captured["env"]["GITLAB_TOKEN"] == "glpat-xxx"

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_runtimeerror(self):
        proc = _proc([], returncode=1, stderr=b"AccessDenied")
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="opencode failed: AccessDenied"):
                async for _ in execute_opencode(
                    SESSION_ID, "x", container="c", model="m", resume=False
                ):
                    pass

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        proc = _proc([b'{"type":"text","part":{"text":"hi"}}\n'])
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", AsyncMock(side_effect=TimeoutError())),
        ):
            with pytest.raises(TimeoutError, match="timed out"):
                async for _ in execute_opencode(
                    SESSION_ID, "x", container="c", model="m", resume=False, timeout=1
                ):
                    pass


# ---------------------------------------------------------------------------
# orchestrate_chat_opencode (broker)
# ---------------------------------------------------------------------------


class TestOrchestrateOpencode:
    def _patch(self, *, history, entry=None, exec_lines=None, exec_factory=None, store_exists=True):
        """Patch the broker's DB/sandbox/exec deps. Returns the patch context + capture dict.

        ``store_exists`` controls the F1 resume guard: opencode.db presence in the durable dir.
        """
        cap: dict = {}

        async def fake_exec(session_id, user_message, **kwargs):
            cap["resume"] = kwargs.get("resume")
            cap["model"] = kwargs.get("model")
            cap["extra_env"] = kwargs.get("extra_env")
            for line in exec_lines or []:
                yield line

        from contextlib import ExitStack

        es = ExitStack()
        es.enter_context(patch.object(oo, "get_session_messages", AsyncMock(return_value=history)))
        es.enter_context(patch.object(oo, "get_effective_skills", AsyncMock(return_value=[])))
        es.enter_context(patch.object(oo, "get_enabled_mcp_servers", AsyncMock(return_value=[])))
        es.enter_context(patch.object(oo, "apply_session_skills", MagicMock()))
        es.enter_context(patch.object(oo, "save_message", AsyncMock()))
        es.enter_context(patch.object(oo, "ensure_session_title", AsyncMock()))
        es.enter_context(patch.object(oo, "update_session_stats", AsyncMock()))
        es.enter_context(patch.object(oo, "record_usage", AsyncMock()))
        es.enter_context(
            patch.object(oo, "resolve_session_system_prompt", AsyncMock(return_value="BASE"))
        )
        es.enter_context(patch.object(oo, "get_system_prompt", MagicMock(return_value="SYS")))
        es.enter_context(patch.object(oo, "resolve_session_model", AsyncMock(return_value="m")))
        es.enter_context(patch.object(oo, "execute_opencode", exec_factory or fake_exec))
        # model_registry.get_entry + sandbox.* are imported lazily inside the broker, so patch
        # the real module functions (not sys.modules — `from app.services import sandbox` binds
        # the already-imported submodule attribute, which a sys.modules swap wouldn't replace).
        es.enter_context(
            patch("app.services.model_registry.get_entry", MagicMock(return_value=entry))
        )
        es.enter_context(
            patch("app.services.sandbox.ensure", AsyncMock(return_value="inkwest_conv_x"))
        )
        es.enter_context(patch("app.services.sandbox.release", AsyncMock()))
        es.enter_context(patch("app.services.sandbox.teardown", AsyncMock()))
        es.enter_context(
            patch(
                "app.services.sandbox.session_dir", MagicMock(return_value="/tmp/does-not-matter")
            )
        )
        gs = es.enter_context(patch.object(oo, "get_settings"))
        gs.return_value.sandbox_enabled = True
        gs.return_value.sandbox_docker_path = "docker"
        gs.return_value.claude_cli_timeout = 120
        # AGENTS.md / opencode.json writes go to a temp workdir (no real FS): mock the
        # makedirs/open and the atomic-publish rename+remove so opencode.json staging
        # doesn't touch disk.
        es.enter_context(patch.object(oo.os, "makedirs", MagicMock()))
        es.enter_context(patch("builtins.open", MagicMock()))
        es.enter_context(patch.object(oo.os, "replace", MagicMock()))
        cap["os_remove"] = es.enter_context(patch.object(oo.os, "remove", MagicMock()))
        # F1 resume guard probes opencode.db presence in the durable dir.
        es.enter_context(patch.object(oo.os.path, "isfile", MagicMock(return_value=store_exists)))
        return es, cap

    @pytest.mark.asyncio
    async def test_sandbox_disabled_errors(self):
        with patch.object(oo, "get_settings") as gs:
            gs.return_value.sandbox_enabled = False
            with patch.object(oo, "get_session_messages", AsyncMock(return_value=[])):
                events = await _collect(
                    oo.orchestrate_chat_opencode(
                        db=AsyncMock(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                    )
                )
        assert any(e.type == "error" and "sandbox" in (e.content or "") for e in events)

    @pytest.mark.asyncio
    async def test_arn_pinned_model_rejected(self):
        es, _cap = self._patch(history=[], entry=SimpleNamespace(arn="arn:aws:bedrock:..."))
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    persist_user_message=False,
                )
            )
        assert any(e.type == "error" and "ARN-pinned" in (e.content or "") for e in events)

    @pytest.mark.asyncio
    async def test_resume_false_on_first_turn_true_with_history(self):
        # No prior history → fresh run (no --continue).
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                )
            )
        assert cap["resume"] is False

        # Prior history → resume.
        es2, cap2 = self._patch(
            history=[{"role": "user", "content": "earlier"}],
            exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n'],
        )
        with es2:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="follow-up",
                )
            )
        assert cap2["resume"] is True

    @pytest.mark.asyncio
    async def test_resume_guarded_when_store_missing(self):
        # F1: DB has history but opencode's own store is gone → don't pass --continue
        # (resuming an empty session would silently drop context).
        es, cap = self._patch(
            history=[{"role": "user", "content": "earlier"}],
            exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n'],
            store_exists=False,
        )
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="follow-up",
                )
            )
        assert cap["resume"] is False

    @pytest.mark.asyncio
    async def test_authoritative_cost_threaded_to_record_usage(self):
        # F8: opencode's per-turn USD cost overrides the token-estimate in record_usage.
        from decimal import Decimal

        line = (
            b'{"type":"step_finish","part":{"reason":"stop",'
            b'"tokens":{"input":10,"output":5},"cost":0.0123}}\n'
        )
        es, _cap = self._patch(history=[], exec_lines=[line])
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            # Assert inside the patch context — the AsyncMock is reverted on exit.
            oo.record_usage.assert_awaited_once()
            assert oo.record_usage.await_args.kwargs["cost_usd"] == Decimal("0.0123")

    @pytest.mark.asyncio
    async def test_llm_broker_wiring_when_enabled(self):
        # Broker on: opencode gets the proxy baseURL + the per-turn grant as the bearer env
        # (overriding the real key for its process), and the grant is burned at turn end.
        captured_cfg: dict = {}
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            with (
                patch.object(
                    oo.llm_broker,
                    "agent_bedrock_endpoint_url",
                    MagicMock(return_value="http://broker/api/llm-proxy"),
                ),
                patch.object(
                    oo.llm_broker,
                    "agent_broker_env",
                    MagicMock(return_value={"AWS_BEARER_TOKEN_BEDROCK": "jwt-x"}),
                ),
                patch.object(oo.llm_broker, "burn_env", MagicMock()) as burn,
                patch.object(oo.json, "dump", lambda cfg, f: captured_cfg.update(cfg)),
            ):
                await _collect(
                    oo.orchestrate_chat_opencode(
                        db=AsyncMock(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )
                burn.assert_called_once()  # grant revoked in the finally
        # Grant injected via the spawn env (docker exec -e overrides the real key).
        assert cap["extra_env"]["AWS_BEARER_TOKEN_BEDROCK"] == "jwt-x"
        # opencode.json points the amazon-bedrock provider at the broker base.
        assert (
            captured_cfg["provider"]["amazon-bedrock"]["options"]["baseURL"]
            == "http://broker/api/llm-proxy"
        )

    @pytest.mark.asyncio
    async def test_broker_off_uses_no_provider_override(self):
        # Broker off: no provider baseURL, no bearer override → direct Bedrock. Force the
        # broker funcs off explicitly (don't rely on the ambient LLM_BROKER_ENABLED default,
        # which is on in some envs and would invert this test's premise).
        captured_cfg: dict = {}
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with (
            es,
            patch.object(oo.llm_broker, "agent_bedrock_endpoint_url", MagicMock(return_value=None)),
            patch.object(oo.llm_broker, "agent_broker_env", MagicMock(return_value={})),
            patch.object(oo.json, "dump", lambda cfg, f: captured_cfg.update(cfg)),
        ):
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
        assert "provider" not in captured_cfg
        assert "AWS_BEARER_TOKEN_BEDROCK" not in (cap["extra_env"] or {})

    @pytest.mark.asyncio
    async def test_small_model_pinned_to_session_model_and_aux_agents_disabled(self):
        # opencode's small_model is pinned to the SESSION model (never the un-enabled
        # claude-haiku default the broker would 403), and the title/summary sub-agents — the
        # out-of-stream small-model spenders — are disabled so no unmetered cost is incurred.
        captured_cfg: dict = {}
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es, patch.object(oo.json, "dump", lambda cfg, f: captured_cfg.update(cfg)):
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
        assert captured_cfg["small_model"] == "amazon-bedrock/m"  # session model from _patch
        assert captured_cfg["agent"]["title"]["disable"] is True
        assert captured_cfg["agent"]["summary"]["disable"] is True

    @pytest.mark.asyncio
    async def test_cred_bearing_opencode_json_removed(self):
        # opencode.json can embed MCP creds/JWT headers → it must not outlive the turn.
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
        cred_path = "/tmp/does-not-matter/workdir/opencode.json"
        assert any(c.args and c.args[0] == cred_path for c in cap["os_remove"].call_args_list), (
            "opencode.json must be removed so creds don't outlive the turn"
        )

    @pytest.mark.asyncio
    async def test_expired_aws_creds_emit_connector_required(self):
        # F5: a FAILED bash result smelling of expired AWS creds surfaces "Give access".
        tool_line = (
            b'{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"c1",'
            b'"state":{"status":"completed","input":{"command":"aws s3 ls"},'
            b'"output":"An error occurred (ExpiredToken) when calling the ListBuckets operation",'
            b'"metadata":{"exit":255}}}}\n'
        )
        stop = b'{"type":"step_finish","part":{"reason":"stop"}}\n'
        es, _cap = self._patch(history=[], exec_lines=[tool_line, stop])
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=AsyncMock(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                )
            )
        assert any(e.type == "connector_required" for e in events)


def test_to_opencode_mcp_shape():
    servers = {
        "coralogix": {
            "type": "http",
            "url": "https://api.x/mcp",
            "headers": {"Authorization": "Bearer j"},
        },
        "local-tool": {"command": "mcp-tool", "args": ["--flag"], "env": {"K": "v"}},
        "bogus": {"nonsense": True},  # neither remote nor stdio → skipped
    }
    out = oo._to_opencode_mcp(servers)
    assert out["coralogix"] == {
        "type": "remote",
        "url": "https://api.x/mcp",
        "enabled": True,
        "headers": {"Authorization": "Bearer j"},
    }
    assert out["local-tool"] == {
        "type": "local",
        "command": ["mcp-tool", "--flag"],
        "enabled": True,
        "environment": {"K": "v"},
    }
    assert "bogus" not in out


class TestOpencodeMcp:
    @pytest.mark.asyncio
    async def test_dead_oauth_grant_emits_mcp_auth_required(self):
        es, _cap = TestOrchestrateOpencode()._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            # An enabled OAuth2 MCP server with no live grant → dead key.
            with (
                patch.object(oo, "get_enabled_mcp_servers", AsyncMock(return_value=["coralogix"])),
                patch.object(
                    oo, "_opencode_mcp_block", AsyncMock(return_value=({}, ["coralogix"]))
                ),
            ):
                events = await _collect(
                    oo.orchestrate_chat_opencode(
                        db=AsyncMock(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )
        assert any(e.type == "mcp_auth_required" for e in events)


def test_detect_expired_aws():
    assert oo._detect_expired_aws("... (ExpiredToken) ...", is_error=True) is True
    assert oo._detect_expired_aws("InvalidClientTokenId", is_error=True) is True
    # Not an error → no trigger (e.g. grepping logs that mention the string).
    assert oo._detect_expired_aws("found ExpiredToken in logs", is_error=False) is False
    # AccessDenied is a legitimate RO-role denial, NOT an expiry → no trigger.
    assert oo._detect_expired_aws("AccessDenied: not authorized", is_error=True) is False
    assert oo._detect_expired_aws(None, is_error=True) is False


# ---------------------------------------------------------------------------
# get_orchestrator dispatch → opencode
# ---------------------------------------------------------------------------


class TestOpencodeDispatch:
    async def _dispatch(self, *, opencode_enabled, sandbox_enabled):
        from app.services.ai_client import get_orchestrator

        seen = {}

        async def fake_cli(**kwargs):
            seen["engine"] = "cli"
            if False:
                yield

        async def fake_opencode(**kwargs):
            seen["engine"] = "opencode"
            if False:
                yield

        with (
            patch("app.services.ai_client.get_settings") as gs,
            patch(
                "app.services.ai_client.resolve_session_model",
                AsyncMock(return_value="anthropic.claude-sonnet-4-6"),
            ),
            patch(
                "app.services.ai_client.resolve_session_engine", AsyncMock(return_value="opencode")
            ),
            patch("app.services.engines.engine_for_turn", MagicMock(return_value="opencode")),
            patch("app.services.engines.opencode_enabled", return_value=opencode_enabled),
            patch("app.services.orchestrator_cli.orchestrate_chat_cli", fake_cli),
            patch("app.services.orchestrator_opencode.orchestrate_chat_opencode", fake_opencode),
        ):
            gs.return_value.ai_provider = "claude-cli"
            gs.return_value.sandbox_enabled = sandbox_enabled
            orchestrator = get_orchestrator()
            async for _ in orchestrator(db=AsyncMock(), session_id=SESSION_ID, user_message="x"):
                pass
        return seen.get("engine")

    @pytest.mark.asyncio
    async def test_routes_to_opencode_when_enabled(self):
        assert await self._dispatch(opencode_enabled=True, sandbox_enabled=True) == "opencode"

    @pytest.mark.asyncio
    async def test_falls_back_to_cli_when_opencode_disabled(self):
        assert await self._dispatch(opencode_enabled=False, sandbox_enabled=True) == "cli"

    @pytest.mark.asyncio
    async def test_falls_back_to_cli_when_sandbox_off(self):
        assert await self._dispatch(opencode_enabled=True, sandbox_enabled=False) == "cli"

    async def _dispatch_nonclaude(self, *, opencode_enabled, sandbox_enabled):
        """Dispatch a NON-Claude model pinned to opencode; return (engine_run, events).

        A non-Claude model can't run on the CLI, so when opencode is unavailable the
        dispatcher must surface a clean error and run NEITHER orchestrator — not silently
        fall back to the CLI (which would 403 / misattribute usage). capable_engines is
        forced to exclude claude-cli to model the non-Claude case.
        """
        from app.services.ai_client import get_orchestrator

        seen = {}
        events = []

        async def fake_cli(**kwargs):
            seen["engine"] = "cli"
            if False:
                yield

        async def fake_opencode(**kwargs):
            seen["engine"] = "opencode"
            if False:
                yield

        with (
            patch("app.services.ai_client.get_settings") as gs,
            patch(
                "app.services.ai_client.resolve_session_model",
                AsyncMock(return_value="meta.llama3-1-70b-instruct-v1:0"),
            ),
            patch(
                "app.services.ai_client.resolve_session_engine", AsyncMock(return_value="opencode")
            ),
            patch("app.services.engines.engine_for_turn", MagicMock(return_value="opencode")),
            patch("app.services.engines.opencode_enabled", return_value=opencode_enabled),
            patch("app.services.engines.inkwest_agent_enabled", return_value=False),
            patch("app.services.engines.capable_engines", MagicMock(return_value=["opencode"])),
            patch("app.services.orchestrator_cli.orchestrate_chat_cli", fake_cli),
            patch("app.services.orchestrator_opencode.orchestrate_chat_opencode", fake_opencode),
        ):
            gs.return_value.ai_provider = "claude-cli"
            gs.return_value.sandbox_enabled = sandbox_enabled
            orchestrator = get_orchestrator()
            async for ev in orchestrator(db=AsyncMock(), session_id=SESSION_ID, user_message="x"):
                events.append(ev)
        return seen.get("engine"), events

    @pytest.mark.asyncio
    async def test_nonclaude_errors_when_opencode_disabled(self):
        engine, events = await self._dispatch_nonclaude(
            opencode_enabled=False, sandbox_enabled=True
        )
        assert engine is None  # neither orchestrator ran — no silent CLI fallback
        err = next((e for e in events if e.type == "error"), None)
        assert err is not None, "expected a clean engine-unavailable error"
        assert err.metadata.get("reason") == "engine_unavailable"
        assert err.metadata.get("engine") == "opencode"

    @pytest.mark.asyncio
    async def test_nonclaude_errors_when_sandbox_off(self):
        engine, events = await self._dispatch_nonclaude(
            opencode_enabled=True, sandbox_enabled=False
        )
        assert engine is None
        err = next((e for e in events if e.type == "error"), None)
        assert err is not None, "expected a clean engine-unavailable error"
        assert err.metadata.get("reason") == "engine_unavailable"
        assert err.metadata.get("engine") == "opencode"
