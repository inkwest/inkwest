"""LLM-broker wiring in the claude-cli orchestrator: env injection + burn-on-turn-end."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import llm_broker
from app.services import llm_proxy_token as token
from app.services import orchestrator_cli as oc

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
        claude_cli_path="claude",
        claude_cli_timeout=120,
        sandbox_docker_path="docker",
    )


_ENABLED_ENTRY = SimpleNamespace(model_id="anthropic.claude-x", enabled=True, arn=None)


def _patches(settings, capture, *, execute=None, entries=None):
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
        patch.object(oc, "get_effective_skills", AsyncMock(return_value=[])),
        patch.object(oc, "apply_session_skills", MagicMock()),
        patch.object(oc, "update_session_stats", AsyncMock()),
        patch.object(oc, "record_usage", AsyncMock()),
        patch("app.services.sandbox.enforce_user_sandbox_cap", AsyncMock()),
        patch("app.services.sandbox.ensure", AsyncMock(return_value="conv")),
        patch("app.services.sandbox.release", AsyncMock()),
        patch("app.services.mcp_config.apply_session_mcp_selection", AsyncMock(return_value=[])),
        patch.object(oc, "execute_claude_cli", side_effect=execute or fake_execute_claude_cli),
    ]


async def _run(settings, capture, *, execute=None, cancel_event=None, entries=None):
    db = AsyncMock()
    db.commit = AsyncMock()
    ps = _patches(settings, capture, execute=execute, entries=entries)
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
