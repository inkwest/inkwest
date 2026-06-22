"""
Tests for AI client factory and configuration.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ai_client import (
    AIClientError,
    get_ai_client,
    get_model_name,
    get_orchestrator,
)


class TestGetAIClient:
    """Tests for get_ai_client factory function."""

    def test_anthropic_provider_requires_api_key(self):
        """Should raise error when Anthropic API key is missing."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "anthropic"
        mock_settings.anthropic_api_key = None

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            with pytest.raises(AIClientError) as exc_info:
                get_ai_client()
            assert "ANTHROPIC_API_KEY is required" in str(exc_info.value)

    def test_anthropic_provider_creates_client(self):
        """Should create AsyncAnthropic client when API key is provided."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "anthropic"
        mock_settings.anthropic_api_key = "test-key"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            with patch("app.services.ai_client.AsyncAnthropic") as mock_anthropic:
                mock_anthropic.return_value = MagicMock()
                _client = get_ai_client()
                mock_anthropic.assert_called_once_with(api_key="test-key")

    def test_bedrock_provider_requires_region(self):
        """Should raise error when AWS region is missing for Bedrock."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "bedrock"
        mock_settings.aws_region = None

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            with pytest.raises(AIClientError) as exc_info:
                get_ai_client()
            assert "AWS_REGION is required" in str(exc_info.value)

    def test_bedrock_provider_creates_client_with_region_only(self):
        """Should create Bedrock client with just region (uses default credentials)."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "bedrock"
        mock_settings.aws_region = "us-east-1"
        mock_settings.aws_access_key_id = None
        mock_settings.aws_secret_access_key = None

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            with patch("app.services.ai_client.AsyncAnthropicBedrock") as mock_bedrock:
                mock_bedrock.return_value = MagicMock()
                _client = get_ai_client()
                mock_bedrock.assert_called_once_with(aws_region="us-east-1")

    def test_bedrock_provider_creates_client_with_explicit_credentials(self):
        """Should create Bedrock client with explicit AWS credentials."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "bedrock"
        mock_settings.aws_region = "us-west-2"
        mock_settings.aws_access_key_id = "AKIAIOSFODNN7EXAMPLE"
        mock_settings.aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            with patch("app.services.ai_client.AsyncAnthropicBedrock") as mock_bedrock:
                mock_bedrock.return_value = MagicMock()
                _client = get_ai_client()
                mock_bedrock.assert_called_once_with(
                    aws_region="us-west-2",
                    aws_access_key="AKIAIOSFODNN7EXAMPLE",
                    aws_secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                )

    def test_unknown_provider_raises_error(self):
        """Should raise error for unknown provider."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "unknown"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            with pytest.raises(AIClientError) as exc_info:
                get_ai_client()
            assert "Unknown AI provider" in str(exc_info.value)


class TestGetModelName:
    """get_model_name delegates to model_registry.default_model (covered there)."""

    def test_delegates_to_registry_default(self):
        with patch("app.services.ai_client.default_model", return_value="claude-opus-4-8"):
            assert get_model_name() == "claude-opus-4-8"


class TestGetOrchestrator:
    """Tests for get_orchestrator function."""

    def test_anthropic_returns_api_orchestrator(self):
        """Anthropic returns a per-turn dispatcher wrapping the API orchestrator (so the
        turn carries engine+model metric labels like the other providers)."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "anthropic"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            orchestrator = get_orchestrator()
            assert orchestrator.__name__ == "_dispatch_anthropic"

    def test_bedrock_returns_dispatcher(self):
        """Bedrock returns a per-turn dispatcher (Claude vs Converse by model)."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "bedrock"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            orchestrator = get_orchestrator()
            assert orchestrator.__name__ == "_dispatch_bedrock"

    def test_claude_cli_returns_engine_dispatcher(self):
        """claude-cli returns the per-turn engine dispatcher (CLI vs sandboxed agent)."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "claude-cli"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            orchestrator = get_orchestrator()
            assert orchestrator.__name__ == "_dispatch_engine"


