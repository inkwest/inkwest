"""
Tests for AI client factory and configuration.
"""

from unittest.mock import MagicMock, patch

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
    """Tests for get_model_name function."""

    def test_anthropic_returns_configured_model(self):
        """Should return configured model for Anthropic provider."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "anthropic"
        mock_settings.anthropic_model = "claude-opus-4-20250514"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            model = get_model_name()
            assert model == "claude-opus-4-20250514"

    def test_bedrock_returns_configured_model_id(self):
        """Should return configured Bedrock model ID."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "bedrock"
        mock_settings.bedrock_model_id = "anthropic.claude-3-opus-20240229-v1:0"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            model = get_model_name()
            assert model == "anthropic.claude-3-opus-20240229-v1:0"

    def test_claude_cli_returns_anthropic_model(self):
        """Should fall back to the Anthropic model for claude-cli provider."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "claude-cli"
        mock_settings.anthropic_model = "claude-sonnet-4-20250514"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            model = get_model_name()
            assert model == "claude-sonnet-4-20250514"


class TestGetOrchestrator:
    """Tests for get_orchestrator function."""

    def test_anthropic_returns_api_orchestrator(self):
        """Should return API orchestrator for Anthropic provider."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "anthropic"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            orchestrator = get_orchestrator()
            assert orchestrator.__name__ == "orchestrate_chat"

    def test_bedrock_returns_api_orchestrator(self):
        """Should return API orchestrator for Bedrock provider."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "bedrock"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            orchestrator = get_orchestrator()
            assert orchestrator.__name__ == "orchestrate_chat"

    def test_claude_cli_returns_cli_orchestrator(self):
        """Should return CLI orchestrator for claude-cli provider."""
        mock_settings = MagicMock()
        mock_settings.ai_provider = "claude-cli"

        with patch("app.services.ai_client.get_settings", return_value=mock_settings):
            orchestrator = get_orchestrator()
            assert orchestrator.__name__ == "orchestrate_chat_cli"
