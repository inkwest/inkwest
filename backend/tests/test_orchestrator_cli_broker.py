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


def _patches(settings, capture):
    def fake_execute_claude_cli(*, extra_env=None, **kw):
        capture["extra_env"] = extra_env
        # Snapshot whether the grant is live *during* the turn (before the finally burns it).
        tok = (extra_env or {}).get("AWS_BEARER_TOKEN_BEDROCK")
        capture["token"] = tok
        capture["live_during_turn"] = bool(tok) and bool(token.verify(tok))
        return _aiter([])  # empty stream → orchestrator emits a synthetic done

    return [
        patch.object(oc, "get_settings", return_value=settings),
        patch.object(llm_broker, "get_settings", return_value=settings),
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
        patch.object(oc, "execute_claude_cli", side_effect=fake_execute_claude_cli),
    ]


async def _run(settings, capture):
    db = AsyncMock()
    db.commit = AsyncMock()
    ps = _patches(settings, capture)
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
