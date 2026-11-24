"""LLM-broker wiring in the claude-cli orchestrator: env injection + burn-on-turn-end."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import llm_broker
from app.services import llm_proxy_token as token
from app.services import orchestrator_cli as oc
from tests.conftest import mock_session

SESSION_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


async def _collect(gen):
    return [e async for e in gen]


async def _aiter(lines):
    for line in lines:
        yield line


def _settings(*, broker_enabled: bool, base: str = "http://gw:8000") -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_enabled=True,
        llm_broker_enabled=broker_enabled,
        mcp_proxy_base_url=base,
        llm_broker_base_url="",
        claude_cli_path="claude",
        claude_cli_timeout=120,
        agent_stream_idle_timeout=120,
        agent_progress_timeout=900,
        sandbox_docker_path="docker",
    )


_ENABLED_ENTRY = SimpleNamespace(model_id="anthropic.claude-x", enabled=True, arn=None)


def _reads_db(result):
    async def read(db, *a, **kw):
        await db.execute("SELECT 1")
        return result

    return read


def _patches(settings, capture, *, execute=None, entries=None, ensure=None):
    def fake_execute_claude_cli(*, extra_env=None, model=None, **kw):
        capture["extra_env"] = extra_env
        capture["model"] = model
        # Snapshot whether the grant is live *during* the turn (before the finally burns it).
        tok = (extra_env or {}).get("AWS_BEARER_TOKEN_BEDROCK")
        capture["token"] = tok
        capture["live_during_turn"] = bool(tok) and bool(token.verify(tok))
        return _aiter([])  # empty stream → orchestrator emits a synthetic done

    return [
        patch.object(oc, "get_settings", return_value=settings),
        patch.object(llm_broker, "get_settings", return_value=settings),
        # Broker ON + no session/admin choice ⇒ the orchestrator forces the registry default so
        # the CLI never invokes an id outside the broker allowlist. Stub the registry: one enabled
        # entry (so the "no enabled models" guard passes) + the resolved default id.
        patch(
            "app.services.model_registry.merged_entries",
            return_value=[_ENABLED_ENTRY] if entries is None else entries,
        ),
        patch("app.services.model_registry.default_model", return_value="anthropic.claude-x"),
        patch.object(oc, "save_message", AsyncMock()),
        patch.object(oc, "ensure_session_title", AsyncMock(return_value="title")),
        patch.object(oc, "get_cli_session_id", AsyncMock(return_value=None)),
        patch.object(oc, "set_cli_session_id", AsyncMock()),
        patch.object(oc, "clear_cli_session_id", AsyncMock()),
        patch.object(oc, "build_connector_env", AsyncMock(return_value=({}, []))),
        patch.object(oc, "resolve_session_system_prompt", AsyncMock(return_value="base")),
        patch.object(oc, "get_system_prompt", MagicMock(return_value="SYS")),
        patch.object(oc, "resolve_session_model_choice", AsyncMock(return_value=None)),
        patch.object(oc, "get_enabled_mcp_servers", AsyncMock(return_value=[])),
        # Reads the DB after sandbox.ensure, like the real helper.
        patch.object(oc, "get_effective_skills", AsyncMock(side_effect=_reads_db([]))),
        patch.object(oc, "apply_session_skills", MagicMock()),
        patch("app.services.turn_usage.update_session_stats", AsyncMock()),
        patch("app.services.turn_usage.record_usage", AsyncMock()),
        patch("app.services.sandbox.enforce_user_sandbox_cap", AsyncMock()),
        patch("app.services.sandbox.ensure", ensure or AsyncMock(return_value="conv")),
        patch("app.services.sandbox.release", AsyncMock()),
        patch("app.services.mcp_config.apply_session_mcp_selection", AsyncMock(return_value=[])),
        patch.object(oc, "execute_claude_cli", side_effect=execute or fake_execute_claude_cli),
    ]


async def _run(
    settings, capture, *, execute=None, cancel_event=None, entries=None, db=None, ensure=None
):
    if db is None:
        db = mock_session()
    ps = _patches(settings, capture, execute=execute, entries=entries, ensure=ensure)
    for p in ps:
        p.start()
    try:
        return await _collect(
            oc.orchestrate_chat_cli(
                db=db,
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="why crash?",
                user_id=USER_ID,
                cancel_event=cancel_event,
            )
        )
    finally:
        for p in ps:
            p.stop()


@pytest.mark.asyncio
async def test_compaction_summary_reaches_the_cli_system_prompt() -> None:
    """A compacted session's summary must ride --append-system-prompt on every later turn."""
    capture: dict = {}

    def fake_execute(*, system_prompt=None, **kw):
        capture["system_prompt"] = system_prompt
        return _aiter([])

    with patch(
        "app.services.compaction.last_boundary",
        AsyncMock(
            return_value=SimpleNamespace(
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
    ):
        await _run(_settings(broker_enabled=False), capture, execute=fake_execute)

    assert "Earlier conversation (compacted)" in capture["system_prompt"]
    assert "FACT: the pod was OOMKilled" in capture["system_prompt"]


@pytest.mark.asyncio
async def test_broker_env_injected_and_burned_on_turn_end() -> None:
    capture: dict = {}
    await _run(_settings(broker_enabled=True), capture)

    env = capture["extra_env"]
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://gw:8000/api/llm-proxy"
    assert capture["live_during_turn"] is True  # grant resolvable while the CLI ran
    # …and burned in the finally: the same token is dead once the turn ended.
    with pytest.raises(token.ProxyAuthError):
        token.verify(capture["token"])


@pytest.mark.asyncio
async def test_no_broker_env_when_disabled() -> None:
    capture: dict = {}
    await _run(_settings(broker_enabled=False), capture)
    # connector env is empty and the broker is off ⇒ nothing injected.
    assert capture["extra_env"] is None


@pytest.mark.asyncio
async def test_broker_enabled_but_base_unset_injects_nothing() -> None:
    """Enabled but mcp_proxy_base_url empty ⇒ the helper falls back to direct Bedrock: no grant
    minted, no broker env injected (the warn-and-fallback path, proven end-to-end not just in the
    helper unit test)."""
    capture: dict = {}
    await _run(_settings(broker_enabled=True, base=""), capture)
    assert capture["extra_env"] is None  # connector env empty + no broker env
    assert capture["token"] is None  # no grant was minted


@pytest.mark.asyncio
async def test_broker_on_forces_registry_default_model() -> None:
    """Broker ON + no session/admin choice ⇒ force the registry default so the CLI never
    invokes its built-in default id (which the broker allowlist would 403)."""
    capture: dict = {}
    await _run(_settings(broker_enabled=True), capture)
    assert capture["model"] == "anthropic.claude-x"


@pytest.mark.asyncio
async def test_broker_on_no_enabled_models_errors_clearly() -> None:
    """Broker ON + no model pin + NO enabled registry rows ⇒ the broker is fail-closed, so the
    turn is refused with a clear config error rather than an opaque mid-turn 403."""
    capture: dict = {}
    events = await _run(_settings(broker_enabled=True), capture, entries=[])
    assert any(e.type == "error" and "enabled" in (e.content or "").lower() for e in events)
    assert "model" not in capture  # execute_claude_cli never reached


@pytest.mark.asyncio
async def test_grant_burned_when_execute_raises() -> None:
    """The security claim — burn runs on the ERROR exit path, not just the clean one. A token
    that leaked from the sandbox must be dead even when the turn blows up mid-flight."""
    seen: dict = {}

    def boom(*, extra_env=None, model=None, **kw):
        seen["token"] = (extra_env or {}).get("AWS_BEARER_TOKEN_BEDROCK")
        assert seen["token"] and token.verify(seen["token"])  # live during the turn
        raise RuntimeError("upstream exploded mid-turn")

    capture: dict = {}
    events = await _run(_settings(broker_enabled=True), capture, execute=boom)
    assert any(e.type == "error" for e in events)  # error surfaced, not swallowed
    with pytest.raises(token.ProxyAuthError):  # …and the grant was burned in the finally
        token.verify(seen["token"])


@pytest.mark.asyncio
async def test_grant_burned_when_cancelled() -> None:
    """Burn also runs when the turn is cancelled (cancel_event pre-set)."""
    import asyncio

    capture: dict = {}
    cancel = asyncio.Event()
    cancel.set()
    await _run(_settings(broker_enabled=True), capture, cancel_event=cancel)
    assert capture["token"]
    with pytest.raises(token.ProxyAuthError):
        token.verify(capture["token"])


# --- P2 authority switch: broker-on skips the engine-side budget row ----------------------


def _usage_events():
    from app.services.events import ChatEvent

    events = [
        ChatEvent(type="text_delta", content="hi"),
        ChatEvent(type="done", metadata={"input_tokens": 10, "output_tokens": 5}),
    ]

    async def fake_parse(_stream):
        for e in events:
            yield e

    return fake_parse


@pytest.mark.asyncio
async def test_broker_on_skips_engine_usage_row_keeps_stats() -> None:
    """Broker metering is the authority: no engine UsageRecord, session stats still update."""
    capture: dict = {}
    ru, uss = AsyncMock(), AsyncMock()
    ps = _patches(_settings(broker_enabled=True), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", uss),
        patch.object(oc, "parse_cli_response", _usage_events()),
    ]
    for p in ps:
        p.start()
    try:
        db = mock_session()
        await _collect(
            oc.orchestrate_chat_cli(
                db=db,
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="why crash?",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    uss.assert_awaited_once()
    ru.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_off_engine_usage_row_carries_turn_id() -> None:
    """Broker off: exactly one engine row per turn, stamped with the per-turn id."""
    capture: dict = {}
    ru, uss = AsyncMock(), AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", uss),
        patch.object(oc, "parse_cli_response", _usage_events()),
    ]
    for p in ps:
        p.start()
    try:
        await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="why crash?",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    uss.assert_awaited_once()
    ru.assert_awaited_once()
    assert isinstance(ru.await_args.kwargs["turn_id"], uuid.UUID)


@pytest.mark.asyncio
async def test_errored_turn_persists_accumulated_usage() -> None:
    """CLI exit 1 mid-turn (P3): tokens accumulated before the crash are still persisted."""
    from app.services.events import ChatEvent

    async def parse_with_crash(_stream):
        yield ChatEvent(type="text_delta", content="partial")
        yield ChatEvent(
            type="usage_stats",
            metadata={
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_tokens": 5000,
                "cache_write_tokens": 100,
            },
        )
        raise RuntimeError("Claude CLI failed with exit code 1")

    capture: dict = {}
    ru, uss = AsyncMock(), AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", uss),
        patch.object(oc, "parse_cli_response", parse_with_crash),
    ]
    for p in ps:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="why crash?",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    assert events[-1].type == "error"  # the turn still surfaces its failure
    uss.assert_awaited_once()
    ru.assert_awaited_once()
    kwargs = ru.await_args.kwargs
    assert kwargs["input_tokens"] == 10
    assert kwargs["cache_read_tokens"] == 5000
    assert kwargs["cache_write_tokens"] == 100


@pytest.mark.asyncio
async def test_done_turn_persists_cache_tokens() -> None:
    """Cache counts from the done metadata land in the engine usage row (broker off)."""
    from app.services.events import ChatEvent

    async def parse_done(_stream):
        yield ChatEvent(type="text_delta", content="hi")
        yield ChatEvent(
            type="done",
            metadata={
                "input_tokens": 10,
                "output_tokens": 349,
                "cache_read_tokens": 0,
                "cache_write_tokens": 26797,
            },
        )

    capture: dict = {}
    ru, uss = AsyncMock(), AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", uss),
        patch.object(oc, "parse_cli_response", parse_done),
    ]
    for p in ps:
        p.start()
    try:
        await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    ru.assert_awaited_once()
    assert ru.await_args.kwargs["cache_write_tokens"] == 26797
    assert uss.await_args.kwargs["cache_write_tokens"] == 26797
    assert uss.await_args.kwargs["cache_read_tokens"] == 0


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        ({"type": "done", "metadata": {"input_tokens": 15, "context_tokens": 53_107}}, 53_107),
        ({"type": "done", "metadata": {"input_tokens": 15}}, None),
        (
            {"type": "usage_stats", "metadata": {"input_tokens": 8, "context_tokens": 52_205}},
            52_205,
        ),
    ],
)
@pytest.mark.asyncio
async def test_assistant_row_gets_context_tokens(terminal: dict, expected: int | None) -> None:
    """The saved assistant row carries the last call's prompt, from done or a cut stream."""
    from app.services.events import ChatEvent

    async def parse(_stream):
        yield ChatEvent(type="text_delta", content="hi")
        yield ChatEvent(**terminal)
        if terminal["type"] == "usage_stats":
            raise RuntimeError("Claude CLI failed with exit code 1")

    capture: dict = {}
    save = AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", AsyncMock()),
        patch("app.services.turn_usage.update_session_stats", AsyncMock()),
        patch.object(oc, "parse_cli_response", parse),
        patch.object(oc, "save_message", save),
    ]
    for p in ps:
        p.start()
    try:
        await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    assistant = [c for c in save.await_args_list if c.args[2] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].kwargs["context_tokens"] == expected


