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
    # input_tokens is FRESH input only — the cache splits ride their own counters
    # (parity with the other engines; folding them here would double-count now that
    # every surface shows the cache columns explicitly).
    assert meta["input_tokens"] == 3 + 6
    assert meta["cache_write_tokens"] == 7949 + 72
    assert meta["cache_read_tokens"] == 0 + 7949
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


@pytest.mark.asyncio
async def test_error_drains_stream_and_carries_usage() -> None:
    # An error must NOT abandon the stream (returning would leave the suspended
    # execute_opencode generator's cleanup to the GC); it drains, carries the usage
    # accumulated so far in its metadata, and suppresses the trailing truncated done.
    lines = [
        '{"type":"step_finish","part":{"reason":"tool-calls","tokens":{"input":10,"output":5},'
        '"cost":0.02}}',
        '{"type":"error","error":{"name":"APIError","data":{"message":"Forbidden"}}}',
        '{"type":"text","part":{"text":"late line, still drained"}}',
    ]

    async def _gen():
        for line in lines:
            yield line

    events = [e async for e in parse_opencode_response(_gen())]
    assert [e.type for e in events] == ["error", "text_delta"]  # drained past the error
    assert events[0].metadata["input_tokens"] == 10
    assert events[0].metadata["output_tokens"] == 5
    assert events[0].metadata["cost_usd"] == pytest.approx(0.02)
    assert not any(e.type == "done" for e in events)  # no second terminal after the error


@pytest.mark.asyncio
async def test_second_stop_does_not_emit_second_done() -> None:
    lines = [
        '{"type":"step_finish","part":{"reason":"stop","tokens":{"input":1,"output":1}}}',
        '{"type":"step_finish","part":{"reason":"stop","tokens":{"input":1,"output":1}}}',
    ]

    async def _gen():
        for line in lines:
            yield line

    events = [e async for e in parse_opencode_response(_gen())]
    assert sum(1 for e in events if e.type == "done") == 1


@pytest.mark.asyncio
async def test_stop_after_error_does_not_add_second_terminal() -> None:
    lines = [
        '{"type":"error","error":{"message":"boom"}}',
        '{"type":"step_finish","part":{"reason":"stop","tokens":{"input":1,"output":1}}}',
    ]

    async def _gen():
        for line in lines:
            yield line

    events = [e async for e in parse_opencode_response(_gen())]
    assert [e.type for e in events] == ["error"]  # the error won; no done after it


@pytest.mark.asyncio
async def test_only_first_error_is_forwarded() -> None:
    lines = [
        '{"type":"error","error":{"message":"first"}}',
        '{"type":"error","error":{"message":"second"}}',
    ]

    async def _gen():
        for line in lines:
            yield line

    events = [e async for e in parse_opencode_response(_gen())]
    assert [e.type for e in events] == ["error"]
    assert events[0].content == "first"


@pytest.mark.asyncio
async def test_stream_crash_yields_error_with_usage_then_reraises() -> None:
    # A nonzero-exit/timeout raises from the source mid-iteration: the parser must
    # surface the reason WITH the usage accumulated so far, then re-raise.
    async def _gen():
        yield (
            '{"type":"step_finish","part":{"reason":"tool-calls",'
            '"tokens":{"input":7,"output":3},"cost":0.05}}'
        )
        raise RuntimeError("opencode failed: Exit code 1")

    events = []
    with pytest.raises(RuntimeError, match="Exit code 1"):
        async for e in parse_opencode_response(_gen()):
            events.append(e)
    assert [e.type for e in events] == ["error"]
    assert events[0].metadata["input_tokens"] == 7
    assert events[0].metadata["cost_usd"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_string_cost_is_ignored() -> None:
    line = (
        '{"type":"step_finish","part":{"reason":"stop",'
        '"tokens":{"input":1,"output":1},"cost":"broken"}}'
    )

    async def _one():
        yield line

    events = [e async for e in parse_opencode_response(_one())]
    assert events[-1].metadata["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_crashed_tool_surfaces_state_error_as_content() -> None:
    # A non-completed tool carries its message in state.error, not output — it must reach
    # the transcript (and the orchestrator's AWS-expiry sniffing) instead of "".
    line = (
        '{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"c1",'
        '"state":{"status":"error","input":{"command":"aws s3 ls"},'
        '"error":"An error occurred (ExpiredToken)"}}}'
    )

    async def _one():
        yield line

    events = [e async for e in parse_opencode_response(_one())]
    assert events[1].type == "tool_result"
    assert events[1].is_error is True
    assert events[1].content is not None and "ExpiredToken" in events[1].content
