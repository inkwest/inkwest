"""Tests for the provider-aware model catalog."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import model_catalog
from app.services.model_catalog import (
    available_models,
    default_model,
    refresh_catalog,
    selectable_models,
    static_catalog,
)


@pytest.fixture(autouse=True)
def reset_catalog_state():
    """Isolate the module-level cache and settings overrides between tests."""
    yield
    model_catalog._live_models = None
    model_catalog._fetched_at = 0.0
    model_catalog._last_fetch_failed = False


def _settings(**overrides):
    base = {
        "ai_provider": "anthropic",
        "anthropic_api_key": "sk-test",  # pragma: allowlist secret
        "aws_region": "eu-west-1",
        "aws_access_key_id": None,
        "aws_secret_access_key": None,
        "aws_cli_path": "aws",
        "claude_cli_models": "",
        "sandbox_enabled": False,
        "sandbox_env_file": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_static_catalog_anthropic_excludes_bedrock_ids():
    with patch.object(model_catalog, "get_settings", return_value=_settings()):
        models = static_catalog()
    assert "claude-opus-4-8" in models
    assert not any(m.startswith("anthropic.") for m in models)


def test_static_catalog_bedrock_only_bedrock_ids():
    with patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")):
        models = static_catalog()
    assert all(m.startswith("anthropic.") for m in models)
    assert "anthropic.claude-3-5-sonnet-20241022-v2:0" in models


def test_available_models_prefers_live_cache():
    model_catalog._live_models = ["claude-live-1", "claude-live-2"]
    assert available_models() == ["claude-live-1", "claude-live-2"]


@pytest.mark.asyncio
async def test_refresh_catalog_anthropic_success():
    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(
            model_catalog,
            "_fetch_anthropic_models",
            AsyncMock(return_value=["claude-fable-5", "claude-opus-4-8"]),
        ),
    ):
        await refresh_catalog(force=True)
    assert model_catalog._live_models == ["claude-fable-5", "claude-opus-4-8"]


@pytest.mark.asyncio
async def test_refresh_catalog_failure_keeps_fallback():
    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(
            model_catalog,
            "_fetch_anthropic_models",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        await refresh_catalog(force=True)
    assert model_catalog._live_models is None  # static fallback applies
    with patch.object(model_catalog, "get_settings", return_value=_settings()):
        assert available_models() == static_catalog()


@pytest.mark.asyncio
async def test_refresh_catalog_claude_cli_declared_list_skips_fetch():
    """An operator-declared CLAUDE_CLI_MODELS wins over auto-discovery."""
    fetch = AsyncMock()
    with (
        patch.object(
            model_catalog,
            "get_settings",
            return_value=_settings(ai_provider="claude-cli", claude_cli_models="claude-x"),
        ),
        patch.object(model_catalog, "_fetch_claude_cli_models", fetch),
    ):
        await refresh_catalog(force=True)
    fetch.assert_not_awaited()
    assert model_catalog._live_models is None  # static_catalog serves the declared list


@pytest.mark.asyncio
async def test_refresh_catalog_claude_cli_auto_discovers():
    with (
        patch.object(
            model_catalog, "get_settings", return_value=_settings(ai_provider="claude-cli")
        ),
        patch.object(
            model_catalog,
            "_fetch_claude_cli_models",
            AsyncMock(return_value=["eu.anthropic.claude-fable-5"]),
        ),
    ):
        await refresh_catalog(force=True)
    assert model_catalog._live_models == ["eu.anthropic.claude-fable-5"]


@pytest.mark.asyncio
async def test_fetch_claude_cli_models_bedrock_sandbox(tmp_path):
    """Sandbox env configuring Bedrock drives a Bedrock catalog fetch."""
    env_file = tmp_path / "sandbox.env"
    env_file.write_text(
        "CLAUDE_CODE_USE_BEDROCK=1\n"
        "AWS_BEARER_TOKEN_BEDROCK=secret-token\n"  # pragma: allowlist secret
        "AWS_REGION=eu-west-1\n"
        "# ANTHROPIC_MODEL=ignored-comment\n"
    )
    bedrock_fetch = AsyncMock(return_value=["eu.anthropic.claude-sonnet-4-5-20250929-v1:0"])
    with (
        patch.object(
            model_catalog,
            "get_settings",
            return_value=_settings(
                ai_provider="claude-cli",
                sandbox_enabled=True,
                sandbox_env_file=str(env_file),
            ),
        ),
        patch.object(model_catalog, "_fetch_bedrock_models", bedrock_fetch),
    ):
        models = await model_catalog._fetch_claude_cli_models()

    assert models == ["eu.anthropic.claude-sonnet-4-5-20250929-v1:0"]
    bedrock_fetch.assert_awaited_once_with(
        region="eu-west-1",
        extra_env={
            "AWS_BEARER_TOKEN_BEDROCK": "secret-token",  # pragma: allowlist secret
            "AWS_REGION": "eu-west-1",
        },
    )


@pytest.mark.asyncio
async def test_fetch_claude_cli_models_anthropic_key(tmp_path):
    env_file = tmp_path / "sandbox.env"
    env_file.write_text("ANTHROPIC_API_KEY=acme-sandbox\n")  # pragma: allowlist secret
    anthropic_fetch = AsyncMock(return_value=["claude-fable-5"])
    with (
        patch.object(
            model_catalog,
            "get_settings",
            return_value=_settings(
                ai_provider="claude-cli",
                sandbox_enabled=True,
                sandbox_env_file=str(env_file),
            ),
        ),
        patch.object(model_catalog, "_fetch_anthropic_models", anthropic_fetch),
    ):
        models = await model_catalog._fetch_claude_cli_models()

    assert models == ["claude-fable-5"]
    anthropic_fetch.assert_awaited_once_with(
        api_key="acme-sandbox",  # pragma: allowlist secret
        base_url=None,
    )


@pytest.mark.asyncio
async def test_fetch_claude_cli_models_undetectable_backend(tmp_path):
    """Vertex / keyless gateway: nothing to query -> static fallback."""
    env_file = tmp_path / "sandbox.env"
    env_file.write_text("CLAUDE_CODE_USE_VERTEX=1\n")
    with patch.object(
        model_catalog,
        "get_settings",
        return_value=_settings(
            ai_provider="claude-cli",
            sandbox_enabled=True,
            sandbox_env_file=str(env_file),
        ),
    ):
        assert await model_catalog._fetch_claude_cli_models() is None


@pytest.mark.asyncio
async def test_refresh_catalog_ttl_noop():
    fetch = AsyncMock(return_value=["claude-x"])
    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(model_catalog, "_fetch_anthropic_models", fetch),
    ):
        await refresh_catalog(force=True)
        await refresh_catalog()  # fresh -> no second fetch
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_fetch_bedrock_models_parses_profiles_and_on_demand():
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "eu.anthropic.claude-opus-4-8"},
                {"inferenceProfileId": "eu.meta.llama-x"},
            ]
        },
        "list-foundation-models": {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                },
                {
                    "modelId": "anthropic.claude-profile-only",
                    "inferenceTypesSupported": ["INFERENCE_PROFILE"],
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models()

    assert models == [
        "eu.anthropic.claude-opus-4-8",
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
    ]


def test_selectable_models_intersects_with_catalog():
    model_catalog._live_models = ["claude-a", "claude-b", "claude-c"]
    with patch.object(model_catalog, "get_enabled_models", return_value=["claude-c", "claude-a"]):
        assert selectable_models() == ["claude-a", "claude-c"]


def test_selectable_models_stale_enabled_falls_back_to_default_only():
    """Provider switch: a stale enabled list must NOT re-expose the full
    catalog (least privilege) — only the default model is offered."""
    model_catalog._live_models = ["anthropic.claude-x", "anthropic.claude-y"]
    with (
        patch.object(model_catalog, "get_enabled_models", return_value=["claude-opus-4-8"]),
        patch.object(model_catalog, "get_model_override", return_value=None),
    ):
        assert selectable_models() == ["anthropic.claude-x"]


def test_default_model_honors_valid_override():
    model_catalog._live_models = ["claude-opus-4-7", "claude-opus-4-8"]
    with patch.object(model_catalog, "get_model_override", return_value="claude-opus-4-7"):
        assert default_model() == "claude-opus-4-7"


def test_default_model_ignores_stale_override():
    """Provider switch: an anthropic-ID override must not be sent to Bedrock —
    the newest catalog model applies instead."""
    model_catalog._live_models = ["anthropic.claude-3-5-sonnet-20241022-v2:0"]
    with patch.object(model_catalog, "get_model_override", return_value="claude-opus-4-8"):
        assert default_model() == "anthropic.claude-3-5-sonnet-20241022-v2:0"


def test_default_model_unset_is_newest_catalog_model():
    """Zero-config: no DB row, no env var — the newest catalog model wins."""
    model_catalog._live_models = None  # static fallback (pricing table, newest first)
    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(model_catalog, "get_model_override", return_value=None),
    ):
        assert default_model() == "claude-fable-5"


def test_static_catalog_claude_cli_declared_models():
    """CLAUDE_CLI_MODELS declares what the CLI's backend serves."""
    with patch.object(
        model_catalog,
        "get_settings",
        return_value=_settings(
            ai_provider="claude-cli",
            claude_cli_models="eu.anthropic.claude-fable-5, eu.anthropic.claude-opus-4-8",
        ),
    ):
        assert static_catalog() == [
            "eu.anthropic.claude-fable-5",
            "eu.anthropic.claude-opus-4-8",
        ]