@pytest.mark.asyncio
async def test_stopped_done_carries_the_usage_the_row_gets() -> None:
    """A user Stop: the live done must carry what the DB row is stamped with."""
    import asyncio

    from app.services.events import ChatEvent

    async def parse_stopped(_stream):
        yield ChatEvent(type="text_delta", content="partial")
        # Clean stream end after the kill: the parser surfaces the usage on usage_stats.
        yield ChatEvent(type="usage_stats", metadata={"input_tokens": 8, "context_tokens": 52_205})

    cancel = asyncio.Event()
    cancel.set()
    capture: dict = {}
    save = AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", AsyncMock()),
        patch("app.services.turn_usage.update_session_stats", AsyncMock()),
        patch.object(oc, "parse_cli_response", parse_stopped),
        patch.object(oc, "save_message", save),
    ]
    for p in ps:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
                cancel_event=cancel,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    done = events[-1]
    assert done.type == "done" and done.metadata["stopped"] is True
    assert done.metadata["context_tokens"] == 52_205
    assert done.metadata["input_tokens"] == 8
    assistant = [c for c in save.await_args_list if c.args[2] == "assistant"]
    assert assistant[0].kwargs["context_tokens"] == 52_205


async def _run_cli_turn(parse, *, cancel=None, extra: list | None = None):
    """Run one CLI turn with the parser and save_message replaced; returns (events, save)."""
    capture: dict = {}
    save = AsyncMock()
    ps = (
        _patches(_settings(broker_enabled=False), capture)
        + [
            patch("app.services.turn_usage.record_usage", AsyncMock()),
            patch("app.services.turn_usage.update_session_stats", AsyncMock()),
            patch.object(oc, "parse_cli_response", parse),
            patch.object(oc, "save_message", save),
        ]
        + (extra or [])
    )
    for p in ps:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
                cancel_event=cancel,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    return events, save


