"""parse_opencode_response — validated against live-captured opencode JSONL.

The fixtures in tests/fixtures/opencode_stream_*.jsonl were captured from a real
``opencode run --format json`` turn against Bedrock (opencode 1.17.10), so these
tests pin the actual wire schema, not an assumed one.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.services.ai_cli_client import parse_opencode_response

FIXTURES = Path(__file__).parent / "fixtures"


async def _lines(name: str) -> AsyncIterator[str]:
    for line in (FIXTURES / name).read_text().splitlines():
        if line.strip():
            yield line


async def _collect(name: str) -> list:
    return [ev async for ev in parse_opencode_response(_lines(name))]


@pytest.mark.asyncio
async def test_text_turn_maps_to_text_delta_then_done() -> None:
    events = await _collect("opencode_stream_text.jsonl")
    assert [e.type for e in events] == ["text_delta", "done"]
    assert events[0].content == "ok"
    meta = events[1].metadata
    assert meta["input_tokens"] == 7410
    assert meta["output_tokens"] == 2
    assert meta["tool_calls"] == 0
    assert meta["iterations"] == 1
    assert meta["cost_usd"] == pytest.approx(0.00025963)


@pytest.mark.asyncio
async def test_tool_turn_emits_start_result_and_aggregates_usage() -> None:
    events = await _collect("opencode_stream_tool.jsonl")
    # bash tool (start+result), then final text, then done.
    assert [e.type for e in events] == ["tool_start", "tool_result", "text_delta", "done"]

    start, result = events[0], events[1]
    assert start.tool_name == "bash"
    assert start.tool_input == {"command": "echo hello-from-bash"}
    assert start.tool_use_id == "tooluse_1"
    assert result.tool_name == "bash"
    assert result.content == "hello-from-bash\n"
    assert result.is_error is False
    assert result.tool_use_id == start.tool_use_id

    meta = events[-1].metadata
    assert meta["tool_calls"] == 1
    assert meta["iterations"] == 2  # two step_finish events
    assert meta["input_tokens"] == 3 + 6
    assert meta["output_tokens"] == 57 + 2
    assert meta["cost_usd"] == pytest.approx(0.01022425 + 0.0009009)


@pytest.mark.asyncio
async def test_failed_bash_marks_tool_result_error() -> None:
    line = (
        '{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"c1",'
        '"state":{"status":"completed","input":{"command":"false"},"output":"",'
        '"metadata":{"exit":1}}}}'
    )

    async def _one() -> AsyncIterator[str]:
        yield line

    events = [e async for e in parse_opencode_response(_one())]
    assert events[0].type == "tool_start"
    assert events[1].type == "tool_result"
    assert events[1].is_error is True
    # No step_finish in the stream → a synthetic done still closes the turn.
    assert events[-1].type == "done"


@pytest.mark.asyncio
async def test_multiple_tool_calls_accumulate() -> None:
    lines = [
        '{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"c1",'
        '"state":{"status":"completed","input":{"command":"ls"},"output":"a\\n",'
        '"metadata":{"exit":0}}}}',
        '{"type":"tool_use","part":{"type":"tool","tool":"read","callID":"c2",'
        '"state":{"status":"completed","input":{"path":"x"},"output":"data"}}}',
        '{"type":"step_finish","part":{"reason":"stop","tokens":{"input":10,"output":5}}}',
    ]

    async def _gen():
        for line in lines:
            yield line

    events = [e async for e in parse_opencode_response(_gen())]
    assert [e.type for e in events] == [
        "tool_start",
        "tool_result",
        "tool_start",
        "tool_result",
        "done",
    ]
    assert events[0].tool_use_id == "c1" and events[2].tool_use_id == "c2"
    assert events[-1].metadata["tool_calls"] == 2
    assert "truncated" not in events[-1].metadata  # genuine stop, not truncated


@pytest.mark.asyncio
async def test_non_bash_status_error_marks_error() -> None:
    # A tool that fails with status!="completed" and no exit code (e.g. a write error).
    line = (
        '{"type":"tool_use","part":{"type":"tool","tool":"write","callID":"c1",'
        '"state":{"status":"error","input":{"path":"/ro"},"output":"permission denied"}}}'
    )

    async def _one():
        yield line

    events = [e async for e in parse_opencode_response(_one())]
    assert events[1].type == "tool_result" and events[1].is_error is True


@pytest.mark.asyncio
async def test_max_steps_without_stop_yields_truncated_done() -> None:
    # Two tool-call steps, never a "stop" → synthetic done tagged truncated, usage summed.
    lines = [
        '{"type":"step_finish","part":{"reason":"tool-calls","tokens":{"input":3,"output":4}}}',
        '{"type":"step_finish","part":{"reason":"tool-calls","tokens":{"input":5,"output":6}}}',
    ]

    async def _gen():
        for line in lines:
            yield line

    events = [e async for e in parse_opencode_response(_gen())]
    assert events[-1].type == "done"
    assert events[-1].metadata["truncated"] is True
    assert events[-1].metadata["input_tokens"] == 8
    assert events[-1].metadata["iterations"] == 2


@pytest.mark.asyncio
async def test_non_json_and_shapeless_lines_skipped() -> None:
    lines = [
        "not json at all",
        '["a","list","not","a","dict"]',
        '{"type":"text","part":{"text":"hi"}}',
        '{"type":"step_finish","part":{"reason":"stop"}}',
    ]

    async def _gen():
        for line in lines:
            yield line

    events = [e async for e in parse_opencode_response(_gen())]
    assert [e.type for e in events] == ["text_delta", "done"]


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"type":"error","error":{"message":"top-level msg"}}', "top-level msg"),
        ('{"type":"error","error":{"name":"OnlyName"}}', "OnlyName"),
        ('{"type":"error","error":{}}', "opencode error"),
    ],
)
@pytest.mark.asyncio
async def test_error_message_fallback_chain(payload: str, expected: str) -> None:
    async def _one():
        yield payload

    events = [e async for e in parse_opencode_response(_one())]
    assert events[0].type == "error" and events[0].content == expected


@pytest.mark.asyncio
async def test_error_event_terminates() -> None:
    line = '{"type":"error","error":{"name":"APIError","data":{"message":"Forbidden: bad key"}}}'

    async def _one() -> AsyncIterator[str]:
        yield line

    events = [e async for e in parse_opencode_response(_one())]
    assert len(events) == 1
    assert events[0].type == "error"
    assert events[0].content and "Forbidden" in events[0].content