@pytest.mark.asyncio
async def test_fetch_bedrock_models_dedupes_and_sorts():
    """Regional > global > on-demand for the same model; newest first."""
    responses = {
        "list-inference-profiles": {
            "inferenceProfileSummaries": [
                {"inferenceProfileId": "global.anthropic.claude-sonnet-4-5-20250929-v1:0"},
                {"inferenceProfileId": "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"},
                {"inferenceProfileId": "global.anthropic.claude-fable-5-20260301-v1:0"},
            ]
        },
        "list-foundation-models": {
            "modelSummaries": [
                {
                    "modelId": "anthropic.claude-sonnet-4-5-20250929-v1:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                },
                {
                    "modelId": "anthropic.claude-3-haiku-20240307-v1:0",
                    "inferenceTypesSupported": ["ON_DEMAND"],
                },
            ]
        },
    }

    async def fake_aws_json(args, extra_env=None):
        return responses[args[1]]

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings(ai_provider="bedrock")),
        patch.object(model_catalog, "_aws_json", side_effect=fake_aws_json),
    ):
        models = await model_catalog._fetch_bedrock_models()

    assert models == [
        # Newest first; the eu. profile wins over global./on-demand duplicates
        "global.anthropic.claude-fable-5-20260301-v1:0",
        "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "anthropic.claude-3-haiku-20240307-v1:0",
    ]