@pytest.mark.parametrize("terminal", ["done", "error"])
@pytest.mark.asyncio
async def test_stop_after_terminal_reports_usage_once(terminal: str) -> None:
    """A Stop after the terminal reached the client: one usage-carrying terminal only."""
    import asyncio

    from app.services.events import ChatEvent

    usage = {"input_tokens": 8, "context_tokens": 52_205}

    async def parse(_stream):
        yield ChatEvent(type="text_delta", content="answer")
        if terminal == "done":
            yield ChatEvent(type="done", metadata=dict(usage))
        else:
            # The CLI's error event carries no usage; the parser surfaces it on stream end.
            yield ChatEvent(type="error", content="API error", is_error=True)
            yield ChatEvent(type="usage_stats", metadata=dict(usage))

    cancel = asyncio.Event()
    cancel.set()
    events, _save = await _run_cli_turn(parse, cancel=cancel)
    with_usage = [
        e for e in events if e.type in ("done", "error") and e.metadata.get("context_tokens")
    ]
    assert len(with_usage) == 1
    assert with_usage[0].metadata["context_tokens"] == 52_205
    assert len([e for e in events if e.type == "done"]) == 1


@pytest.mark.asyncio
async def test_stale_resume_retry_drops_the_first_attempt_reading() -> None:
    """Attempt 1 died on a stale --resume after reporting usage; attempt 2 is stopped."""
    import asyncio

    from app.services.events import ChatEvent

    attempts = {"n": 0}

    async def parse(_stream):
        attempts["n"] += 1
        if attempts["n"] == 1:
            yield ChatEvent(
                type="usage_stats", metadata={"input_tokens": 8, "context_tokens": 52_205}
            )
            raise RuntimeError("No conversation found with session ID: old-cli-id")
        yield ChatEvent(type="text_delta", content="partial")

    cancel = asyncio.Event()
    cancel.set()
    events, save = await _run_cli_turn(
        parse,
        cancel=cancel,
        extra=[patch.object(oc, "get_cli_session_id", AsyncMock(return_value="old-cli-id"))],
    )
    assert attempts["n"] == 2
    done = events[-1]
    assert done.type == "done" and done.metadata["stopped"] is True
    assert "context_tokens" not in done.metadata
    assistant = [c for c in save.await_args_list if c.args[2] == "assistant"]
    assert assistant[0].kwargs["context_tokens"] is None


