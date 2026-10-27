"""Tests for agentic engine resolution and the catalog gate."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import SecretStr

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


def test_default_is_claude_cli_only(monkeypatch):
    # The shipped CODE default preserves today's behavior: only Claude Code.
    # _env_file=None ignores a developer's local .env, and delenv drops the ambient env var
    # (the suite sets AGENTIC_ENGINES in conftest for the integration deployment default) so
    # this guards the true code default, masked by neither source.
    monkeypatch.delenv("AGENTIC_ENGINES", raising=False)
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
                "opencode",
            }
            # Capability is pure: every agent engine (inkwest-agent + opencode) can run a
            # non-Claude vendor; enabled-first ordering puts the enabled one ahead.
            assert engines.capable_engines("meta.llama3-1-70b-instruct-v1:0") == [
                "inkwest-agent",
                "opencode",
            ]


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


# --- opencode engine ---------------------------------------------------------------


def test_opencode_enabled_and_validate_accepts_it():
    with patch.object(engines, "get_settings", return_value=_engine_settings("claude-cli")):
        assert engines.opencode_enabled() is False
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "opencode")
    ):
        assert engines.opencode_enabled() is True
        engines.validate_engines()  # opencode is a known engine → no raise


def test_open_catalog_enabled_for_any_agent_engine():
    # claude-cli alone is Claude-only → no open catalog.
    with patch.object(engines, "get_settings", return_value=_engine_settings("claude-cli")):
        assert engines.open_catalog_enabled() is False
    # opencode is an agent engine → it opens the whole-Bedrock catalog on its own.
    with patch.object(engines, "get_settings", return_value=_engine_settings("opencode")):
        assert engines.open_catalog_enabled() is True
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "inkwest-agent")
    ):
        assert engines.open_catalog_enabled() is True


def test_opencode_runs_every_vendor_like_an_agent_engine():
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "opencode")
    ):
        with patch("app.services.model_registry.get_entry", return_value=None):
            # Non-Claude → only the agent engine (opencode) is runnable.
            assert engines.resolve_engine("meta.llama3-1-70b-instruct-v1:0") == "opencode"
            # Claude → CLI wins when ambiguous, but opencode is capable too.
            assert "opencode" in engines.capable_engines("anthropic.claude-opus-4-8")
            assert engines.resolve_engine("anthropic.claude-opus-4-8") == "claude-cli"
    # opencode alone → it runs Claude too (via Bedrock).
    with patch.object(engines, "get_settings", return_value=_engine_settings("opencode")):
        with patch("app.services.model_registry.get_entry", return_value=None):
            assert engines.resolve_engine("anthropic.claude-opus-4-8") == "opencode"


def test_engine_for_turn_honors_opencode_pin():
    # The linchpin that makes the get_orchestrator opencode branch reachable: an explicit
    # opencode pin resolves to OPENCODE when opencode is enabled and runnable for the model.
    with patch.object(
        engines, "get_settings", return_value=_engine_settings("claude-cli", "opencode")
    ):
        with patch("app.services.model_registry.get_entry", return_value=None):
            assert engines.engine_for_turn("anthropic.claude-opus-4-8", "opencode") == "opencode"
            llama = "meta.llama3-1-70b-instruct-v1:0"
            assert engines.engine_for_turn(llama, "opencode") == "opencode"


def _validate_settings_stub(**overrides):
    """A minimal settings stand-in for validate_settings (config-side attrs only)."""
    base = dict(
        app_env="development",
        secret_key="x",
        aws_region=None,
        llm_broker_enabled=False,
        sandbox_enabled=True,
        aws_bearer_token_bedrock=None,
        connectors_enabled=False,
        aws_sso_role_filter=None,
        vapid_public_key="",
        vapid_private_key=SecretStr(""),
        vapid_subject="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "enabled_engines",
    [["claude-cli"], ["claude-cli", "inkwest-agent"], ["claude-cli", "opencode"]],
)
def test_validate_settings_requires_bearer_when_broker_on(enabled_engines):
    # The broker swaps in the backend-held bearer for EVERY sandboxed engine (claude-cli's
    # cli_broker_env included) — booting without it would fail every turn mid-stream.
    from app.core import config as config_mod

    stub = _validate_settings_stub(llm_broker_enabled=True, aws_region="eu-west-1")
    with (
        patch.object(config_mod, "get_settings", return_value=stub),
        patch.object(engines, "get_settings", return_value=_engine_settings(*enabled_engines)),
    ):
        with pytest.raises(ValueError, match="AWS_BEARER_TOKEN_BEDROCK is required"):
            config_mod.validate_settings()


def test_validate_settings_requires_region_when_broker_on():
    from app.core import config as config_mod

    stub = _validate_settings_stub(llm_broker_enabled=True, aws_bearer_token_bedrock="tok")
    with (
        patch.object(config_mod, "get_settings", return_value=stub),
        patch.object(
            engines, "get_settings", return_value=_engine_settings("claude-cli", "opencode")
        ),
    ):
        with pytest.raises(ValueError, match="AWS_REGION is required"):
            config_mod.validate_settings()


def test_validate_settings_broker_on_sandbox_off_needs_no_aws():
    # Broker only serves sandboxed engines; without the sandbox it is inert — no gate.
    from app.core import config as config_mod

    stub = _validate_settings_stub(llm_broker_enabled=True, sandbox_enabled=False)
    with (
        patch.object(config_mod, "get_settings", return_value=stub),
        patch.object(engines, "get_settings", return_value=_engine_settings("claude-cli")),
    ):
        config_mod.validate_settings()  # must not raise


def test_validate_settings_opencode_broker_off_needs_no_aws():
    # Broker off: opencode's creds ride sandbox.env (opaque to the backend) — no gate.
    from app.core import config as config_mod

    stub = _validate_settings_stub()
    with (
        patch.object(config_mod, "get_settings", return_value=stub),
        patch.object(
            engines, "get_settings", return_value=_engine_settings("claude-cli", "opencode")
        ),
    ):
        config_mod.validate_settings()  # must not raise