class TestDispatcherSetsTurnContext:
    """Regression: each dispatcher must set engine+model in the task context so deep metric
    sites (record_usage, tools) and turn_runner read the right values. A refactor that drops
    set_turn_engine/model would silently revert every label to 'unknown' with tests green."""

    @pytest.mark.asyncio
    async def test_engine_dispatcher_sets_context(self):
        import asyncio

        from app.services import ai_client, engines

        seen = {}

        async def fake_cli(db, session_id, **kwargs):
            from app.core import metrics

            seen["engine"] = metrics.turn_engine()
            seen["model"] = metrics.turn_model()
            return
            yield

        mock_settings = MagicMock()
        mock_settings.ai_provider = "claude-cli"
        mock_settings.sandbox_enabled = False
        with (
            patch.object(ai_client, "get_settings", return_value=mock_settings),
            patch.object(ai_client, "resolve_session_model", AsyncMock(return_value="claude-x")),
            patch.object(ai_client, "resolve_session_engine", AsyncMock(return_value=None)),
            patch.object(engines, "configured_default_engine", return_value=None),
            patch.object(engines, "engine_for_turn", return_value="claude-cli"),
            patch("app.services.orchestrator_cli.orchestrate_chat_cli", fake_cli),
        ):
            dispatch = ai_client.get_orchestrator()

            async def run():
                async for _ in dispatch(db=AsyncMock(), session_id="s"):
                    pass

            await asyncio.create_task(run())
        assert seen == {"engine": "claude-cli", "model": "claude-x"}

    @pytest.mark.asyncio
    async def test_anthropic_dispatcher_sets_context(self):
        import asyncio

        from app.services import ai_client

        seen = {}

        async def fake_api(db, session_id, **kwargs):
            from app.core import metrics

            seen["engine"] = metrics.turn_engine()
            seen["model"] = metrics.turn_model()
            return
            yield

        mock_settings = MagicMock()
        mock_settings.ai_provider = "anthropic"
        with (
            patch.object(ai_client, "get_settings", return_value=mock_settings),
            patch.object(ai_client, "resolve_session_model", AsyncMock(return_value="claude-y")),
            patch("app.services.orchestrator.orchestrate_chat", fake_api),
        ):
            dispatch = ai_client.get_orchestrator()

            async def run():
                async for _ in dispatch(db=AsyncMock(), session_id="s"):
                    pass

            await asyncio.create_task(run())
        assert seen == {"engine": "anthropic", "model": "claude-y"}


class TestResolveSessionModelChoice:
    """Turn-time enforcement of the admin-enabled subset."""

    @pytest.mark.asyncio
    async def test_pinned_model_honored_while_enabled(self):
        from app.services.ai_client import resolve_session_model_choice

        db = AsyncMock()
        db.scalar = AsyncMock(return_value="claude-opus-4-8")
        with patch(
            "app.services.ai_client.selectable_models",
            return_value=["claude-opus-4-8", "claude-haiku-4-5"],
        ):
            assert await resolve_session_model_choice(db, "s1") == "claude-opus-4-8"

    @pytest.mark.asyncio
    async def test_pinned_model_falls_back_when_admin_disables_it(self):
        """An admin uncheck must bite existing sessions, not just new pins."""
        from app.services.ai_client import resolve_session_model_choice

        db = AsyncMock()
        db.scalar = AsyncMock(return_value="claude-fable-5")
        with (
            patch(
                "app.services.ai_client.selectable_models",
                return_value=["claude-haiku-4-5"],
            ),
            patch("app.services.ai_client.configured_default_id", return_value=None),
        ):
            assert await resolve_session_model_choice(db, "s1") is None

    @pytest.mark.asyncio
    async def test_admin_default_must_also_be_enabled(self):
        from app.services.ai_client import resolve_session_model_choice

        db = AsyncMock()
        db.scalar = AsyncMock(return_value=None)
        with (
            patch(
                "app.services.ai_client.selectable_models",
                return_value=["claude-haiku-4-5"],
            ),
            patch(
                "app.services.ai_client.configured_default_id",
                return_value="claude-fable-5",
            ),
        ):
            assert await resolve_session_model_choice(db, "s1") is None