@pytest.mark.asyncio
async def test_timeout_turn_persists_accumulated_usage() -> None:
    """H: the TimeoutError handler must persist too (a timed-out turn burned real spend)."""

    async def parse_with_timeout(_stream):
        from app.services.events import ChatEvent

        yield ChatEvent(type="text_delta", content="partial")
        yield ChatEvent(
            type="usage_stats",
            metadata={
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
        )
        raise TimeoutError("idle stall")

    capture: dict = {}
    ru = AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", AsyncMock()),
        patch.object(oc, "parse_cli_response", parse_with_timeout),
    ]
    for p in ps:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    assert events[-1].type == "error"
    ru.assert_awaited_once()
    assert ru.await_args.kwargs["input_tokens"] == 12
    # containment: the internal accounting event never reaches the client
    assert all(e.type != "usage_stats" for e in events)


@pytest.mark.asyncio
async def test_unexpected_error_persists_accumulated_usage() -> None:
    """H: the generic Exception handler must persist too."""

    async def parse_with_boom(_stream):
        from app.services.events import ChatEvent

        yield ChatEvent(
            type="usage_stats",
            metadata={
                "input_tokens": 7,
                "output_tokens": 2,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
        )
        # Neither RuntimeError nor TimeoutError -> lands in the generic Exception handler.
        raise ValueError("unexpected parser blowup")

    capture: dict = {}
    ru = AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", AsyncMock()),
        patch.object(oc, "parse_cli_response", parse_with_boom),
    ]
    for p in ps:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    assert events[-1].type == "error"
    ru.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_persists_usage_surfaced_on_clean_stream_end() -> None:
    """A user Stop kills the CLI (clean stream end, no done): the parser's trailing
    usage_stats must feed a persist before the stopped done."""
    import asyncio as aio

    from app.services.events import ChatEvent

    cancel = aio.Event()
    cancel.set()

    async def parse_stopped(_stream):
        yield ChatEvent(type="text_delta", content="partial answer")
        yield ChatEvent(
            type="usage_stats",
            metadata={
                "input_tokens": 30,
                "output_tokens": 11,
                "cache_read_tokens": 5000,
                "cache_write_tokens": 100,
            },
        )

    capture: dict = {}
    ru = AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", AsyncMock()),
        patch.object(oc, "parse_cli_response", parse_stopped),
    ]
    for p in ps:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
                cancel_event=cancel,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    assert events[-1].type == "done" and events[-1].metadata.get("stopped") is True
    ru.assert_awaited_once()
    assert ru.await_args.kwargs["cache_read_tokens"] == 5000
    assert all(e.type != "usage_stats" for e in events)


