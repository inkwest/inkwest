"""
Tests for AI chat orchestrator.

Tests the tool-use loop including:
- Simple text responses
- Single tool calls
- Multiple sequential tool calls
- Parallel tool calls
- Error handling
- Max iterations limit
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import (
    make_response,
    make_text_block,
    make_tool_use_block,
)


class TestOrchestrateChat:
    """Tests for orchestrate_chat function."""

    @pytest.mark.asyncio
    async def test_simple_text_response(
        self,
        mock_db,
        session_id,
        cluster_context,
        simple_text_response,
        mock_messages_empty,
        mock_save_message,
        mock_ensure_title,
    ):
        """Test simple response with no tool use."""
        from app.services.orchestrator import orchestrate_chat

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=simple_text_response)

        with patch("app.services.orchestrator.get_ai_client", return_value=mock_client):
            events = []
            async for event in orchestrate_chat(
                db=mock_db, session_id=session_id, user_message="Hello", **cluster_context
            ):
                events.append(event)

        # Should have text event and done event
        event_types = [e.type for e in events]
        assert "text_delta" in event_types
        assert "done" in event_types

        # Verify text content
        text_events = [e for e in events if e.type == "text_delta"]
        assert text_events[0].content == "Here's a summary of your cluster."

    @pytest.mark.asyncio
    async def test_retry_replays_trailing_user_message_without_persisting(
        self,
        mock_db,
        session_id,
        cluster_context,
        simple_text_response,
        mock_save_message,
        mock_ensure_title,
    ):
        """On retry (persist_user_message=False) the user message is already the
        trailing history row: it must not be re-saved or duplicated in the payload."""
        from app.services.orchestrator import orchestrate_chat

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=simple_text_response)

        history = [{"role": "user", "content": "analyze the cluster"}]
        with (
            patch("app.services.orchestrator.get_ai_client", return_value=mock_client),
            patch(
                "app.services.orchestrator.get_session_messages",
                AsyncMock(return_value=history),
            ),
        ):
            async for _ in orchestrate_chat(
                db=mock_db,
                session_id=session_id,
                user_message="analyze the cluster",
                persist_user_message=False,
                **cluster_context,
            ):
                pass

        # No user-role save (the assistant reply is still persisted).
        user_saves = [
            c for c in mock_save_message.call_args_list if len(c.args) >= 3 and c.args[2] == "user"
        ]
        assert user_saves == []

        # The API payload replays the existing history — user message appears once.
        sent = mock_client.messages.create.call_args.kwargs["messages"]
        assert [m for m in sent if m["role"] == "user"] == history

    @pytest.mark.asyncio
    async def test_single_tool_call(
        self,
        mock_db,
        session_id,
        cluster_context,
        single_tool_response,
        mock_messages_empty,
        mock_save_message,
        mock_ensure_title,
        mock_kubectl_success,
    ):
        """Test response with single tool call."""
        from app.services.orchestrator import orchestrate_chat

        tool_response, final_response = single_tool_response

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[tool_response, final_response])

        with patch("app.services.orchestrator.get_ai_client", return_value=mock_client):
            events = []
            async for event in orchestrate_chat(
                db=mock_db,
                session_id=session_id,
                user_message="Show me the pods",
                **cluster_context,
            ):
                events.append(event)

        event_types = [e.type for e in events]

        # Should have: text_delta, tool_start, tool_result, text_delta, done
        assert "text_delta" in event_types
        assert "tool_start" in event_types
        assert "tool_result" in event_types
        assert "done" in event_types

        # Verify tool events
        tool_start = next(e for e in events if e.type == "tool_start")
        assert tool_start.tool_name == "kubectl_get"
        assert tool_start.tool_input["resource"] == "pods"

        tool_result = next(e for e in events if e.type == "tool_result")
        assert tool_result.is_error is False

    @pytest.mark.asyncio
    async def test_multi_tool_sequence(
        self,
        mock_db,
        session_id,
        cluster_context,
        multi_tool_response,
        mock_messages_empty,
        mock_save_message,
        mock_ensure_title,
        mock_kubectl_success,
    ):
        """Test response with multiple sequential tool calls."""
        from app.services.orchestrator import orchestrate_chat

        first, second, final = multi_tool_response

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[first, second, final])

        with patch("app.services.orchestrator.get_ai_client", return_value=mock_client):
            events = []
            async for event in orchestrate_chat(
                db=mock_db,
                session_id=session_id,
                user_message="Analyze my cluster",
                **cluster_context,
            ):
                events.append(event)

        # Count tool calls
        tool_starts = [e for e in events if e.type == "tool_start"]
        tool_results = [e for e in events if e.type == "tool_result"]

        assert len(tool_starts) == 2
        assert len(tool_results) == 2
        assert tool_starts[0].tool_name == "kubectl_get"
        assert tool_starts[1].tool_name == "kubectl_events"

    @pytest.mark.asyncio
    async def test_parallel_tools(
        self,
        mock_db,
        session_id,
        cluster_context,
        parallel_tools_response,
        mock_messages_empty,
        mock_save_message,
        mock_ensure_title,
        mock_kubectl_success,
    ):
        """Test response with parallel tool calls."""
        from app.services.orchestrator import orchestrate_chat

        tool_response, final_response = parallel_tools_response

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[tool_response, final_response])

        with patch("app.services.orchestrator.get_ai_client", return_value=mock_client):
            events = []
            async for event in orchestrate_chat(
                db=mock_db,
                session_id=session_id,
                user_message="Get pods and services",
                **cluster_context,
            ):
                events.append(event)

        # Should have 2 tool starts and 2 results (parallel)
        tool_starts = [e for e in events if e.type == "tool_start"]
        tool_results = [e for e in events if e.type == "tool_result"]

        assert len(tool_starts) == 2
        assert len(tool_results) == 2

    @pytest.mark.asyncio
    async def test_tool_error_handling(
        self,
        mock_db,
        session_id,
        cluster_context,
        single_tool_response,
        mock_messages_empty,
        mock_save_message,
        mock_ensure_title,
        mock_kubectl_error,
    ):
        """Test handling of tool execution errors."""
        from app.services.orchestrator import orchestrate_chat

        tool_response, final_response = single_tool_response

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(side_effect=[tool_response, final_response])

        with patch("app.services.orchestrator.get_ai_client", return_value=mock_client):
            events = []
            async for event in orchestrate_chat(
                db=mock_db,
                session_id=session_id,
                user_message="Show me the pods",
                **cluster_context,
            ):
                events.append(event)

        # Tool result should indicate error
        tool_result = next(e for e in events if e.type == "tool_result")
        assert tool_result.is_error is True
        assert "not found" in tool_result.content

    @pytest.mark.asyncio
    async def test_max_iterations_limit(
        self,
        mock_db,
        session_id,
        cluster_context,
        mock_messages_empty,
        mock_save_message,
        mock_ensure_title,
        mock_kubectl_success,
    ):
        """Test that max iterations limit is enforced."""
        from app.core.constants import MAX_ORCHESTRATION_ITERATIONS
        from app.services.orchestrator import orchestrate_chat

        # Create response that always requests a tool (infinite loop scenario)
        infinite_tool_response = make_response(
            content=[
                make_text_block("Checking..."),
                make_tool_use_block("toolu_01", "kubectl_get", {"resource": "pods"}),
            ],
            stop_reason="tool_use",
        )

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=infinite_tool_response)

        with patch("app.services.orchestrator.get_ai_client", return_value=mock_client):
            events = []
            async for event in orchestrate_chat(
                db=mock_db, session_id=session_id, user_message="Loop forever", **cluster_context
            ):
                events.append(event)

        # Should end with error about max iterations
        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "maximum iterations" in error_events[0].content.lower()

        # Verify we hit the limit
        tool_starts = [e for e in events if e.type == "tool_start"]
        assert len(tool_starts) == MAX_ORCHESTRATION_ITERATIONS

    @pytest.mark.asyncio
    async def test_ai_client_error(
        self,
        mock_db,
        session_id,
        cluster_context,
        mock_messages_empty,
        mock_save_message,
        mock_ensure_title,
    ):
        """Test handling of AI client initialization error."""
        from app.services.orchestrator import orchestrate_chat

        with patch(
            "app.services.orchestrator.get_ai_client",
            side_effect=ValueError("API key not configured"),
        ):
            events = []
            async for event in orchestrate_chat(
                db=mock_db, session_id=session_id, user_message="Hello", **cluster_context
            ):
                events.append(event)

        # Should get error event
        assert len(events) == 1
        assert events[0].type == "error"
        assert "unavailable" in events[0].content.lower()

    @pytest.mark.asyncio
    async def test_rate_limit_error(
        self,
        mock_db,
        session_id,
        cluster_context,
        mock_messages_empty,
        mock_save_message,
        mock_ensure_title,
    ):
        """Test handling of rate limit errors."""
        from anthropic import RateLimitError

        from app.services.orchestrator import orchestrate_chat

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=RateLimitError(
                message="Rate limit exceeded", response=MagicMock(status_code=429), body={}
            )
        )

        with patch("app.services.orchestrator.get_ai_client", return_value=mock_client):
            events = []
            async for event in orchestrate_chat(
                db=mock_db, session_id=session_id, user_message="Hello", **cluster_context
            ):
                events.append(event)

        # Should get rate limit error
        error_events = [e for e in events if e.type == "error"]
        assert len(error_events) == 1
        assert "rate limit" in error_events[0].content.lower()


class TestCallClaudeWithRetry:
    """Tests for retry logic."""

    @pytest.mark.asyncio
    async def test_successful_call(self, simple_text_response):
        """Test successful API call without retry."""
        from app.services.orchestrator import call_claude_with_retry

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=simple_text_response)

        result = await call_claude_with_retry(
            mock_client,
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            messages=[{"role": "user", "content": "test"}],
        )

        assert result == simple_text_response
        assert mock_client.messages.create.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_rate_limit(self, simple_text_response):
        """Test retry on rate limit error."""
        from anthropic import RateLimitError

        from app.services.orchestrator import call_claude_with_retry

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=[
                RateLimitError(message="Rate limit", response=MagicMock(status_code=429), body={}),
                simple_text_response,
            ]
        )

        with patch("app.core.retry.asyncio.sleep", new_callable=AsyncMock):
            result = await call_claude_with_retry(
                mock_client,
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                messages=[{"role": "user", "content": "test"}],
            )

        assert result == simple_text_response
        assert mock_client.messages.create.call_count == 2


class TestEventTypes:
    """Tests for event type structure (using dataclass attributes)."""

    def test_text_event_structure(self):
        """Test text event has correct structure."""
        from app.services.events import text_event

        event = text_event("Hello world")
        assert event.type == "text_delta"
        assert event.content == "Hello world"

    def test_tool_start_event_structure(self):
        """Test tool_start event has correct structure."""
        from app.services.events import tool_start_event

        event = tool_start_event("kubectl_get", {"resource": "pods"}, "toolu_123")
        assert event.type == "tool_start"
        assert event.tool_name == "kubectl_get"
        assert event.tool_input == {"resource": "pods"}
        assert event.tool_use_id == "toolu_123"

    def test_tool_result_event_structure(self):
        """Test tool_result event has correct structure."""
        from app.services.events import tool_result_event

        event = tool_result_event("kubectl_get", "pod1  Running", "toolu_123", False)
        assert event.type == "tool_result"
        assert event.tool_name == "kubectl_get"
        assert event.content == "pod1  Running"
        assert event.tool_use_id == "toolu_123"
        assert event.is_error is False

    def test_done_event_structure(self):
        """Test done event has correct structure."""
        from app.services.events import done_event

        event = done_event(metadata={"iterations": 2})
        assert event.type == "done"
        assert event.metadata["iterations"] == 2

    def test_error_event_structure(self):
        """Test error event has correct structure."""
        from app.services.events import error_event

        event = error_event("Something went wrong")
        assert event.type == "error"
        assert event.content == "Something went wrong"
        assert event.is_error is True

    def test_event_to_dict(self):
        """Test event serialization to dict."""
        from app.services.events import text_event

        event = text_event("Hello")
        d = event.to_dict()
        assert d["type"] == "text_delta"
        assert d["content"] == "Hello"

    def test_event_to_json(self):
        """Test event serialization to JSON."""
        import json

        from app.services.events import done_event

        event = done_event(metadata={"tokens": 100})
        j = event.to_json()
        parsed = json.loads(j)
        assert parsed["type"] == "done"
        assert parsed["metadata"]["tokens"] == 100