@pytest.mark.asyncio
async def test_fetch_anthropic_models_sorts_newest_first():
    """The API's ordering is not contractual — default_model() needs newest-first."""

    class FakeModel:
        def __init__(self, model_id):
            self.id = model_id

    class FakePage:
        def __aiter__(self):
            async def gen():
                # Deliberately oldest-first
                for m in ["claude-haiku-4-5", "claude-opus-4-8", "claude-fable-5"]:
                    yield FakeModel(m)

            return gen()

    fake_client = MagicMock()
    fake_client.models.list = MagicMock(return_value=FakePage())
    fake_client.close = AsyncMock()

    with (
        patch.object(model_catalog, "get_settings", return_value=_settings()),
        patch.object(model_catalog, "AsyncAnthropic", return_value=fake_client),
    ):
        models = await model_catalog._fetch_anthropic_models()

    assert models == ["claude-fable-5", "claude-opus-4-8", "claude-haiku-4-5"]


def test_default_model_fallback_stays_within_enabled_subset():
    """A retired saved default must fall back to the newest ENABLED model,
    not the newest catalog model (which the admin may have disabled)."""
    model_catalog._live_models = ["claude-fable-5", "claude-opus-4-8", "claude-haiku-4-5"]
    with (
        patch.object(model_catalog, "get_model_override", return_value="claude-gone-9"),
        patch.object(model_catalog, "get_enabled_models", return_value=["claude-haiku-4-5"]),
    ):
        assert default_model() == "claude-haiku-4-5"
