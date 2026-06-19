"""Tests for agentic engine resolution and the catalog gate."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.services import engines


def _engine_settings(*enabled):
    """A settings stand-in exposing only the parsed engine list."""
    return SimpleNamespace(agentic_engines_list=list(enabled))


def test_agentic_engines_list_strips_and_dedupes():
    settings = Settings(agentic_engines="claude-cli, inkwest-agent , claude-cli")
    assert settings.agentic_engines_list == ["claude-cli", "inkwest-agent"]


def test_agentic_engines_list_empty():
    assert Settings(agentic_engines="  ,  ").agentic_engines_list == []


def test_default_is_claude_cli_only():
    # The shipped default preserves today's behavior: only Claude Code.
    assert Settings().agentic_engines_list == ["claude-cli"]


def test_agent_image_falls_back_to_toolbox():
    # Empty agent image → reuse the toolbox image (it bakes the agent in).
    assert Settings(sandbox_toolbox_image="claude-tools:1").agent_image == "claude-tools:1"
    # A dedicated agent image wins when set.
    s = Settings(sandbox_toolbox_image="claude-tools:1", sandbox_agent_image="agent:2")
    assert s.agent_image == "agent:2"


def test_inkwest_agent_enabled_reflects_list():
    with patch.object(engines, "get_settings", return_value=_engine_settings("claude-cli")):
        assert engines.inkwest_agent_enabled() is False
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        assert engines.inkwest_agent_enabled() is True


def test_resolve_engine_non_claude_routes_to_inkwest_agent():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        assert engines.resolve_engine("meta.llama3-1-70b-instruct-v1:0") == "inkwest-agent"


def test_resolve_engine_claude_single_engine_wins():
    with patch.object(engines, "get_settings", return_value=_engine_settings("inkwest-agent")):
        # Only inkwest-agent enabled → Claude runs there (via Converse) too.
        assert engines.resolve_engine("anthropic.claude-opus-4-8") == "inkwest-agent"
    with patch.object(engines, "get_settings", return_value=_engine_settings("claude-cli")):
        assert engines.resolve_engine("anthropic.claude-opus-4-8") == "claude-cli"


def test_resolve_engine_claude_prefers_claude_cli_when_ambiguous():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        assert engines.resolve_engine("anthropic.claude-opus-4-8") == "claude-cli"


def test_validate_engines_rejects_unknown():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "bogus")
    ):
        with pytest.raises(ValueError, match="unknown engine"):
            engines.validate_engines()


def test_validate_engines_rejects_empty():
    with patch.object(engines, "get_settings", return_value=_engine_settings()):
        with pytest.raises(ValueError, match="at least one engine"):
            engines.validate_engines()


def test_validate_engines_accepts_known():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        engines.validate_engines()  # no raise