@pytest.mark.asyncio
async def test_cache_only_turn_is_persisted() -> None:
    """A fully-cached turn (0 fresh in/out) still burned billable cache tokens."""
    from app.services.events import ChatEvent

    async def parse_cached(_stream):
        yield ChatEvent(
            type="done",
            metadata={
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 8000,
                "cache_write_tokens": 0,
            },
        )

    capture: dict = {}
    ru = AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", AsyncMock()),
        patch.object(oc, "parse_cli_response", parse_cached),
    ]
    for p in ps:
        p.start()
    try:
        await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    ru.assert_awaited_once()
    assert ru.await_args.kwargs["cache_read_tokens"] == 8000


@pytest.mark.asyncio
async def test_1h_ttl_split_threaded_to_record_usage() -> None:
    from app.services.events import ChatEvent

    async def parse_1h(_stream):
        yield ChatEvent(
            type="done",
            metadata={
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_read_tokens": 0,
                "cache_write_tokens": 1000,
                "cache_write_1h_tokens": 400,
            },
        )

    capture: dict = {}
    ru = AsyncMock()
    ps = _patches(_settings(broker_enabled=False), capture) + [
        patch("app.services.turn_usage.record_usage", ru),
        patch("app.services.turn_usage.update_session_stats", AsyncMock()),
        patch.object(oc, "parse_cli_response", parse_1h),
    ]
    for p in ps:
        p.start()
    try:
        await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="q",
                user_id=USER_ID,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
    assert ru.await_args.kwargs["cache_write_1h_tokens"] == 400


