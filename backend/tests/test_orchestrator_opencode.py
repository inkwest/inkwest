"""Tests for the opencode engine: spawn primitive, broker, and dispatch routing.

Mirrors test_orchestrator_inkwest_agent.py's harness for the sibling engine. The
JSONL→ChatEvent parser itself is covered separately (test_opencode_parser.py) against
live-captured fixtures; here we cover the spawn argv/resume/env, the broker's
resume/guard/cancel behavior, and that get_orchestrator routes to opencode.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import orchestrator_opencode as oo
from app.services import turn_usage as tu
from app.services.ai_cli_client import execute_opencode
from tests.conftest import mock_session

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
            # execute_opencode spawns twice: the opencode run, then a best-effort
            # `docker exec … rm -f <pidfile>` cleanup. Capture only the first (the run).
            if "cmd" not in captured:
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
        assert "--pure" in cmd  # no external plugins (workdir persistence vector)
        assert "--continue" not in cmd  # first turn → no resume
        assert cmd[-1] == "investigate the outage"  # prompt is the final argv element
        assert cmd[-2] == "--"  # flag terminator: a leading-dash prompt can't inject flags
        # pid file is per-TURN (uuid suffix) so a lingering prior-turn tail can't signal
        # this turn's process through a shared name.
        assert any(f"/tmp/inkwest-opencode-{SESSION_ID}-" in p and ".pid" in p for p in cmd)
        # prompt rides argv, not stdin
        import asyncio as _a

        assert captured["stdin"] == _a.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_resume_appends_continue(self):
        captured: dict = {}
        proc = _proc([b'{"type":"step_finish","part":{"reason":"stop"}}\n'])

        def factory(*cmd, **kwargs):
            if "cmd" not in captured:  # first spawn = the opencode run (2nd = pid rm)
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
            if "cmd" not in captured:  # first spawn = the opencode run (2nd = pid rm)
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

        # A plain async fn (not AsyncMock(side_effect=...)) that CLOSES the awaitable it
        # was handed — the mock variant leaks every call site's un-awaited coroutine
        # (RuntimeWarning) and silently short-circuits _stop_agent's helpers.
        async def _timeout(awaitable, timeout=None):
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch("asyncio.wait_for", _timeout),
        ):
            with pytest.raises(TimeoutError, match="timed out"):
                async for _ in execute_opencode(
                    SESSION_ID, "x", container="c", model="m", resume=False, timeout=1
                ):
                    pass

    @pytest.mark.asyncio
    async def test_inactivity_stall_times_out(self):
        # M2: opencode holds stdout open but emits nothing → the between-line inactivity
        # window (not just the post-EOF wait) must fire, kill the agent, and raise.
        import asyncio as _a

        from app.services import ai_cli_client as acc

        proc = MagicMock()
        proc.stdin = None

        async def _hang():
            await _a.sleep(5)  # never returns within the tiny inactivity window
            return b""

        proc.stdout = MagicMock()
        proc.stdout.readline = _hang
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch.object(acc, "_stop_agent", AsyncMock()) as stop,
        ):
            with pytest.raises(TimeoutError, match="inactivity"):
                async for _ in execute_opencode(
                    SESSION_ID, "x", container="c", model="m", resume=False, timeout=0.05
                ):
                    pass
            stop.assert_awaited()  # the stalled agent was killed


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
            cap["message"] = user_message
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
        es.enter_context(patch("app.services.turn_usage.update_session_stats", AsyncMock()))
        es.enter_context(patch("app.services.turn_usage.record_usage", AsyncMock()))
        es.enter_context(
            patch.object(oo, "resolve_session_system_prompt", AsyncMock(return_value="BASE"))
        )
        es.enter_context(patch.object(oo, "get_system_prompt", MagicMock(return_value="SYS")))
        es.enter_context(patch.object(oo, "resolve_session_model", AsyncMock(return_value="m")))
        # Pin the LLM broker OFF by default so these tests are hermetic against the ambient
        # LLM_BROKER_ENABLED (on in some dev .envs). With P2's authority switch, a live
        # broker env would silently skip record_usage and invert the accounting asserts.
        # Broker-specific tests re-patch these inside their own `with` (inner patch wins).
        es.enter_context(
            patch.object(oo.llm_broker, "agent_broker_env", MagicMock(return_value={}))
        )
        es.enter_context(
            patch.object(oo.llm_broker, "agent_bedrock_endpoint_url", MagicMock(return_value=None))
        )
        es.enter_context(patch.object(oo, "execute_opencode", exec_factory or fake_exec))
        # model_registry.get_entry + sandbox.* are imported lazily inside the broker, so patch
        # the real module functions (not sys.modules — `from app.services import sandbox` binds
        # the already-imported submodule attribute, which a sys.modules swap wouldn't replace).
        es.enter_context(
            patch("app.services.model_registry.get_entry", MagicMock(return_value=entry))
        )
        # Broker fail-closed pre-check consults enabled registry rows; default to one enabled
        # so broker-on tests don't trip the "no models enabled" config error.
        es.enter_context(
            patch(
                "app.services.model_registry.merged_entries",
                MagicMock(return_value=[SimpleNamespace(enabled=True)]),
            )
        )
        # EKS-context surfacing reads the session's generated kubeconfig — not under test
        # here (and builtins.open is mocked below, which would break its YAML read).
        es.enter_context(
            patch("app.tools.connector_exec.session_kube_contexts", MagicMock(return_value=[]))
        )
        cap["rmtree"] = es.enter_context(patch.object(oo.shutil, "rmtree", MagicMock()))
        # Admission control is real code with in-process state — stub it so these tests
        # don't depend on the empty-registry accident (its raising branch has its own test).
        es.enter_context(patch("app.services.sandbox.enforce_user_sandbox_cap", AsyncMock()))
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
        # The boundary sidecar: current unless a test says otherwise.
        cap["stale"] = es.enter_context(
            patch("app.services.sandbox.opencode_store_is_stale", MagicMock(return_value=False))
        )
        cap["reset"] = es.enter_context(patch("app.services.sandbox.reset_opencode_store"))
        cap["record"] = es.enter_context(patch("app.services.sandbox.record_opencode_boundary"))
        return es, cap

    @pytest.mark.asyncio
    async def test_compaction_summary_reaches_the_published_prompt(self):
        """A compacted session's summary must ride the AGENTS.md prompt on every later turn."""
        published: dict = {}
        es, cap = self._patch(history=[{"role": "user", "content": "after boundary"}])
        with es:
            with (
                patch(
                    "app.services.orchestrator_opencode.last_boundary",
                    AsyncMock(
                        return_value=SimpleNamespace(
                            id=uuid.uuid4(),
                            role="system",
                            content={
                                "type": "session_event",
                                "subject": "context",
                                "action": "compacted",
                                "targets": [{"archive_path": "/workdir/.inkwest/history-1.md"}],
                                "summary": "FACT: the pod was OOMKilled",
                            },
                        )
                    ),
                ),
                patch.object(
                    oo,
                    "_publish_workdir_file",
                    lambda wd, name, content: published.update({name: content}),
                ),
            ):
                await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )

        prompt = published["AGENTS.md"]
        assert "Earlier conversation (compacted)" in prompt
        assert "FACT: the pod was OOMKilled" in prompt

    @pytest.mark.asyncio
    async def test_sandbox_disabled_errors(self):
        with patch.object(oo, "get_settings") as gs:
            gs.return_value.sandbox_enabled = False
            with patch.object(oo, "get_session_messages", AsyncMock(return_value=[])):
                events = await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
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
                    db=mock_session(),
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
                    db=mock_session(),
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
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="follow-up",
                )
            )
        assert cap2["resume"] is True

    @pytest.mark.asyncio
    async def test_read_txn_released_before_stream(self):
        """The last DB call before sandbox.ensure and before the stream is a commit."""
        db = mock_session()
        last_db_call: dict[str, str | None] = {}

        def snapshot(step: str) -> None:
            last_db_call[step] = db.method_calls[-1][0] if db.method_calls else None

        async def fake_exec(session_id, user_message, **kwargs):
            snapshot("exec")
            yield b'{"type":"step_finish","part":{"reason":"stop"}}\n'

        es, _cap = self._patch(history=[], exec_factory=fake_exec)
        with es:
            from app.services import sandbox

            sandbox.ensure.side_effect = lambda *a, **kw: snapshot("ensure") or "inkwest_conv_x"

            async def mcp_block(*a, db, **kw):  # reads the DB after ensure, like the real one
                await db.execute("SELECT 1")
                return {}, []

            es.enter_context(patch.object(oo, "_opencode_mcp_block", side_effect=mcp_block))
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=db,
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                )
            )

        assert last_db_call == {"ensure": "commit", "exec": "commit"}

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
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="follow-up",
                )
            )
        assert cap["resume"] is False

    async def _turn_under_boundary(
        self, *, stale: bool, exec_lines: list[bytes] | None = None, **turn_kwargs
    ):
        es, cap = self._patch(
            history=[{"role": "user", "content": "a turn another engine ran"}],
            exec_lines=exec_lines
            if exec_lines is not None
            else [b'{"type":"step_finish","part":{"reason":"stop"}}\n'],
        )
        boundary = SimpleNamespace(
            id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
            role="system",
            content={
                "type": "session_event",
                "subject": "context",
                "action": "compacted",
                "summary": "FACT",
            },
        )
        with es:
            cap["stale"].return_value = stale
            fetch = AsyncMock(return_value=boundary)
            # Both names: a second lookup through the compaction module counts too.
            with (
                patch.object(oo, "last_boundary", fetch),
                patch("app.services.compaction.last_boundary", fetch),
            ):
                cap["events"] = await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="follow-up",
                        **turn_kwargs,
                    )
                )
        cap["fetch"] = fetch
        return cap, str(boundary.id)

    @pytest.mark.asyncio
    async def test_store_from_before_the_boundary_is_reset_and_never_resumed(self):
        """A failed post-compaction reset leaves the pre-boundary session newest in the store;
        a later turn must not `--continue` it, whichever engine ran in between."""
        cap, boundary_id = await self._turn_under_boundary(stale=True)
        cap["stale"].assert_called_once_with(SESSION_ID, boundary_id)
        cap["reset"].assert_called_once_with(SESSION_ID)
        assert cap["resume"] is False

    @pytest.mark.asyncio
    async def test_compaction_on_a_stale_store_never_spawns(self):
        """The store holds no post-boundary turn: a fresh run would summarize nothing."""
        from app.services import compaction

        flag = compaction._compacting.set(True)
        try:
            cap, _ = await self._turn_under_boundary(stale=True, persist_user_message=False)
        finally:
            compaction._compacting.reset(flag)
        assert cap["events"] == []
        assert "message" not in cap  # execute_opencode never ran

    @pytest.mark.asyncio
    async def test_sidecar_failures_are_logged_with_their_session(self):
        es, cap = self._patch(
            history=[{"role": "user", "content": "x"}],
            exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n'],
        )
        with es, patch.object(oo, "logger") as log:
            cap["stale"].return_value = True
            cap["reset"].side_effect = OSError("opencode.db survived")
            cap["record"].side_effect = OSError("disk full")
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="follow-up",
                )
            )
        logged = {c.args[0]: c.kwargs.get("extra") for c in log.exception.call_args_list}
        assert logged["opencode_store_reset_retry_failed"] == {"session_id": SESSION_ID}
        assert logged["opencode_boundary_record_failed"] == {"session_id": SESSION_ID}

    @pytest.mark.asyncio
    async def test_the_boundary_is_looked_up_once_per_turn(self):
        cap, _ = await self._turn_under_boundary(stale=False)
        assert cap["fetch"].await_count == 1

    @pytest.mark.asyncio
    async def test_store_written_under_the_boundary_keeps_continue(self):
        cap, _ = await self._turn_under_boundary(stale=False)
        cap["reset"].assert_not_called()
        assert cap["resume"] is True

    @pytest.mark.asyncio
    async def test_a_spawned_run_records_its_boundary(self):
        cap, boundary_id = await self._turn_under_boundary(stale=True)
        cap["record"].assert_called_once_with(SESSION_ID, boundary_id)

    @pytest.mark.asyncio
    async def test_a_run_that_never_produced_output_records_nothing(self):
        """No output, no new session: recording the boundary would vouch for the old one."""
        cap, _ = await self._turn_under_boundary(stale=True, exec_lines=[])
        cap["record"].assert_not_called()

    @pytest.mark.asyncio
    async def test_compaction_with_a_missing_store_never_spawns(self):
        """A fresh run has no transcript to summarize: spawning it bills a summary that can
        only be discarded."""
        from app.services import compaction

        es, cap = self._patch(
            history=[{"role": "user", "content": "earlier"}],
            exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n'],
            store_exists=False,
        )
        flag = compaction._compacting.set(True)
        try:
            with es:
                record_usage = es.enter_context(
                    patch("app.services.turn_usage.record_usage", AsyncMock())
                )
                events = await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="summarize",
                        persist_user_message=False,
                    )
                )
        finally:
            compaction._compacting.reset(flag)
        assert events == []
        assert "message" not in cap  # execute_opencode never ran
        record_usage.assert_not_awaited()

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
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            # Assert inside the patch context — the AsyncMock is reverted on exit.
            tu.record_usage.assert_awaited_once()
            assert tu.record_usage.await_args.kwargs["cost_usd"] == Decimal("0.0123")

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
                patch.object(
                    oo,
                    "_publish_workdir_file",
                    lambda wd, name, content: captured_cfg.update(
                        json.loads(content) if name == "opencode.json" else {}
                    ),
                ),
            ):
                await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
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
            patch.object(
                oo,
                "_publish_workdir_file",
                lambda wd, name, content: captured_cfg.update(
                    json.loads(content) if name == "opencode.json" else {}
                ),
            ),
        ):
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
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
        with (
            es,
            patch.object(
                oo,
                "_publish_workdir_file",
                lambda wd, name, content: captured_cfg.update(
                    json.loads(content) if name == "opencode.json" else {}
                ),
            ),
        ):
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
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
        # L1: defense-in-depth flags must be present — dropping any of these silently widens
        # the sandbox (egress tools) or defeats the version pin (autoupdate).
        assert captured_cfg["autoupdate"] is False
        assert captured_cfg["tools"] == {"websearch": False, "webfetch": False, "skill": False}

    @pytest.mark.asyncio
    async def test_cred_bearing_opencode_json_removed(self):
        # opencode.json can embed MCP creds/JWT headers → it must not outlive the turn.
        # Two removals of the same path are expected: the pre-turn stale prune AND the
        # finally cleanup — asserting ≥2 keeps this from passing on the prune alone.
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
        cred_path = "/tmp/does-not-matter/workdir/opencode.json"
        removals = [c for c in cap["os_remove"].call_args_list if c.args and c.args[0] == cred_path]
        assert len(removals) >= 2, (
            "opencode.json must be removed both at turn start (stale prune) and in the "
            "finally (creds must not outlive the turn)"
        )

    @pytest.mark.asyncio
    async def test_cancel_persists_partial_and_tears_down(self):
        # Cancel mid-stream: PERSIST the partial blocks (the client already saw them streamed;
        # any dangling tool_use is pruned on the history read path), emit a single stopped
        # done, and TEAR DOWN the container (reap opencode's children).
        import asyncio

        cancel = asyncio.Event()

        async def exec_factory(session_id, user_message, **kwargs):
            yield b'{"type":"text","part":{"text":"partial"}}\n'
            yield (
                b'{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"c1",'
                b'"state":{"status":"completed","input":{},"output":"o","metadata":{"exit":0}}}}\n'
            )
            cancel.set()
            # Kill artifact: the parser closes a cut stream with a truncated done.

        es, _cap = self._patch(history=[], exec_factory=exec_factory)
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                    cancel_event=cancel,
                )
            )
            from app.services import sandbox

            # Our lease is always dropped first; abnormal then tears down (idle-guarded).
            sandbox.release.assert_awaited_once()
            sandbox.teardown.assert_awaited_once()  # abnormal → reap children
            assert sandbox.teardown.await_args.kwargs.get("only_if_idle") is True
            # user turn + the flushed partial (assistant blocks, then its tool_result pair)
            roles = [c.args[2] for c in oo.save_message.await_args_list]
            assert roles[0] == "user"
            assert "assistant" in roles[1:]
        dones = [e for e in events if e.type == "done"]
        assert len(dones) == 1 and dones[0].metadata.get("stopped") is True
        assert not any(e.type == "done" and e.metadata.get("truncated") for e in events)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [RuntimeError, TimeoutError])
    async def test_silent_crash_surfaces_error_burns_grant_and_tears_down(self, exc):
        # Nonzero exit / timeout with nothing terminal: error to the user, grant burned,
        # container torn down (not released warm).
        async def exec_factory(session_id, user_message, **kwargs):
            yield b'{"type":"text","part":{"text":"partial"}}\n'
            raise exc("opencode failed: squid blocked")

        es, _cap = self._patch(history=[], exec_factory=exec_factory)
        with es:
            with (
                patch.object(
                    oo.llm_broker,
                    "agent_broker_env",
                    MagicMock(return_value={"AWS_BEARER_TOKEN_BEDROCK": "jwt-x"}),
                ),
                patch.object(
                    oo.llm_broker, "agent_bedrock_endpoint_url", MagicMock(return_value="http://b")
                ),
                patch.object(oo.llm_broker, "burn_env", MagicMock()) as burn,
            ):
                events = await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )
                burn.assert_called_once()  # grant dead even on crash
            from app.services import sandbox

            sandbox.release.assert_awaited_once()
            sandbox.teardown.assert_awaited_once()
        assert any(e.type == "error" and "squid blocked" in (e.content or "") for e in events)

    @pytest.mark.asyncio
    async def test_silent_crash_persists_partial(self):
        """A crashed turn keeps what already streamed — same contract as a user Stop."""

        async def exec_factory(session_id, user_message, **kwargs):
            yield b'{"type":"text","part":{"text":"partial"}}\n'
            raise RuntimeError("opencode failed: squid blocked")

        es, _cap = self._patch(history=[], exec_factory=exec_factory)
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            calls = [c for c in oo.save_message.await_args_list if c.args[2] == "assistant"]
            assert len(calls) == 1
            assert calls[0].args[3] == [{"type": "text", "text": "partial"}]
        assert any(e.type == "error" for e in events)

    @pytest.mark.asyncio
    async def test_error_event_records_usage_and_tears_down(self):
        # A parser `error` (e.g. Bedrock 403) is terminal: the container is torn down
        # (opencode's state is unknown) and the usage accumulated up to the failure —
        # carried on the error's metadata — is still recorded.
        lines = [
            b'{"type":"step_finish","part":{"reason":"tool-calls",'
            b'"tokens":{"input":10,"output":5},"cost":0.01}}\n',
            b'{"type":"error","error":{"name":"APIError","data":{"message":"Forbidden"}}}\n',
        ]
        es, _cap = self._patch(history=[], exec_lines=lines)
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            from decimal import Decimal

            from app.services import sandbox

            sandbox.release.assert_awaited_once()
            sandbox.teardown.assert_awaited_once()
            tu.update_session_stats.assert_awaited_once()
            assert tu.update_session_stats.await_args.kwargs["input_tokens"] == 10
            assert tu.update_session_stats.await_args.kwargs["output_tokens"] == 5
            tu.record_usage.assert_awaited_once()
            # cost accumulated before the error rides its metadata → authoritative override
            assert tu.record_usage.await_args.kwargs["cost_usd"] == Decimal("0.01")
        assert any(e.type == "error" and "Forbidden" in (e.content or "") for e in events)
        # The parser suppresses its trailing truncated done after an error — one terminal.
        assert not any(e.type == "done" for e in events)

    @pytest.mark.asyncio
    async def test_stop_then_nonzero_exit_keeps_result_and_usage(self):
        # A genuine stop followed by a nonzero process exit: the user already has the
        # outcome — no second terminal error — but usage is still recorded and the
        # container is torn down (abnormal exit).
        async def exec_factory(session_id, user_message, **kwargs):
            yield b'{"type":"text","part":{"text":"answer"}}\n'
            yield (
                b'{"type":"step_finish","part":{"reason":"stop",'
                b'"tokens":{"input":10,"output":5},"cost":0.01}}\n'
            )
            raise RuntimeError("opencode failed: Exit code 1")

        es, _cap = self._patch(history=[], exec_factory=exec_factory)
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            from app.services import sandbox

            sandbox.release.assert_awaited_once()
            sandbox.teardown.assert_awaited_once()
            tu.update_session_stats.assert_awaited_once()
            # The assistant text that streamed before the crash is persisted.
            saved_roles = [c.args[2] for c in oo.save_message.await_args_list]
            assert "assistant" in saved_roles
        assert not any(e.type == "error" for e in events)
        assert any(e.type == "done" for e in events)

    @pytest.mark.asyncio
    async def test_assistant_and_tool_results_persisted_with_roles(self):
        # The _flush state machine: assistant text+tool_use ride an assistant message,
        # tool results ride a separate tool_result message, in order.
        lines = [
            b'{"type":"text","part":{"text":"looking"}}\n',
            b'{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"c1",'
            b'"state":{"status":"completed","input":{"command":"ls"},"output":"a",'
            b'"metadata":{"exit":0}}}}\n',
            b'{"type":"text","part":{"text":"done"}}\n',
            b'{"type":"step_finish","part":{"reason":"stop","tokens":{"input":1,"output":1}}}\n',
        ]
        es, _cap = self._patch(history=[], exec_lines=lines)
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            calls = oo.save_message.await_args_list
            roles = [c.args[2] for c in calls]
            assert roles == ["user", "assistant", "tool_result", "assistant"]
            first_assistant = calls[1].args[3]
            assert first_assistant[0] == {"type": "text", "text": "looking"}
            assert first_assistant[1]["type"] == "tool_use"
            assert first_assistant[1]["id"] == "c1"
            tool_results = calls[2].args[3]
            assert tool_results[0]["tool_use_id"] == "c1"
            assert calls[3].args[3] == [{"type": "text", "text": "done"}]
            # user turn carries author attribution (shared conversations)
            assert calls[0].kwargs["author_user_id"] == USER_ID

    @pytest.mark.asyncio
    async def test_expired_aws_reauth_emitted_once_per_turn(self):
        # An agent retrying `aws` on expired creds must not spam "Give access".
        tool = (
            b'{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"%s",'
            b'"state":{"status":"completed","input":{"command":"aws s3 ls"},'
            b'"output":"An error occurred (ExpiredToken)","metadata":{"exit":255}}}}\n'
        )
        lines = [tool % b"c1", tool % b"c2", b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        es, _cap = self._patch(history=[], exec_lines=lines)
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                )
            )
        assert sum(1 for e in events if e.type == "connector_required") == 1

    @pytest.mark.asyncio
    async def test_sandbox_cap_exceeded_errors_cleanly(self):
        from app.services import sandbox as sandbox_mod

        es, _cap = self._patch(history=[])
        with es:
            with patch(
                "app.services.sandbox.enforce_user_sandbox_cap",
                AsyncMock(side_effect=sandbox_mod.SandboxCapExceededError(active=3, hard_cap=3)),
            ):
                events = await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )
            from app.services import sandbox

            sandbox.ensure.assert_not_awaited()  # refused before provisioning
        assert any(
            e.type == "error" and "Too many conversations" in (e.content or "") for e in events
        )

    @pytest.mark.asyncio
    async def test_attachments_only_turn_synthesizes_prompt(self):
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="   ",
                    user_id=USER_ID,
                    attachments=[{"filename": "report.csv"}],
                )
            )
        assert cap["message"] == "Please review the attached file(s): /workdir/report.csv"

    @pytest.mark.asyncio
    async def test_attachments_with_message_appends_filenames(self):
        # "summarize the attached file" must reach opencode WITH the /workdir path —
        # parity with the cli's @/workdir refs / the agent's AttachmentSpec.
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="summarize the attached file",
                    user_id=USER_ID,
                    attachments=[{"filename": "report.csv"}],
                )
            )
        assert cap["message"].startswith("summarize the attached file")
        assert "/workdir/report.csv" in cap["message"]

    @pytest.mark.asyncio
    async def test_eks_contexts_surface_in_prompt_and_cluster_label(self):
        # Discovered EKS contexts rewrite the cluster label and land in AGENTS.md so the
        # model doesn't claim it has no Kubernetes access.
        writes: list[str] = []
        open_mock = MagicMock()
        open_mock.return_value.__enter__.return_value.write = lambda s: writes.append(s)
        es, _cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            with (
                patch(
                    "app.tools.connector_exec.session_kube_contexts",
                    MagicMock(return_value=["ctx-a", "ctx-b"]),
                ),
                patch("builtins.open", open_mock),
            ):
                await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )
            oo.get_system_prompt.assert_called_once()
            assert oo.get_system_prompt.call_args.args[0] == "EKS via AWS access (2 cluster(s))"
        agents_md = "".join(writes)
        assert "## Kubernetes (EKS) access" in agents_md
        assert "ctx-a" in agents_md and "ctx-b" in agents_md
        assert "kubectl --context" in agents_md

    @pytest.mark.asyncio
    async def test_connector_hint_lands_in_prompt(self):
        # Injected connector creds must be ANNOUNCED to the model (env alone is invisible).
        writes: list[str] = []
        open_mock = MagicMock()
        open_mock.return_value.__enter__.return_value.write = lambda s: writes.append(s)
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            with (
                patch(
                    "app.tools.connector_exec.build_connector_env",
                    AsyncMock(return_value=({"GITLAB_TOKEN": "glpat-x"}, ["gitlab"])),
                ),
                patch("builtins.open", open_mock),
            ):
                await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )
        agents_md = "".join(writes)
        assert "External access is available" in agents_md
        assert cap["extra_env"]["GITLAB_TOKEN"] == "glpat-x"

    @pytest.mark.asyncio
    async def test_dot_opencode_pruned_at_turn_start(self):
        es, cap = self._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
        cap["rmtree"].assert_called_once_with(
            "/tmp/does-not-matter/workdir/.opencode", ignore_errors=True
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw_cost", ["0", "-0.5", '"broken"'])
    async def test_untrustworthy_cost_falls_back_to_estimate(self, raw_cost):
        # Zero (= models.dev has no pricing), negative, or malformed cost must NOT be
        # recorded verbatim — cost_usd passes as None so record_usage's own CACHE-AWARE
        # token×rate estimate applies (pinned in test_budget_service/test_pricing).
        line = (
            b'{"type":"step_finish","part":{"reason":"stop",'
            b'"tokens":{"input":10,"output":5},"cost":' + raw_cost.encode() + b"}}\n"
        )
        es, _cap = self._patch(history=[], exec_lines=[line])
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            tu.record_usage.assert_awaited_once()
            # The bogus value never reaches the row; None delegates to the estimator.
            assert tu.record_usage.await_args.kwargs["cost_usd"] is None

    @pytest.mark.asyncio
    async def test_cost_fallback_prices_cache_tokens_at_cache_rates(self):
        # Cache write/read tokens must NOT be billed at the full input rate — stats see
        # fresh input, the splits ride their own kwargs, and cost_usd=None delegates to
        # record_usage's cache-aware estimator (rates pinned in test_pricing).
        line = (
            b'{"type":"step_finish","part":{"reason":"stop",'
            b'"tokens":{"input":10,"output":5,"cache":{"write":8000,"read":4000}}}}\n'
        )
        es, _cap = self._patch(history=[], exec_lines=[line])
        with es:
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            # stats see FRESH input (cache rides its own counters — no double count);
            # cost prices fresh + cache splits at their real rates.
            assert tu.update_session_stats.await_args.kwargs["input_tokens"] == 10
            assert tu.update_session_stats.await_args.kwargs["cache_read_tokens"] == 4000
            assert tu.update_session_stats.await_args.kwargs["cache_write_tokens"] == 8000
            assert tu.record_usage.await_args.kwargs["cost_usd"] is None
            # ...and the raw splits land in the usage row's cache columns.
            assert tu.record_usage.await_args.kwargs["cache_read_tokens"] == 4000
            assert tu.record_usage.await_args.kwargs["cache_write_tokens"] == 8000

    @pytest.mark.asyncio
    async def test_broker_on_with_no_enabled_models_errors_cleanly(self):
        # Fail-closed broker + empty registry would 403 mid-turn — surface a config error.
        es, _cap = self._patch(history=[])
        with es:
            with (
                patch.object(
                    oo.llm_broker,
                    "agent_broker_env",
                    MagicMock(return_value={"AWS_BEARER_TOKEN_BEDROCK": "jwt-x"}),
                ),
                patch(
                    "app.services.model_registry.merged_entries",
                    MagicMock(return_value=[SimpleNamespace(enabled=False)]),
                ),
            ):
                events = await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )
        assert any(
            e.type == "error" and "No models are enabled" in (e.content or "") for e in events
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
                    db=mock_session(),
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
    async def test_static_servers_are_brokered(self):
        # Static catalog servers must go through build_session_static_servers (proxy URL +
        # internal JWT — real headers never enter the sandbox), same as the sibling engines.
        with (
            patch(
                "app.services.mcp_config.build_session_static_servers",
                MagicMock(return_value={"coralogix": {"url": "http://proxy/x"}}),
            ) as build_static,
            patch(
                "app.services.mcp_config.oauth_proxy_servers_for",
                AsyncMock(return_value=({}, [])),
            ),
            patch.object(oo, "get_settings") as gs,
        ):
            gs.return_value.mcp_proxy_base_url = "http://backend:8000"
            mcp, dead = await oo._opencode_mcp_block(
                SESSION_ID, ["coralogix"], user_id=USER_ID, db=AsyncMock()
            )
        assert dead == []
        assert mcp["coralogix"]["type"] == "remote"
        kwargs = build_static.call_args.kwargs
        assert kwargs["base"] == "http://backend:8000"
        assert kwargs["user_id"] == USER_ID
        assert kwargs["session_id"] == SESSION_ID

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
                        db=mock_session(),
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
            async for _ in orchestrator(db=mock_session(), session_id=SESSION_ID, user_message="x"):
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
            async for ev in orchestrator(
                db=mock_session(), session_id=SESSION_ID, user_message="x"
            ):
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


# ---------------------------------------------------------------------------
# _publish_workdir_file — symlink-safe write (C1)
# ---------------------------------------------------------------------------


def test_publish_workdir_file_replaces_planted_symlink(tmp_path):
    # The workdir is sandbox-writable: a prior turn can plant `AGENTS.md -> <host file>`.
    # The publish must NOT follow the link (no write to its target); it replaces the link
    # with a real file, and cleans up its staging sibling.
    workdir = tmp_path / "sid" / "workdir"
    workdir.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("DO-NOT-TOUCH")
    (workdir / "AGENTS.md").symlink_to(victim)

    oo._publish_workdir_file(str(workdir), "AGENTS.md", "fresh content\n")

    assert victim.read_text() == "DO-NOT-TOUCH"  # host file untouched — no write-escape
    assert not (workdir / "AGENTS.md").is_symlink()
    assert (workdir / "AGENTS.md").read_text() == "fresh content\n"
    # staging sibling (outside the mount dir) is removed
    assert list((tmp_path / "sid").glob(".AGENTS.md.*")) == []


def test_publish_workdir_file_plain_write(tmp_path):
    workdir = tmp_path / "sid" / "workdir"
    workdir.mkdir(parents=True)
    oo._publish_workdir_file(str(workdir), "opencode.json", '{"k":1}')
    assert (workdir / "opencode.json").read_text() == '{"k":1}'


# ---------------------------------------------------------------------------
# Additional hardening coverage
# ---------------------------------------------------------------------------


class TestOpencodeHardening:
    @pytest.mark.asyncio
    async def test_events_after_error_are_not_persisted(self):
        # L4: parser drains the stream past a terminal error; the orchestrator must ignore
        # those stray events — one terminal error, no trailing assistant message, no 2nd
        # connector_required.
        lines = [
            b'{"type":"error","error":{"message":"boom"}}\n',
            b'{"type":"text","part":{"text":"late text, drained"}}\n',
        ]
        es, _cap = TestOrchestrateOpencode()._patch(history=[], exec_lines=lines)
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            saved_roles = [c.args[2] for c in oo.save_message.await_args_list]
        assert sum(1 for e in events if e.type == "error") == 1
        assert not any(e.type == "text_delta" for e in events)  # drained text not re-yielded
        assert "assistant" not in saved_roles  # nothing persisted after the terminal

    @pytest.mark.asyncio
    async def test_dot_opencode_symlink_unlinked_before_rmtree(self):
        # M6: a planted `.opencode -> elsewhere` symlink (which rmtree refuses) must be
        # unlinked first, or the prune is bypassable.
        es, cap = TestOrchestrateOpencode()._patch(
            history=[], exec_lines=[b'{"type":"step_finish","part":{"reason":"stop"}}\n']
        )
        with es:
            with (
                patch.object(oo.os.path, "islink", MagicMock(return_value=True)),
                patch.object(oo.os, "unlink", MagicMock()) as unlink,
            ):
                await _collect(
                    oo.orchestrate_chat_opencode(
                        db=mock_session(),
                        session_id=SESSION_ID,
                        cluster_name=None,
                        kubeconfig_path=None,
                        context_name=None,
                        user_message="x",
                        user_id=USER_ID,
                    )
                )
            unlink.assert_any_call("/tmp/does-not-matter/workdir/.opencode")

    @pytest.mark.asyncio
    async def test_genuine_stop_then_cancel_keeps_completed_turn(self):
        # L3: a Stop racing a GENUINE stop must not discard the completed turn nor emit a
        # second terminal — the done already reached the client; flush + release normally.
        import asyncio

        cancel = asyncio.Event()

        async def exec_factory(session_id, user_message, **kwargs):
            yield b'{"type":"text","part":{"text":"final answer"}}\n'
            yield (
                b'{"type":"step_finish","part":{"reason":"stop","tokens":{"input":3,"output":2}}}\n'
            )
            cancel.set()  # user hits Stop just after the genuine terminal

        es, _cap = TestOrchestrateOpencode()._patch(history=[], exec_factory=exec_factory)
        with es:
            events = await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                    cancel_event=cancel,
                )
            )
            from app.services import sandbox

            saved_roles = [c.args[2] for c in oo.save_message.await_args_list]
            sandbox.release.assert_awaited_once()
            sandbox.teardown.assert_not_awaited()  # completed cleanly → warm release
        dones = [e for e in events if e.type == "done"]
        assert len(dones) == 1 and not dones[0].metadata.get("stopped")  # genuine done, not stopped
        assert "assistant" in saved_roles  # the completed answer was persisted


