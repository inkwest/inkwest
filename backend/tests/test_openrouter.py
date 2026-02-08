"""
Tests for OpenRouter orchestrator.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestOpenRouterPricing:
    """Tests for OpenRouter model pricing."""

    def test_anthropic_models(self):
        """Should have pricing for Anthropic models via OpenRouter."""
        from app.services.pricing import get_model_pricing

        pricing = get_model_pricing("anthropic/claude-sonnet-4")
        assert pricing.input_price > 0
        assert pricing.output_price > 0

        pricing = get_model_pricing("anthropic/claude-opus-4")
        assert pricing.input_price > 0
        assert pricing.output_price > 0

    def test_openai_models(self):
        """Should have pricing for OpenAI models via OpenRouter."""
        from app.services.pricing import get_model_pricing

        pricing = get_model_pricing("openai/gpt-4o")
        assert pricing.input_price > 0
        assert pricing.output_price > 0

        pricing = get_model_pricing("openai/gpt-4o-mini")
        assert pricing.input_price > 0
        assert pricing.output_price > 0

    def test_google_models(self):
        """Should have pricing for Google models via OpenRouter."""
        from app.services.pricing import get_model_pricing

        pricing = get_model_pricing("google/gemini-pro-1.5")
        assert pricing.input_price > 0
        assert pricing.output_price > 0

    def test_meta_models(self):
        """Should have pricing for Meta models via OpenRouter."""
        from app.services.pricing import get_model_pricing

        pricing = get_model_pricing("meta-llama/llama-3.1-405b-instruct")
        assert pricing.input_price > 0
        assert pricing.output_price > 0

    def test_mistral_models(self):
        """Should have pricing for Mistral models via OpenRouter."""
        from app.services.pricing import get_model_pricing

        pricing = get_model_pricing("mistralai/mistral-large")
        assert pricing.input_price > 0
        assert pricing.output_price > 0


class TestOpenRouterConfig:
    """Tests for OpenRouter configuration."""

    def test_openrouter_in_provider_literal(self):
        """OpenRouter should be a valid AI provider."""
        from app.core.config import Settings

        # Should not raise
        settings = Settings(ai_provider="openrouter")
        assert settings.ai_provider == "openrouter"

    def test_default_openrouter_settings(self):
        """Should have sensible defaults for OpenRouter settings."""
        from app.core.config import Settings

        settings = Settings()
        assert settings.openrouter_base_url == "https://openrouter.ai/api/v1"
        assert settings.openrouter_model == "anthropic/claude-sonnet-4"
        assert settings.openrouter_app_name == "Inkwest"


class TestOpenRouterModelName:
    """Tests for model name resolution."""

    def test_get_current_model_name_openrouter(self):
        """Should return OpenRouter model name."""
        with patch("app.services.pricing.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                ai_provider="openrouter",
                openrouter_model="anthropic/claude-opus-4",
            )

            from app.services.pricing import get_current_model_name

            assert get_current_model_name() == "anthropic/claude-opus-4"


class TestOpenRouterValidation:
    """Tests for OpenRouter configuration validation."""

    def test_validate_settings_openrouter_missing_key(self):
        """Should fail validation when OpenRouter API key is missing."""
        from app.core.config import validate_settings

        with patch("app.core.config.get_settings") as mock_get:
            mock_get.return_value = MagicMock(
                ai_provider="openrouter",
                openrouter_api_key=None,
            )

            with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required"):
                validate_settings()


class TestOpenRouterSystemMessage:
    """Tests for system message generation."""

    def test_build_system_message(self):
        """Should build appropriate system message via get_system_prompt."""
        from app.services.prompts import get_system_prompt

        message = get_system_prompt("my-cluster")

        assert "my-cluster" in message
        assert "Kubernetes" in message or "kubernetes" in message.lower()
        assert "kubectl" in message


class TestOpenRouterClient:
    """Tests for OpenRouter client configuration."""

    def test_client_requires_api_key(self):
        """Should raise error when API key is missing."""
        with patch("app.services.orchestrator_openrouter.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                openrouter_api_key=None,
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_site_url=None,
                frontend_url="http://localhost:3000",
                openrouter_app_name="Inkwest",
            )

            from app.services.orchestrator_openrouter import get_openrouter_client

            with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required"):
                get_openrouter_client()

    def test_client_with_valid_config(self):
        """Should create client with valid configuration."""
        with patch("app.services.orchestrator_openrouter.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                openrouter_api_key="sk-or-test-key",
                openrouter_base_url="https://openrouter.ai/api/v1",
                openrouter_site_url="https://example.com",
                frontend_url="http://localhost:3000",
                openrouter_app_name="Inkwest",
            )

            from app.services.orchestrator_openrouter import get_openrouter_client

            client = get_openrouter_client()
            assert client is not None
            assert client.api_key == "sk-or-test-key"


class TestOpenRouterOrchestatorSelection:
    """Tests for orchestrator selection."""

    def test_get_orchestrator_openrouter(self):
        """Should return OpenRouter orchestrator when configured."""
        with patch("app.services.ai_client.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(ai_provider="openrouter")

            from app.services.ai_client import get_orchestrator

            orchestrator = get_orchestrator()
            assert orchestrator.__name__ == "orchestrate_chat_openrouter"
