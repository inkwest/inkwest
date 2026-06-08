"""
Tests for Azure OpenAI orchestrator.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.orchestrator_azure import (
    convert_messages_to_openai_format,
    convert_tools_to_openai_format,
    orchestrate_chat_azure,
)
from app.tools.definitions import KUBECTL_TOOLS


class TestConvertToolsToOpenAIFormat:
    """Tests for Anthropic to OpenAI tool format conversion."""

    def test_converts_basic_tool(self):
        """Should convert a basic Anthropic tool to OpenAI format."""
        anthropic_tool = {
            "name": "test_tool",
            "description": "A test tool",
            "input_schema": {
                "type": "object",
                "properties": {"param1": {"type": "string", "description": "A parameter"}},
                "required": ["param1"],
            },
        }

        openai_tools = convert_tools_to_openai_format([anthropic_tool])

        assert len(openai_tools) == 1
        assert openai_tools[0]["type"] == "function"
        assert openai_tools[0]["function"]["name"] == "test_tool"
        assert openai_tools[0]["function"]["description"] == "A test tool"
        assert openai_tools[0]["function"]["parameters"]["type"] == "object"
        assert "param1" in openai_tools[0]["function"]["parameters"]["properties"]

    def test_converts_all_kubectl_tools(self):
        """Should convert all kubectl tools without error."""
        openai_tools = convert_tools_to_openai_format(KUBECTL_TOOLS)

        assert len(openai_tools) == len(KUBECTL_TOOLS)
        for tool in openai_tools:
            assert tool["type"] == "function"
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]

    def test_preserves_required_fields(self):
        """Should preserve required fields in schema."""
        anthropic_tool = {
            "name": "kubectl_get",
            "description": "Get resources",
            "input_schema": {
                "type": "object",
                "properties": {"resource": {"type": "string"}},
                "required": ["resource"],
            },
        }

        openai_tools = convert_tools_to_openai_format([anthropic_tool])
        assert openai_tools[0]["function"]["parameters"]["required"] == ["resource"]


class TestConvertMessagesToOpenAIFormat:
    """Tests for Anthropic to OpenAI message format conversion."""

    def test_converts_simple_user_message(self):
        """Should convert simple user message."""
        messages = [{"role": "user", "content": "Hello"}]
        result = convert_messages_to_openai_format(messages)

        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "Hello"

    def test_converts_simple_assistant_message(self):
        """Should convert simple assistant message."""
        messages = [{"role": "assistant", "content": "Hi there!"}]
        result = convert_messages_to_openai_format(messages)

        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Hi there!"

    def test_converts_assistant_message_with_text_blocks(self):
        """Should convert assistant message with text content blocks."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "First part."},
                    {"type": "text", "text": "Second part."},
                ],
            }
        ]
        result = convert_messages_to_openai_format(messages)

        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert "First part." in result[0]["content"]
        assert "Second part." in result[0]["content"]

    def test_converts_assistant_message_with_tool_use(self):
        """Should convert assistant message with tool use blocks."""
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check that."},
                    {
                        "type": "tool_use",
                        "id": "tool_123",
                        "name": "kubectl_get",
                        "input": {"resource": "pods"},
                    },
                ],
            }
        ]
        result = convert_messages_to_openai_format(messages)

        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Let me check that."
        assert len(result[0]["tool_calls"]) == 1
        assert result[0]["tool_calls"][0]["id"] == "tool_123"
        assert result[0]["tool_calls"][0]["type"] == "function"
        assert result[0]["tool_calls"][0]["function"]["name"] == "kubectl_get"
        assert json.loads(result[0]["tool_calls"][0]["function"]["arguments"]) == {
            "resource": "pods"
        }

    def test_converts_tool_results(self):
        """Should convert tool results to tool messages."""
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_123",
                        "content": "NAME   READY   STATUS\npod1   1/1     Running",
                        "is_error": False,
                    }
                ],
            }
        ]
        result = convert_messages_to_openai_format(messages)

        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tool_123"
        assert "pod1" in result[0]["content"]

    def test_converts_multiple_tool_results(self):
        """Should convert multiple tool results to separate tool messages."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool_1", "content": "Result 1"},
                    {"type": "tool_result", "tool_use_id": "tool_2", "content": "Result 2"},
                ],
            }
        ]
        result = convert_messages_to_openai_format(messages)

        assert len(result) == 2
        assert all(r["role"] == "tool" for r in result)
        assert result[0]["tool_call_id"] == "tool_1"
        assert result[1]["tool_call_id"] == "tool_2"

    def test_handles_full_conversation(self):
        """Should handle a complete conversation with tools."""
        messages = [
            {"role": "user", "content": "List all pods"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I'll list the pods for you."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "kubectl_get",
                        "input": {"resource": "pods"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "pod1\npod2"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Here are your pods: pod1 and pod2."}],
            },
        ]

        result = convert_messages_to_openai_format(messages)

        assert len(result) == 4
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert "tool_calls" in result[1]
        assert result[2]["role"] == "tool"
        assert result[3]["role"] == "assistant"


class TestOrchestrateAzureChatLoop:
    """Tests for Azure orchestration loop."""

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        # "nothing found" so resolve_session_system_prompt falls back to the
        # hardcoded default instead of returning an AsyncMock.
        db.get = AsyncMock(return_value=None)
        db.scalar = AsyncMock(return_value=None)
        return db

    @pytest.fixture
    def mock_azure_client(self):
        """Create a mock Azure OpenAI client."""
        return MagicMock()

    @pytest.fixture
    def mock_settings(self):
        """Create mock settings for Azure."""
        settings = MagicMock()
        settings.ai_provider = "azure"
        settings.azure_openai_endpoint = "https://test.openai.azure.com"
        settings.azure_openai_api_key = "test-key"
        settings.azure_openai_deployment = "gpt-5-codex"
        settings.azure_openai_api_version = "2024-12-01-preview"
        return settings

    def make_azure_response(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        finish_reason: str = "stop",
    ) -> MagicMock:
        """Create a mock Azure OpenAI response."""
        response = MagicMock()
        choice = MagicMock()
        message = MagicMock()

        message.content = content
        message.tool_calls = tool_calls

        choice.message = message
        choice.finish_reason = finish_reason

        response.choices = [choice]
        response.usage = MagicMock()
        response.usage.prompt_tokens = 100
        response.usage.completion_tokens = 50

        return response

    def make_tool_call(self, tool_id: str, name: str, arguments: dict) -> MagicMock:
        """Create a mock tool call."""
        tool_call = MagicMock()
        tool_call.id = tool_id
        tool_call.function = MagicMock()
        tool_call.function.name = name
        tool_call.function.arguments = json.dumps(arguments)
        return tool_call

    @pytest.mark.asyncio
    async def test_simple_text_response(self, mock_db, mock_azure_client, mock_settings):
        """Should handle simple text response without tools."""
        response = self.make_azure_response(content="Hello! How can I help?")

        mock_azure_client.chat.completions.create = AsyncMock(return_value=response)

        with patch("app.services.orchestrator_azure.get_settings", return_value=mock_settings):
            with patch(
                "app.services.orchestrator_azure.get_azure_client", return_value=mock_azure_client
            ):
                with patch(
                    "app.services.orchestrator_azure.get_session_messages",
                    new_callable=AsyncMock,
                    return_value=[],
                ):
                    with patch(
                        "app.services.orchestrator_azure.save_message", new_callable=AsyncMock
                    ):
                        with patch(
                            "app.services.orchestrator_azure.ensure_session_title",
                            new_callable=AsyncMock,
                        ):
                            events = []
                            async for event in orchestrate_chat_azure(
                                mock_db,
                                "12345678-1234-5678-1234-567812345678",
                                "test-cluster",
                                "/path/to/kubeconfig",
                                None,
                                "Hello",
                            ):
                                events.append(event)

        event_types = [e.type for e in events]
        assert "text_delta" in event_types
        assert "done" in event_types

        text_events = [e for e in events if e.type == "text_delta"]
        assert any("Hello" in e.content for e in text_events)

    @pytest.mark.asyncio
    async def test_tool_use_response(self, mock_db, mock_azure_client, mock_settings):
        """Should handle tool use and execute kubectl."""
        tool_call = self.make_tool_call(
            "call_123", "kubectl_get", {"resource": "pods", "namespace": "default"}
        )
        tool_response = self.make_azure_response(
            content="Let me check the pods.", tool_calls=[tool_call], finish_reason="tool_calls"
        )
        final_response = self.make_azure_response(
            content="Found 2 pods running.", finish_reason="stop"
        )

        mock_azure_client.chat.completions.create = AsyncMock(
            side_effect=[tool_response, final_response]
        )

        with patch("app.services.orchestrator_azure.get_settings", return_value=mock_settings):
            with patch(
                "app.services.orchestrator_azure.get_azure_client", return_value=mock_azure_client
            ):
                with patch(
                    "app.services.orchestrator_azure.get_session_messages",
                    new_callable=AsyncMock,
                    return_value=[],
                ):
                    with patch(
                        "app.services.orchestrator_azure.save_message", new_callable=AsyncMock
                    ):
                        with patch(
                            "app.services.orchestrator_azure.ensure_session_title",
                            new_callable=AsyncMock,
                        ):
                            with patch(
                                "app.tools.dispatch.execute_kubectl",
                                new_callable=AsyncMock,
                            ) as mock_kubectl:
                                mock_kubectl.return_value = ("pod1\npod2", False)

                                events = []
                                async for event in orchestrate_chat_azure(
                                    mock_db,
                                    "12345678-1234-5678-1234-567812345678",
                                    "test-cluster",
                                    "/path/to/kubeconfig",
                                    None,
                                    "Show me all pods",
                                ):
                                    events.append(event)

        event_types = [e.type for e in events]
        assert "tool_start" in event_types
        assert "tool_result" in event_types
        assert "done" in event_types

        # Verify kubectl was called
        mock_kubectl.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_tool_execution_error(self, mock_db, mock_azure_client, mock_settings):
        """Should handle tool execution errors gracefully."""
        tool_call = self.make_tool_call("call_456", "kubectl_get", {"resource": "pods"})
        tool_response = self.make_azure_response(
            content="Checking pods.", tool_calls=[tool_call], finish_reason="tool_calls"
        )
        final_response = self.make_azure_response(
            content="There was an error getting the pods.", finish_reason="stop"
        )

        mock_azure_client.chat.completions.create = AsyncMock(
            side_effect=[tool_response, final_response]
        )

        with patch("app.services.orchestrator_azure.get_settings", return_value=mock_settings):
            with patch(
                "app.services.orchestrator_azure.get_azure_client", return_value=mock_azure_client
            ):
                with patch(
                    "app.services.orchestrator_azure.get_session_messages",
                    new_callable=AsyncMock,
                    return_value=[],
                ):
                    with patch(
                        "app.services.orchestrator_azure.save_message", new_callable=AsyncMock
                    ):
                        with patch(
                            "app.services.orchestrator_azure.ensure_session_title",
                            new_callable=AsyncMock,
                        ):
                            with patch(
                                "app.tools.dispatch.execute_kubectl",
                                new_callable=AsyncMock,
                            ) as mock_kubectl:
                                mock_kubectl.side_effect = Exception("Connection refused")

                                events = []
                                async for event in orchestrate_chat_azure(
                                    mock_db,
                                    "12345678-1234-5678-1234-567812345678",
                                    "test-cluster",
                                    "/path/to/kubeconfig",
                                    None,
                                    "Show me all pods",
                                ):
                                    events.append(event)

        # Should complete without crashing
        event_types = [e.type for e in events]
        assert "done" in event_types

        # Tool result should show error
        tool_results = [e for e in events if e.type == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0].is_error is True