class TestBrokerAuthoritySwitch:
    """P2: broker-on skips ONLY the engine-side budget row; session stats stay engine-fed."""

    _patch = TestOrchestrateOpencode._patch

    @pytest.mark.asyncio
    async def test_broker_on_skips_engine_usage_row_keeps_stats(self):
        line = (
            b'{"type":"step_finish","part":{"reason":"stop",'
            b'"tokens":{"input":10,"output":5},"cost":0.0123}}\n'
        )
        es, _cap = self._patch(history=[], exec_lines=[line])
        with (
            es,
            patch.object(
                oo.llm_broker,
                "agent_broker_env",
                MagicMock(return_value={"AWS_BEARER_TOKEN_BEDROCK": "grant-x"}),
            ),
            patch.object(
                oo.llm_broker,
                "agent_bedrock_endpoint_url",
                MagicMock(return_value="http://broker/api/llm-proxy"),
            ),
            patch.object(oo.llm_broker, "burn_env", MagicMock()),
            patch.object(oo, "_publish_workdir_file", lambda wd, name, content: None),
        ):
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            tu.update_session_stats.assert_awaited_once()
            tu.record_usage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_broker_off_engine_usage_row_carries_turn_id(self):
        line = (
            b'{"type":"step_finish","part":{"reason":"stop",'
            b'"tokens":{"input":10,"output":5},"cost":0.0123}}\n'
        )
        es, _cap = self._patch(history=[], exec_lines=[line])
        with (
            es,
            patch.object(oo.llm_broker, "agent_broker_env", MagicMock(return_value={})),
            patch.object(oo.llm_broker, "agent_bedrock_endpoint_url", MagicMock(return_value=None)),
        ):
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            tu.update_session_stats.assert_awaited_once()
            tu.record_usage.assert_awaited_once()
            assert isinstance(tu.record_usage.await_args.kwargs["turn_id"], uuid.UUID)


