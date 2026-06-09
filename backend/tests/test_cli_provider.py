"""
Tests for Claude CLI provider.

Tests the CLI client, output parsing, and orchestrator.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.ai_cli_client import (
    execute_claude_cli,
    parse_cli_response,
    parse_cli_stream_line,
)
from app.services.events import ChatEvent


class TestParseCliStreamLine:
    """Tests for parse_cli_stream_line function."""

    def test_parse_text_event(self):
        """Test parsing a text event."""
        line = '{"type": "text", "content": "Hello"}'
        result = parse_cli_stream_line(line)
        assert result is not None
        assert result["type"] == "text"
        assert result["content"] == "Hello"

    def test_parse_tool_use_event(self):
        """Test parsing a tool use event."""
        line = '{"type": "tool_use", "name": "kubectl_get", "input": {"resource": "pods"}}'
        result = parse_cli_stream_line(line)
        assert result is not None
        assert result["type"] == "tool_use"
        assert result["name"] == "kubectl_get"
        assert result["input"]["resource"] == "pods"

    def test_parse_tool_result_event(self):
        """Test parsing a tool result event."""
        line = (
            '{"type": "tool_result", "name": "kubectl_get",'
            ' "content": "pod/nginx", "is_error": false}'
        )
        result = parse_cli_stream_line(line)
        assert result is not None
        assert result["type"] == "tool_result"
        assert result["name"] == "kubectl_get"
        assert result["content"] == "pod/nginx"
        assert result["is_error"] is False

    def test_parse_done_event(self):
        """Test parsing a done event."""
        line = '{"type": "done"}'
        result = parse_cli_stream_line(line)
        assert result is not None
        assert result["type"] == "done"

    def test_parse_message_stop_event(self):
        """Test parsing message_stop event."""
        line = '{"type": "message_stop"}'
        result = parse_cli_stream_line(line)
        assert result is not None
        assert result["type"] == "message_stop"

    def test_parse_invalid_json(self):
        """Test that invalid JSON returns None."""
        result = parse_cli_stream_line("not json")
        assert result is None

    def test_parse_empty_line(self):
        """Test that empty line returns None."""
        result = parse_cli_stream_line("")
        assert result is None

    def test_parse_status_message(self):
        """Test that status messages (non-JSON) return None."""
        result = parse_cli_stream_line("Thinking...")
        assert result is None

    def test_parse_content_block_delta(self):
        """Test parsing content block delta event."""
        line = '{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}'
        result = parse_cli_stream_line(line)
        assert result is not None
        assert result["type"] == "content_block_delta"
        assert result["delta"]["type"] == "text_delta"
        assert result["delta"]["text"] == "Hello"

    def test_parse_assistant_message(self):
        """Test parsing full assistant message."""
        line = '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}}'
        result = parse_cli_stream_line(line)
        assert result is not None
        assert result["type"] == "assistant"
        assert len(result["message"]["content"]) == 1

    def test_parse_error_event(self):
        """Test parsing error event."""
        line = '{"type": "error", "message": "Something went wrong"}'
        result = parse_cli_stream_line(line)
        assert result is not None
        assert result["type"] == "error"
        assert result["message"] == "Something went wrong"


class TestParseCliResponse:
    """Tests for parse_cli_response async generator."""

    @pytest.mark.asyncio
    async def test_parse_text_delta(self):
        """Test parsing text delta events."""

        async def mock_stream():
            yield (
                '{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello "}}'
            )
            yield (
                '{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "World"}}'
            )
            yield '{"type": "message_stop"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        assert len(events) == 3
        assert events[0].type == "text_delta"
        assert events[0].content == "Hello "
        assert events[1].type == "text_delta"
        assert events[1].content == "World"
        assert events[2].type == "done"

    @pytest.mark.asyncio
    async def test_parse_tool_use(self):
        """Test parsing tool use events."""

        async def mock_stream():
            yield (
                '{"type": "content_block_start",'
                ' "content_block": {"type": "tool_use",'
                ' "id": "123", "name": "kubectl_get"}}'
            )
            yield '{"type": "message_stop"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        assert len(events) == 2
        assert events[0].type == "tool_start"
        assert events[0].tool_name == "kubectl_get"
        assert events[0].tool_use_id == "123"

    @pytest.mark.asyncio
    async def test_parse_tool_result(self):
        """Test parsing tool result events."""

        async def mock_stream():
            yield (
                '{"type": "tool_result",'
                ' "name": "kubectl_get",'
                ' "content": "pod/nginx",'
                ' "is_error": false}'
            )
            yield '{"type": "done"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        assert len(events) == 2
        assert events[0].type == "tool_result"
        assert events[0].tool_name == "kubectl_get"
        assert events[0].content == "pod/nginx"
        assert events[0].is_error is False

    @pytest.mark.asyncio
    async def test_parse_error(self):
        """Test parsing error events."""

        async def mock_stream():
            yield '{"type": "error", "message": "API error"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        assert len(events) == 1
        assert events[0].type == "error"
        assert events[0].content == "API error"
        assert events[0].is_error is True

    @pytest.mark.asyncio
    async def test_parse_legacy_text_format(self):
        """Test parsing legacy text format."""

        async def mock_stream():
            yield '{"type": "text", "content": "Hello World"}'
            yield '{"type": "end"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        assert len(events) == 2
        assert events[0].type == "text_delta"
        assert events[0].content == "Hello World"
        assert events[1].type == "done"

    @pytest.mark.asyncio
    async def test_skip_non_json_lines(self):
        """Test that non-JSON lines are skipped."""

        async def mock_stream():
            yield "Thinking..."
            yield '{"type": "text", "content": "Hello"}'
            yield "Processing..."
            yield '{"type": "done"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        assert len(events) == 2
        assert events[0].type == "text_delta"
        assert events[1].type == "done"

    @pytest.mark.asyncio
    async def test_parse_result_event(self):
        """Test parsing result event with success subtype."""

        async def mock_stream():
            yield '{"type": "result", "result": "Analysis complete.", "subtype": "success"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        assert len(events) == 2
        assert events[0].type == "text_delta"
        assert events[0].content == "Analysis complete."
        assert events[1].type == "done"

    @pytest.mark.asyncio
    async def test_result_does_not_duplicate_assistant_text(self):
        """The `result` field repeats the assistant text — it must not be emitted twice."""

        async def mock_stream():
            yield (
                '{"type": "assistant", "message": {"content":'
                ' [{"type": "text", "text": "The answer is 42."}]}}'
            )
            yield '{"type": "result", "result": "The answer is 42.", "subtype": "success"}'

        events = [e async for e in parse_cli_response(mock_stream())]

        text_events = [e for e in events if e.type == "text_delta"]
        assert len(text_events) == 1
        assert text_events[0].content == "The answer is 42."
        assert any(e.type == "done" for e in events)

    @pytest.mark.asyncio
    async def test_parse_full_assistant_message(self):
        """Test parsing full assistant message with multiple content blocks."""

        async def mock_stream():
            message = {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Let me check the pods."},
                        {
                            "type": "tool_use",
                            "id": "tool1",
                            "name": "kubectl_get",
                            "input": {"resource": "pods"},
                        },
                    ]
                },
            }
            yield json.dumps(message)

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        assert len(events) == 2
        assert events[0].type == "text_delta"
        assert events[0].content == "Let me check the pods."
        assert events[1].type == "tool_start"
        assert events[1].tool_name == "kubectl_get"

    @pytest.mark.asyncio
    async def test_parse_session_init_event(self):
        """Test parsing system init event with CLI session ID."""

        async def mock_stream():
            yield ('{"type": "system", "subtype": "init", "session_id": "abc123-def456-ghi789"}')
            yield '{"type": "text", "content": "Hello"}'
            yield '{"type": "done"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        # Should have session_info, text_delta, and done events
        assert len(events) == 3
        assert events[0].type == "session_info"
        sid = "abc123-def456-ghi789"
        assert events[0].metadata["cli_session_id"] == sid
        assert events[1].type == "text_delta"
        assert events[2].type == "done"

    @pytest.mark.asyncio
    async def test_parse_text_with_newline_separation(self):
        """Test that multiple assistant text blocks get newline separation."""

        async def mock_stream():
            # First assistant message
            yield (
                '{"type": "assistant", "message":'
                ' {"content": [{"type": "text",'
                ' "text": "First message."}]}}'
            )
            yield (
                '{"type": "tool_result", "name": "test", "content": "result", "is_error": false}'
            )
            yield (
                '{"type": "assistant", "message":'
                ' {"content": [{"type": "text",'
                ' "text": "Second message."}]}}'
            )
            yield '{"type": "done"}'

        events = []
        async for event in parse_cli_response(mock_stream()):
            events.append(event)

        # Find text_delta events
        text_events = [e for e in events if e.type == "text_delta"]
        assert len(text_events) == 2
        assert text_events[0].content == "First message."
        # Second text should have newline prefix
        assert text_events[1].content.startswith("\n\n")


class TestExecuteClaudeCli:
    """Tests for execute_claude_cli function."""

    @pytest.mark.asyncio
    async def test_execute_cli_basic(self):
        """Test basic CLI execution."""
        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.returncode = 0

        # Mock readline to return lines then empty
        mock_process.stdout.readline = AsyncMock(
            side_effect=[b'{"type": "text", "content": "Hello"}\n', b'{"type": "done"}\n', b""]
        )
        mock_process.stderr.read = AsyncMock(return_value=b"")
        mock_process.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            lines = []
            async for line in execute_claude_cli("test-session", "Hello"):
                lines.append(line)

        assert len(lines) == 2
        assert '{"type": "text", "content": "Hello"}' in lines[0]

    @pytest.mark.asyncio
    async def test_execute_cli_timeout(self):
        """Test CLI timeout handling."""
        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()

        # Mock readline to return empty (EOF)
        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.kill = MagicMock()

        # First call to wait() times out, second call (after kill) succeeds
        call_count = [0]

        async def mock_wait():
            call_count[0] += 1
            if call_count[0] == 1:
                raise TimeoutError()
            return 0

        mock_process.wait = mock_wait

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(TimeoutError, match="timed out"):
                async for _ in execute_claude_cli("test", "prompt", timeout=1):
                    pass

    @pytest.mark.asyncio
    async def test_execute_cli_error_exit(self):
        """Test CLI error handling on non-zero exit."""
        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.returncode = 1

        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.stdout.read = AsyncMock(
            return_value=b""
        )  # Also mock stdout.read for error handling
        mock_process.stderr.read = AsyncMock(return_value=b"Error: CLI failed")
        mock_process.wait = AsyncMock(return_value=1)
        mock_process.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with pytest.raises(RuntimeError, match="CLI failed"):
                async for _ in execute_claude_cli("test", "prompt"):
                    pass

    @pytest.mark.asyncio
    async def test_execute_cli_session_id_format(self):
        """Test that --resume is used when resume_session_id is provided."""
        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.returncode = 0

        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.wait = AsyncMock(return_value=0)

        captured_cmd = None

        async def capture_subprocess(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return mock_process

        # Test with resume_session_id
        with patch("asyncio.create_subprocess_exec", side_effect=capture_subprocess):
            async for _ in execute_claude_cli(
                "my-session", "test prompt", resume_session_id="abc123-def456"
            ):
                pass

        assert captured_cmd is not None
        # Check that --resume abc123-def456 is in the command
        assert "--resume" in captured_cmd
        resume_idx = captured_cmd.index("--resume")
        assert captured_cmd[resume_idx + 1] == "abc123-def456"

    @pytest.mark.asyncio
    async def test_execute_cli_without_resume(self):
        """Test that --resume is not used when resume_session_id is not provided."""
        mock_process = MagicMock()
        mock_process.stdout = AsyncMock()
        mock_process.stderr = AsyncMock()
        mock_process.returncode = 0

        mock_process.stdout.readline = AsyncMock(return_value=b"")
        mock_process.wait = AsyncMock(return_value=0)

        captured_cmd = None

        async def capture_subprocess(*args, **kwargs):
            nonlocal captured_cmd
            captured_cmd = args
            return mock_process

        # Test without resume_session_id
        with patch("asyncio.create_subprocess_exec", side_effect=capture_subprocess):
            async for _ in execute_claude_cli("my-session", "test prompt"):
                pass

        assert captured_cmd is not None
        # Check that --resume is NOT in the command
        assert "--resume" not in captured_cmd


class TestGetOrchestrator:
    """Tests for get_orchestrator factory function."""

    def test_get_orchestrator_cli(self):
        """Test getting CLI orchestrator."""
        with patch("app.services.ai_client.get_settings") as mock_settings:
            mock_settings.return_value.ai_provider = "claude-cli"

            from app.services.ai_client import get_orchestrator

            orchestrator = get_orchestrator()

            from app.services.orchestrator_cli import orchestrate_chat_cli

            assert orchestrator == orchestrate_chat_cli

    def test_get_orchestrator_anthropic(self):
        """Test getting API orchestrator for anthropic."""
        with patch("app.services.ai_client.get_settings") as mock_settings:
            mock_settings.return_value.ai_provider = "anthropic"

            from app.services.ai_client import get_orchestrator

            orchestrator = get_orchestrator()

            from app.services.orchestrator import orchestrate_chat

            assert orchestrator == orchestrate_chat

    def test_get_orchestrator_bedrock(self):
        """Test getting API orchestrator for bedrock."""
        with patch("app.services.ai_client.get_settings") as mock_settings:
            mock_settings.return_value.ai_provider = "bedrock"

            from app.services.ai_client import get_orchestrator

            orchestrator = get_orchestrator()

            from app.services.orchestrator import orchestrate_chat

            assert orchestrator == orchestrate_chat


class TestOrchestratorContentOrder:
    """Persisted assistant content must keep the live stream order."""

    async def test_persisted_block_order_matches_stream(self):
        """Interleaved text/tool events persist in arrival order, deltas coalesced."""
        # Stream: text -> tool_use -> tool_result -> text (split across two deltas).
        # Regression guard: the old code merged all text first, all tools after.
        streamed = [
            ChatEvent(type="text_delta", content="Let me check. "),
            ChatEvent(
                type="tool_start",
                tool_name="kubectl",
                tool_input={"command": "get pods"},
                tool_use_id="t1",
            ),
            ChatEvent(type="tool_result", content="pod/nginx", tool_use_id="t1"),
            ChatEvent(type="text_delta", content="All "),
            ChatEvent(type="text_delta", content="good."),
            ChatEvent(type="done", metadata={"input_tokens": 10, "output_tokens": 5}),
        ]

        async def fake_parse(_stream):
            for event in streamed:
                yield event

        from app.services import orchestrator_cli

        with (
            patch.object(orchestrator_cli, "parse_cli_response", fake_parse),
            patch.object(orchestrator_cli, "execute_claude_cli", return_value=MagicMock()),
            patch.object(orchestrator_cli, "save_message", new=AsyncMock()) as mock_save,
            patch.object(
                orchestrator_cli, "resolve_session_system_prompt", new=AsyncMock(return_value="")
            ),
            patch.object(orchestrator_cli, "get_system_prompt", return_value="sys"),
            patch.object(orchestrator_cli, "get_cli_session_id", new=AsyncMock(return_value=None)),
            patch.object(orchestrator_cli, "set_cli_session_id", new=AsyncMock()),
            patch.object(
                orchestrator_cli, "ensure_session_title", new=AsyncMock(return_value=None)
            ),
            patch.object(
                orchestrator_cli, "build_connector_env", new=AsyncMock(return_value=({}, set()))
            ),
            patch.object(orchestrator_cli, "update_session_stats", new=AsyncMock()),
            patch.object(orchestrator_cli, "record_usage", new=AsyncMock()),
            patch.object(orchestrator_cli.os, "makedirs"),
        ):
            db = AsyncMock()
            async for _ in orchestrator_cli.orchestrate_chat_cli(
                db=db,
                session_id=uuid4(),
                cluster_name="prod",
                kubeconfig_path=None,
                context_name=None,
                user_message="list pods",
            ):
                pass

        # The assistant message is the save_message call with role == "assistant".
        assistant_content = next(
            call.args[3] for call in mock_save.call_args_list if call.args[2] == "assistant"
        )

        assert [block["type"] for block in assistant_content] == [
            "text",
            "tool_use",
            "tool_result",
            "text",
        ]
        # Tool block sits between the two text blocks (interleaving preserved),
        # not pushed to the end after a single merged text block.
        assert assistant_content[0]["text"] == "Let me check. "
        assert assistant_content[3]["text"] == "All good."  # consecutive deltas coalesced
