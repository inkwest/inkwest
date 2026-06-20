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
    # The shipped CODE default preserves today's behavior: only Claude Code.
    # _env_file=None so a developer's local .env (which may enable inkwest-agent for
    # sandbox testing) can't mask the default this test guards.
    assert Settings(_env_file=None).agentic_engines_list == ["claude-cli"]


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


def test_runnable_engines_claude_both_when_both_enabled():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        # Claude → both engines (CLI native + agent via Converse), enabled order preserved.
        assert engines.runnable_engines("anthropic.claude-opus-4-8") == [
            "claude-cli",
            "inkwest-agent",
        ]
        # Non-Claude is Converse-only → agent alone, even with the CLI enabled.
        assert engines.runnable_engines("meta.llama3-1-70b-instruct-v1:0") == ["inkwest-agent"]


def test_runnable_engines_intersects_enabled_set():
    with patch.object(engines, "get_settings", return_value=_engine_settings("claude-cli")):
        # Only the CLI is on → Claude runs there; a non-Claude model has no runnable engine.
        assert engines.runnable_engines("anthropic.claude-opus-4-8") == ["claude-cli"]
        assert engines.runnable_engines("meta.llama3-1-70b-instruct-v1:0") == []


def test_engine_for_turn_honors_a_valid_pin():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        # A Claude model pinned to the agent runs there (Converse) instead of the default CLI.
        assert (
            engines.engine_for_turn("anthropic.claude-opus-4-8", "inkwest-agent") == "inkwest-agent"
        )
        # Pin matching the default still works.
        assert engines.engine_for_turn("anthropic.claude-opus-4-8", "claude-cli") == "claude-cli"


def test_engine_for_turn_ignores_an_unrunnable_pin():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        # claude-cli can't run a non-Claude model → fall back to the default rule (agent).
        assert (
            engines.engine_for_turn("meta.llama3-1-70b-instruct-v1:0", "claude-cli")
            == "inkwest-agent"
        )
    with patch.object(engines, "get_settings", return_value=_engine_settings("claude-cli")):
        # A pin to a disabled engine is ignored → default rule (the single enabled engine).
        assert engines.engine_for_turn("anthropic.claude-opus-4-8", "inkwest-agent") == "claude-cli"


def test_engine_for_turn_no_pin_matches_resolve_engine():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        assert engines.engine_for_turn("anthropic.claude-opus-4-8") == "claude-cli"
        assert engines.engine_for_turn("meta.llama3-1-70b-instruct-v1:0") == "inkwest-agent"


def _entry(provider="anthropic", options=None):
    return SimpleNamespace(provider=provider, options=options or {})


def test_capable_engines_is_pure_capability():
    # No registry row → ID heuristic. Claude is capable on every known engine; a
    # non-Claude vendor only on the agent engines (all known except claude-cli).
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        with patch("app.services.model_registry.get_entry", return_value=None):
            assert set(engines.capable_engines("anthropic.claude-opus-4-8")) == {
                "claude-cli",
                "inkwest-agent",
            }
            assert engines.capable_engines("meta.llama3-1-70b-instruct-v1:0") == ["inkwest-agent"]


def test_runnable_engines_honors_admin_availability():
    # A Claude model the admin restricted to inkwest-agent only (options["engines"]).
    entry = _entry(provider="anthropic", options={"engines": ["inkwest-agent"]})
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        with patch("app.services.model_registry.get_entry", return_value=entry):
            assert engines.runnable_engines("anthropic.claude-opus-4-8") == ["inkwest-agent"]
            # The default-rule engine for the turn follows the curation (CLI is no
            # longer offered, so the agent wins even for a Claude model).
            assert engines.engine_for_turn("anthropic.claude-opus-4-8") == "inkwest-agent"


def test_engine_for_turn_falls_back_when_nothing_offered():
    # Admin curated the model to an engine that isn't enabled → no runnable engine;
    # the turn still resolves to the capability default rather than wedging.
    entry = _entry(provider="anthropic", options={"engines": ["inkwest-agent"]})
    with patch.object(engines, "get_settings", return_value=_engine_settings("claude-cli")):
        with patch("app.services.model_registry.get_entry", return_value=entry):
            assert engines.runnable_engines("anthropic.claude-opus-4-8") == []
            assert engines.engine_for_turn("anthropic.claude-opus-4-8") == "claude-cli"


def test_configured_default_engine_reads_setting():
    with patch("app.services.app_settings.get_setting", return_value="inkwest-agent"):
        assert engines.configured_default_engine() == "inkwest-agent"
    # Empty/unset normalizes to None (resolve heuristically).
    with patch("app.services.app_settings.get_setting", return_value=""):
        assert engines.configured_default_engine() is None
    with patch("app.services.app_settings.get_setting", return_value=None):
        assert engines.configured_default_engine() is None


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
