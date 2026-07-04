"""Tests for the LLM broker wiring helpers (app.services.llm_broker)."""

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services import llm_broker
from app.services import llm_proxy_token as token

_U = uuid.UUID("00000000-0000-4000-8000-000000000001")
_S = uuid.UUID("00000000-0000-4000-8000-000000000002")


def _settings(enabled: bool, base: str = "http://host.docker.internal:8000") -> SimpleNamespace:
    return SimpleNamespace(llm_broker_enabled=enabled, mcp_proxy_base_url=base)


def _patch(settings: SimpleNamespace):
    return patch("app.services.llm_broker.get_settings", return_value=settings)


def test_base_url_none_when_disabled() -> None:
    with _patch(_settings(enabled=False)):
        assert llm_broker.broker_base_url() is None


def test_base_url_none_when_host_unset() -> None:
    with _patch(_settings(enabled=True, base="")):
        assert llm_broker.broker_base_url() is None  # warns + falls back to direct Bedrock


def test_base_url_appends_proxy_path() -> None:
    with _patch(_settings(enabled=True, base="http://gw:8000")):
        assert llm_broker.broker_base_url() == "http://gw:8000/api/llm-proxy"


def test_base_url_strips_trailing_slash() -> None:
    with _patch(_settings(enabled=True, base="http://gw:8000/")):
        assert llm_broker.broker_base_url() == "http://gw:8000/api/llm-proxy"


def test_cli_env_disabled_is_empty() -> None:
    with _patch(_settings(enabled=False)):
        assert llm_broker.cli_broker_env(_U, _S) == {}


def test_cli_env_carries_bedrock_routing_and_live_grant() -> None:
    with _patch(_settings(enabled=True, base="http://gw:8000")):
        env = llm_broker.cli_broker_env(_U, _S)
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://gw:8000/api/llm-proxy"
    # The token is a live grant resolvable by the broker until burned.
    claims = token.verify(env["AWS_BEARER_TOKEN_BEDROCK"])
    assert claims.session_id == str(_S)
    llm_broker.burn_env(env)


def test_agent_endpoint_url_matches_base() -> None:
    with _patch(_settings(enabled=True, base="http://gw:8000")):
        assert llm_broker.agent_bedrock_endpoint_url() == "http://gw:8000/api/llm-proxy"
    with _patch(_settings(enabled=False)):
        assert llm_broker.agent_bedrock_endpoint_url() is None


def test_agent_env_carries_only_grant() -> None:
    with _patch(_settings(enabled=True, base="http://gw:8000")):
        env = llm_broker.agent_broker_env(_U, _S)
    assert set(env) == {"AWS_BEARER_TOKEN_BEDROCK"}
    token.verify(env["AWS_BEARER_TOKEN_BEDROCK"])  # live
    llm_broker.burn_env(env)


def test_agent_env_disabled_is_empty() -> None:
    with _patch(_settings(enabled=False)):
        assert llm_broker.agent_broker_env(_U, _S) == {}


def test_burn_env_revokes_grant() -> None:
    with _patch(_settings(enabled=True, base="http://gw:8000")):
        env = llm_broker.cli_broker_env(_U, _S)
    tok = env["AWS_BEARER_TOKEN_BEDROCK"]
    token.verify(tok)  # live before burn
    llm_broker.burn_env(env)
    with pytest.raises(token.ProxyAuthError):
        token.verify(tok)  # dead after burn


def test_burn_env_none_and_empty_safe() -> None:
    llm_broker.burn_env(None)
    llm_broker.burn_env({})  # no token key ⇒ no-op


def test_env_helpers_thread_engine_and_turn_id_into_claims() -> None:
    turn = uuid.uuid4()
    with _patch(_settings(enabled=True, base="http://gw:8000")):
        cli_env = llm_broker.cli_broker_env(_U, _S, engine="claude-cli", turn_id=turn)
        agent_env = llm_broker.agent_broker_env(_U, _S, engine="opencode", turn_id=turn)
    cli_claims = token.verify(cli_env["AWS_BEARER_TOKEN_BEDROCK"])
    agent_claims = token.verify(agent_env["AWS_BEARER_TOKEN_BEDROCK"])
    assert cli_claims.engine == "claude-cli"
    assert agent_claims.engine == "opencode"
    assert cli_claims.turn_id == str(turn) == agent_claims.turn_id
    llm_broker.burn_env(cli_env)
    llm_broker.burn_env(agent_env)