class TestStampFirstAssistantRow:
    _patch = TestOrchestrateOpencode._patch

    @pytest.mark.asyncio
    async def test_turn_totals_stamped_on_first_assistant_row(self):
        """Bubble fusion keeps the FIRST assistant row's fields — stamp must target it."""
        lines = [
            b'{"type":"text","part":{"type":"text","text":"looking..."}}\n',
            (
                b'{"type":"tool_use","part":{"tool":"bash","callID":"c1",'
                b'"state":{"status":"completed","input":{},"output":"ok","metadata":{"exit":0}}}}\n'
            ),
            b'{"type":"text","part":{"type":"text","text":"done."}}\n',
            (
                b'{"type":"step_finish","part":{"reason":"stop",'
                b'"tokens":{"input":7,"output":16,"cache":{"read":12608,"write":0}},"cost":0.0}}\n'
            ),
        ]
        es, _cap = self._patch(history=[], exec_lines=lines)
        stamp = AsyncMock()
        # save order: user turn, FIRST assistant flush (text+tool), tool_result, trailing text.
        user_row = SimpleNamespace(id=uuid.uuid4())
        first_row = SimpleNamespace(id=uuid.uuid4())
        with es, patch("app.services.turn_usage.stamp_message_usage", stamp):
            oo.save_message.side_effect = [
                user_row,
                first_row,
                SimpleNamespace(id=uuid.uuid4()),
                SimpleNamespace(id=uuid.uuid4()),
            ]
            await _collect(
                oo.orchestrate_chat_opencode(
                    db=mock_session(),
                    session_id=SESSION_ID,
                    cluster_name=None,
                    kubeconfig_path=None,
                    context_name=None,
                    user_message="x",
                    user_id=USER_ID,
                )
            )
            stamp.assert_awaited_once()
            args, kwargs = stamp.await_args
            assert args[1] == first_row.id
            assert kwargs["input_tokens"] == 7  # FRESH input — cache not folded in
            assert kwargs["cache_read_tokens"] == 12608
            # session stats carry the splits too
            assert tu.update_session_stats.await_args.kwargs["input_tokens"] == 7
            assert tu.update_session_stats.await_args.kwargs["cache_read_tokens"] == 12608