@pytest.mark.asyncio
async def test_read_txn_released_before_stream() -> None:
    """The last DB call before sandbox.ensure and before the stream is a commit."""
    db = mock_session()
    last_db_call: dict[str, str | None] = {}

    def snapshot(step: str) -> None:
        last_db_call[step] = db.method_calls[-1][0] if db.method_calls else None

    async def ensure(*a, **kw):
        snapshot("ensure")
        return "conv"

    def execute(*, extra_env=None, model=None, **kw):
        snapshot("exec")
        return _aiter([])

    await _run(_settings(broker_enabled=False), {}, execute=execute, db=db, ensure=ensure)

    assert last_db_call == {"ensure": "commit", "exec": "commit"}


async def _compacting_turn(execute, *, cli_session_id=None):
    """Run a CLI turn the way a compaction does, returning (events, record_usage mock)."""
    from app.services import compaction

    ru = AsyncMock()
    ps = _patches(_settings(broker_enabled=False), {}, execute=execute) + [
        patch.object(oc, "get_cli_session_id", AsyncMock(return_value=cli_session_id)),
        patch("app.services.turn_usage.record_usage", ru),
    ]
    flag = compaction._compacting.set(True)
    for p in ps:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_cli(
                db=mock_session(),
                session_id=SESSION_ID,
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="summarize",
                user_id=USER_ID,
                persist_user_message=False,
            )
        )
    finally:
        for p in reversed(ps):
            p.stop()
        compaction._compacting.reset(flag)
    return events, ru


@pytest.mark.asyncio
async def test_compaction_without_a_cli_session_never_spawns() -> None:
    """No --resume target means the CLI has nothing to summarize: a spawn is a billed turn
    whose summary can only be discarded."""
    execute = MagicMock(side_effect=AssertionError("spawned a blind compaction"))
    events, ru = await _compacting_turn(execute)
    assert events == []
    execute.assert_not_called()
    ru.assert_not_awaited()


@pytest.mark.asyncio
async def test_compaction_on_a_stale_cli_session_never_respawns() -> None:
    """The stale --resume self-heal retries fresh, i.e. blind: fine for a question, a billed
    no-op for a compaction."""
    resumes: list[str | None] = []

    async def fail(**kw):
        raise RuntimeError("Claude CLI failed: No conversation found with session ID: stale-id")
        yield  # pragma: no cover

    def execute(*, resume_session_id=None, **kw):
        resumes.append(resume_session_id)
        return fail()

    events, ru = await _compacting_turn(execute, cli_session_id="stale-id")
    assert resumes == ["stale-id"]
    assert events == []
    ru.assert_not_awaited()
