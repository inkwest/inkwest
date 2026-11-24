"""Tests for the standalone inkwest_agent package (loop + protocol + entrypoint)."""

import copy
import io
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from app.services.events import ChatEvent
from inkwest_agent import loop as loop_mod
from inkwest_agent.protocol import TurnSpec, TurnSpecError, emit, parse_turn_spec

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _spec(**overrides) -> TurnSpec:
    base: dict = {
        "session_id": SESSION_ID,
        "user_message": "why is my pod crashing?",
        "model": "meta.llama3-1-70b-instruct-v1:0",
        "system_prompt": "You are an SRE assistant.",
        "region": "eu-west-1",
    }
    # Convenience: route grouped fields into the nested context/limits objects.
    context = {}
    if "history" in overrides:
        context["history"] = overrides.pop("history")
    limits = {}
    for k in ("max_iterations", "max_tokens", "max_conversation_messages"):
        if k in overrides:
            limits[k] = overrides.pop(k)
    if context:
        base["context"] = context
    if limits:
        base["limits"] = limits
    base.update(overrides)
    return parse_turn_spec(base)


def _converse_text(text: str, *, stop: str = "end_turn", tokens=(10, 5)) -> dict:
    return {
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop,
    }


def _converse_tool(tool_use_id: str, name: str, tool_input: dict) -> dict:
    return {
        "usage": {"inputTokens": 8, "outputTokens": 4},
        "output": {
            "message": {
                "content": [
                    {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}
                ]
            }
        },
        "stopReason": "tool_use",
    }


def _client(responses: list) -> MagicMock:
    """A fake bedrock-runtime client whose converse() returns queued responses."""
    client = MagicMock()
    client.converse = MagicMock(side_effect=responses)
    return client


async def _collect(spec: TurnSpec, client) -> list[ChatEvent]:
    return [ev async for ev in loop_mod.run_turn(spec, client)]


# --------------------------------------------------------------------------- #
# protocol
# --------------------------------------------------------------------------- #


def test_parse_turn_spec_minimal():
    spec = _spec()
    assert spec.session_id == SESSION_ID
    assert spec.invoke_target == spec.model  # no ARN override


def test_invoke_target_prefers_arn():
    spec = _spec(model_target="arn:aws:bedrock:eu-west-1::imported-model/x")
    assert spec.invoke_target.startswith("arn:")


@pytest.mark.parametrize("drop", ["session_id", "user_message", "model", "system_prompt"])
def test_parse_turn_spec_missing_required(drop):
    raw = {
        "session_id": SESSION_ID,
        "user_message": "x",
        "model": "m",
        "system_prompt": "s",
    }
    del raw[drop]
    with pytest.raises(TurnSpecError, match="missing required"):
        parse_turn_spec(raw)


def test_parse_turn_spec_rejects_unknown_field():
    with pytest.raises(TurnSpecError, match="unknown field"):
        parse_turn_spec(
            {
                "session_id": SESSION_ID,
                "user_message": "x",
                "model": "m",
                "system_prompt": "s",
                "bogus": 1,
            }
        )


def test_parse_turn_spec_accepts_bedrock_endpoint_url():
    """Regression guard for the protocol v5 field (the 'unknown field bedrock_endpoint_url'
    bug class): the agent must ACCEPT the field the broker-wired orchestrator sends, and the
    value must survive parsing. Catches any name drift between orchestrator and agent."""
    spec = parse_turn_spec(
        {
            "session_id": SESSION_ID,
            "user_message": "x",
            "model": "m",
            "system_prompt": "s",
            "bedrock_endpoint_url": "http://gw:8000/api/llm-proxy",
        }
    )
    assert spec.bedrock_endpoint_url == "http://gw:8000/api/llm-proxy"


def test_parse_turn_spec_rejects_non_object():
    with pytest.raises(TurnSpecError, match="must be a JSON object"):
        parse_turn_spec(["not", "an", "object"])


def test_parse_turn_spec_history_must_be_list():
    with pytest.raises(TurnSpecError, match="'context.history' must be a list"):
        parse_turn_spec(
            {
                "session_id": SESSION_ID,
                "user_message": "x",
                "model": "m",
                "system_prompt": "s",
                "context": {"history": {}},
            }
        )


def test_parse_turn_spec_nested_contract():
    spec = parse_turn_spec(
        {
            "session_id": SESSION_ID,
            "user_message": "x",
            "model": "m",
            "system_prompt": "s",
            "engine": "inkwest-agent",
            "context": {"mode": "replay", "history": [{"role": "user", "content": "hi"}]},
            "limits": {"max_tokens": 2048, "budget_remaining": 1000, "wallclock_seconds": 60},
            "tools": [{"name": "aws_call", "exec_profile": "agent"}],
            "creds_path": "/home/agent/.aws",
        }
    )
    assert spec.context.mode == "replay"
    assert spec.context.history == [{"role": "user", "content": "hi"}]
    assert spec.limits.max_tokens == 2048
    assert spec.limits.budget_remaining == 1000
    assert spec.tools[0].name == "aws_call" and spec.tools[0].exec_profile == "agent"
    assert spec.creds_path == "/home/agent/.aws"


def test_parse_turn_spec_rejects_unknown_nested_field():
    with pytest.raises(TurnSpecError, match="'limits' has unknown"):
        parse_turn_spec(
            {
                "session_id": SESSION_ID,
                "user_message": "x",
                "model": "m",
                "system_prompt": "s",
                "limits": {"bogus": 1},
            }
        )


@pytest.mark.parametrize(
    "bad", [{"max_tokens": "lots"}, {"max_iterations": 1.5}, {"max_tokens": True}]
)
def test_parse_turn_spec_limits_reject_non_int(bad):
    with pytest.raises(TurnSpecError, match="must be an int"):
        parse_turn_spec(
            {
                "session_id": SESSION_ID,
                "user_message": "x",
                "model": "m",
                "system_prompt": "s",
                "limits": bad,
            }
        )


def test_tool_spec_from_raw_defaults_exec_profile():
    spec = _spec(tools=[{"name": "aws_call"}])  # no exec_profile on the wire
    assert spec.tools[0].name == "aws_call"
    assert spec.tools[0].exec_profile == "agent"  # default applied on parse


def test_parse_turn_spec_round_trips_reserved_fields():
    """asdict → parse_turn_spec is stable, incl. the reserved fields (wire contract)."""
    import dataclasses

    spec = parse_turn_spec(
        {
            "session_id": SESSION_ID,
            "user_message": "x",
            "model": "m",
            "system_prompt": "s",
            "engine": "inkwest-agent",
            "context": {"mode": "resume", "history": [], "resume_token": "tok-123"},
            "limits": {"budget_remaining": 5000, "wallclock_seconds": 90},
            "tools": [{"name": "kubectl_get", "exec_profile": "broker"}],
            "creds_path": "/home/agent/.aws",
        }
    )
    assert parse_turn_spec(dataclasses.asdict(spec)) == spec


def test_emit_writes_one_ndjson_line():
    buf = io.StringIO()
    emit(ChatEvent(type="text_delta", content="hello"), stream=buf)
    line = buf.getvalue()
    assert line.endswith("\n")
    assert json.loads(line) == {"type": "text_delta", "content": "hello"}


# --------------------------------------------------------------------------- #
# converse translation
# --------------------------------------------------------------------------- #


def test_to_converse_message_string_content():
    from inkwest_agent.converse import to_converse_message

    out = to_converse_message({"role": "user", "content": "hello"})
    assert out == {"role": "user", "content": [{"text": "hello"}]}


def test_to_converse_message_translates_blocks():
    from inkwest_agent.converse import to_converse_message

    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "let me check"},
            {
                "type": "tool_use",
                "id": "tu-1",
                "name": "kubectl_get",
                "input": {"resource": "pods"},
            },
            {"type": "thinking", "thinking": "dropped"},  # Claude-only → dropped
        ],
    }
    blocks = to_converse_message(msg)["content"]
    assert {"text": "let me check"} in blocks
    assert any("toolUse" in b for b in blocks)
    assert all("thinking" not in b for b in blocks)


def test_to_converse_message_tool_result_and_status():
    from inkwest_agent.converse import to_converse_message

    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "tu-1", "content": "boom", "is_error": True},
        ],
    }
    tr = to_converse_message(msg)["content"][0]["toolResult"]
    assert tr["toolUseId"] == "tu-1"
    assert tr["status"] == "error"
    assert tr["content"] == [{"text": "boom"}]


def test_to_converse_message_empty_content_gets_stub():
    from inkwest_agent.converse import to_converse_message

    # Converse rejects empty content; an all-whitespace message keeps a stub.
    out = to_converse_message({"role": "assistant", "content": [{"type": "text", "text": "  "}]})
    assert out["content"] == [{"text": "(no content)"}]


def test_tool_result_text_flattens_block_list():
    from inkwest_agent.converse import tool_result_text

    assert tool_result_text("plain") == "plain"
    assert tool_result_text([{"text": "a"}, {"text": "b"}]) == "a\nb"
    assert tool_result_text(42) == "42"


def test_to_converse_tools_wraps_definitions():
    from inkwest_agent.converse import to_converse_tools

    cfg = to_converse_tools()
    assert "tools" in cfg and cfg["tools"]
    spec0 = cfg["tools"][0]["toolSpec"]
    assert {"name", "description", "inputSchema"} <= spec0.keys()
    assert "json" in spec0["inputSchema"]


def test_make_bedrock_client_passes_explicit_creds():
    from inkwest_agent import converse as cv

    secret = "SK"  # pragma: allowlist secret
    with patch.object(cv, "boto3") as boto3_mock:
        cv.make_bedrock_client("eu-west-1", access_key="AK", secret_key=secret)
        kwargs = boto3_mock.client.call_args.kwargs
        assert kwargs["region_name"] == "eu-west-1"
        assert kwargs["aws_access_key_id"] == "AK"
        assert kwargs["aws_secret_access_key"] == secret


# --------------------------------------------------------------------------- #
# loop
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_turn_text_only_ends_with_done():
    client = _client([_converse_text("Your pod OOMed.", tokens=(12, 7))])
    events = await _collect(_spec(), client)

    assert [e.type for e in events] == ["text_delta", "done"]
    assert events[0].content == "Your pod OOMed."
    meta = events[-1].metadata
    assert meta["iterations"] == 1
    assert meta["input_tokens"] == 12
    assert meta["output_tokens"] == 7
    assert meta["tool_calls"] == 0
    # The user_message is appended to history for the model call.
    sent = client.converse.call_args.kwargs["messages"]
    assert sent[-1]["content"][0]["text"] == "why is my pod crashing?"


@pytest.mark.asyncio
async def test_run_turn_executes_tool_then_finishes():
    client = _client(
        [
            _converse_tool("tu-1", "kubectl_get", {"resource": "pods"}),
            _converse_text("Found 3 pods.", tokens=(20, 9)),
        ]
    )
    # Patch the shared executor so no real kubectl/subprocess runs.
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pod/foo Running", False)),
    ) as exec_tool:
        events = await _collect(_spec(), client)

    types = [e.type for e in events]
    assert types == ["tool_start", "tool_result", "text_delta", "done"]
    exec_tool.assert_awaited_once()
    assert events[-1].metadata["tool_calls"] == 1
    assert events[-1].metadata["iterations"] == 2
    # tokens accumulate across both iterations (8+20 in, 4+9 out)
    assert events[-1].metadata["input_tokens"] == 28
    assert events[-1].metadata["output_tokens"] == 13


@pytest.mark.asyncio
async def test_run_turn_client_error_emits_error():
    err = ClientError({"Error": {"Message": "throttled"}}, "Converse")
    client = MagicMock()
    client.converse = MagicMock(side_effect=err)
    events = await _collect(_spec(), client)

    assert events[0].type == "error"
    assert "throttled" in events[0].content


@pytest.mark.asyncio
async def test_run_turn_unexpected_error_emits_error():
    client = MagicMock()
    client.converse = MagicMock(side_effect=RuntimeError("boom"))
    events = await _collect(_spec(), client)

    assert events[0].type == "error"
    assert "boom" in events[0].content


@pytest.mark.asyncio
async def test_run_turn_max_iterations_emits_error():
    # Always returns tool_use → never terminates until the iteration cap.
    client = MagicMock()
    client.converse = MagicMock(
        return_value=_converse_tool("tu", "kubectl_get", {"resource": "pods"})
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("ok", False)),
    ):
        events = await _collect(_spec(max_iterations=3), client)

    assert events[-1].type == "error"
    assert "maximum iterations" in events[-1].content
    assert client.converse.call_count == 3


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_main_run_emits_ndjson_and_returns_zero(capsys):
    from inkwest_agent import __main__ as entry

    spec_json = json.dumps(
        {
            "session_id": SESSION_ID,
            "user_message": "hi",
            "model": "meta.llama3-1-70b-instruct-v1:0",
            "system_prompt": "s",
            "region": "eu-west-1",
        }
    )
    client = _client([_converse_text("hello back")])
    with (
        patch.object(entry.sys, "stdin", io.StringIO(spec_json)),
        patch.object(entry, "make_bedrock_client", return_value=client),
    ):
        rc = await entry._run()

    out_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert rc == 0
    assert [e["type"] for e in out_lines] == ["text_delta", "done"]


@pytest.mark.asyncio
async def test_main_run_bad_spec_returns_one(capsys):
    from inkwest_agent import __main__ as entry

    with patch.object(entry.sys, "stdin", io.StringIO("not json")):
        rc = await entry._run()

    out = capsys.readouterr().out
    assert rc == 1
    assert json.loads(out)["type"] == "error"


@pytest.mark.asyncio
async def test_main_run_client_build_failure_returns_one(capsys):
    from inkwest_agent import __main__ as entry

    spec_json = json.dumps(
        {
            "session_id": SESSION_ID,
            "user_message": "hi",
            "model": "m",
            "system_prompt": "s",
            "region": "eu-west-1",
        }
    )
    with (
        patch.object(entry.sys, "stdin", io.StringIO(spec_json)),
        patch.object(entry, "make_bedrock_client", side_effect=RuntimeError("no creds")),
    ):
        rc = await entry._run()

    out = capsys.readouterr().out
    assert rc == 1
    assert json.loads(out)["type"] == "error"
    assert "unavailable" in json.loads(out)["content"]


@pytest.mark.asyncio
async def test_main_run_loop_error_returns_one(capsys):
    from app.services.events import error_event
    from inkwest_agent import __main__ as entry

    spec_json = json.dumps(
        {
            "session_id": SESSION_ID,
            "user_message": "hi",
            "model": "m",
            "system_prompt": "s",
            "region": "eu-west-1",
        }
    )

    async def _err_turn(spec, client):
        yield error_event("boom")

    with (
        patch.object(entry.sys, "stdin", io.StringIO(spec_json)),
        patch.object(entry, "make_bedrock_client", return_value=object()),
        patch.object(entry, "run_turn", _err_turn),
    ):
        rc = await entry._run()

    assert rc == 1
    assert json.loads(capsys.readouterr().out)["type"] == "error"


@pytest.mark.asyncio
async def test_run_turn_truncates_long_history():
    # History longer than the cap exercises the truncate path before the call.
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(6)
    ]
    client = _client([_converse_text("ok")])
    events = await _collect(_spec(history=history, max_conversation_messages=2), client)

    assert events[-1].type == "done"
    # Truncated to the cap before translating to Converse messages.
    assert len(client.converse.call_args.kwargs["messages"]) <= 2


@pytest.mark.asyncio
async def test_run_turn_resume_mode_still_replays_history():
    """mode=resume is reserved: the loop ignores it and uses the seeded history."""
    spec = parse_turn_spec(
        {
            "session_id": SESSION_ID,
            "user_message": "now",
            "model": "m",
            "system_prompt": "s",
            "region": "eu-west-1",
            "context": {"mode": "resume", "history": [{"role": "user", "content": "before"}]},
        }
    )
    client = _client([_converse_text("ok")])
    events = await _collect(spec, client)

    assert events[-1].type == "done"
    msgs = client.converse.call_args.kwargs["messages"]
    # Seeded history is used (prior turn present) + the new user message appended.
    assert msgs[0]["content"][0]["text"] == "before"
    assert msgs[-1]["content"][0]["text"] == "now"


# --- P3: Converse prompt-cache accumulation + usage on error paths ----------------------
#
# Cache usage keys mirror the live captures in tests/fixtures/bedrock/converse_nonstream_*
# (documented ``…Tokens`` names + the ``…TokenCount`` alias observed on the agent path).


def _converse_cached(text: str, usage: dict) -> dict:
    return {
        "usage": usage,
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": "end_turn",
    }


@pytest.mark.asyncio
async def test_run_turn_accumulates_cache_tokens_into_done_meta():
    client = _client(
        [
            _converse_cached(
                "ok",
                {
                    "inputTokens": 7,
                    "outputTokens": 16,
                    "cacheReadInputTokens": 0,
                    "cacheWriteInputTokens": 12608,
                },
            )
        ]
    )
    events = await _collect(_spec(), client)
    meta = events[-1].metadata
    assert events[-1].type == "done"
    assert meta["cache_read_tokens"] == 0
    assert meta["cache_write_tokens"] == 12608
    assert meta["input_tokens"] == 7


@pytest.mark.asyncio
async def test_run_turn_cache_count_alias_spelling_accepted():
    # The agent path emitted cacheReadInputTokenCount alongside the documented name
    # (captured live); when only the alias is present it must still count.
    client = _client(
        [
            _converse_cached(
                "ok",
                {"inputTokens": 5, "outputTokens": 2, "cacheReadInputTokenCount": 42},
            )
        ]
    )
    events = await _collect(_spec(), client)
    assert events[-1].metadata["cache_read_tokens"] == 42


@pytest.mark.asyncio
async def test_run_turn_client_error_carries_accumulated_usage():
    """A turn that errors after completed iterations still reports the burned tokens."""
    err = ClientError({"Error": {"Message": "throttled"}}, "Converse")
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[_converse_tool("tu-1", "kubectl_get", {"resource": "pods"}), err]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pod/foo Running", False)),
    ):
        events = await _collect(_spec(), client)
    assert events[-1].type == "error"
    meta = events[-1].metadata
    # First iteration completed (8 in / 4 out) — billed by AWS, so reported on the error.
    assert meta["input_tokens"] == 8
    assert meta["output_tokens"] == 4
    assert meta["cache_read_tokens"] == 0


def _converse_tool_usage(tool_use_id: str, usage: dict) -> dict:
    return {
        "usage": usage,
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": tool_use_id,
                            "name": "kubectl_get",
                            "input": {"resource": "pods"},
                        }
                    }
                ]
            }
        },
        "stopReason": "tool_use",
    }


def _call_usage(fresh: int, read: int, write: int) -> dict:
    return {
        "inputTokens": fresh,
        "outputTokens": 40,
        "cacheReadInputTokens": read,
        "cacheWriteInputTokens": write,
    }


@pytest.mark.asyncio
async def test_run_turn_context_tokens_is_last_call_prompt():
    """The turn's counts sum every iteration's prompt; context_tokens is the last one's."""
    client = _client(
        [
            _converse_tool_usage("tu-1", _call_usage(3, 0, 51_000)),
            _converse_tool_usage("tu-2", _call_usage(5, 51_000, 1_200)),
            _converse_cached("done", _call_usage(7, 52_200, 900)),
        ]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pod/foo Running", False)),
    ):
        events = await _collect(_spec(), client)
    assert events[-1].type == "done"
    meta = events[-1].metadata
    assert meta["context_tokens"] == 7 + 52_200 + 900
    assert meta["input_tokens"] == 15
    assert meta["cache_read_tokens"] == 103_200
    assert meta["cache_write_tokens"] == 53_100


@pytest.mark.parametrize("empty_usage", [{}, _call_usage(0, 0, 0)], ids=["absent", "zeros"])
@pytest.mark.asyncio
async def test_run_turn_empty_usage_call_keeps_the_last_real_reading(empty_usage: dict):
    client = _client(
        [
            _converse_tool_usage("tu-1", _call_usage(3, 0, 51_000)),
            _converse_cached("done", empty_usage),
        ]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pod/foo Running", False)),
    ):
        events = await _collect(_spec(), client)
    assert events[-1].type == "done"
    assert events[-1].metadata["context_tokens"] == 51_003


@pytest.mark.asyncio
async def test_run_turn_error_carries_last_completed_call_context():
    err = ClientError({"Error": {"Message": "throttled"}}, "Converse")
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[_converse_tool_usage("tu-1", _call_usage(3, 0, 51_000)), err]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pod/foo Running", False)),
    ):
        events = await _collect(_spec(), client)
    assert events[-1].type == "error"
    assert events[-1].metadata["context_tokens"] == 51_003


@pytest.mark.asyncio
async def test_run_turn_error_before_any_call_reports_no_context():
    client = MagicMock()
    client.converse = MagicMock(side_effect=RuntimeError("boom"))
    events = await _collect(_spec(), client)
    assert events[-1].type == "error"
    assert events[-1].metadata["context_tokens"] is None


# --- Opportunistic prompt caching -------------------------------------------
#
# Rejection messages are VERBATIM live captures (2026-07-04/05 probes, recorded in the
# gitignored misc/bedrock-fixtures-verbatim/round3-cachepoint-placement-probe.md) — never
# invent variants. Two signatures, two degradation tiers:
#   AccessDeniedException + "prompt caching"        -> model caches nothing: strip ALL markers
#   ValidationException  + "extraneous key [cachePoint]" (Nova, toolConfig placement)
#                                                   -> drop the TOOLS marker only, keep caching

CACHE_POINT = {"cachePoint": {"type": "default"}}
CACHE_REJECTION_MSG = (
    "You invoked an unsupported model or your request did not allow prompt caching. "
    "See the documentation for more information."
)
PLACEMENT_REJECTION_MSG = (
    "The model returned the following errors: Malformed input request: #/toolConfig/tools/0: "
    "extraneous key [cachePoint] is not permitted, please reformat your input and try again."
)


def _cache_rejection() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": CACHE_REJECTION_MSG}}, "Converse"
    )


def _placement_rejection() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ValidationException", "Message": PLACEMENT_REJECTION_MSG}}, "Converse"
    )


def _assert_marker_free(kwargs: dict[str, Any]) -> None:
    assert all("cachePoint" not in b for b in kwargs["system"])
    assert all("cachePoint" not in b for b in kwargs["toolConfig"]["tools"])
    assert all("cachePoint" not in b for m in kwargs["messages"] for b in m["content"])


def _assert_markers_no_tools(kwargs: dict[str, Any]) -> None:
    """System + last-message markers present, tools marker absent (Nova downgrade)."""
    assert kwargs["system"][-1] == CACHE_POINT
    assert all("cachePoint" not in b for b in kwargs["toolConfig"]["tools"])
    assert kwargs["messages"][-1]["content"][-1] == CACHE_POINT


@pytest.mark.asyncio
async def test_run_turn_places_cache_points():
    """(a) Markers land after system, as last tools member, and on the last message."""
    client = _client([_converse_text("ok")])
    await _collect(_spec(), client)

    kw = client.converse.call_args.kwargs
    assert kw["system"][-1] == CACHE_POINT
    assert sum(1 for b in kw["system"] if "cachePoint" in b) == 1
    assert kw["toolConfig"]["tools"][-1] == CACHE_POINT
    assert sum(1 for b in kw["toolConfig"]["tools"] if "cachePoint" in b) == 1
    # Only the LAST message carries a marker (the growing history is the cached prefix).
    assert kw["messages"][-1]["content"][-1] == CACHE_POINT
    assert sum(1 for m in kw["messages"] for b in m["content"] if "cachePoint" in b) == 1


@pytest.mark.asyncio
async def test_run_turn_cache_points_follow_last_message_across_iterations():
    """On a tool-use turn, iteration 2's marker sits on the tool-result message."""
    client = _client(
        [
            _converse_tool("tu-1", "kubectl_get", {"resource": "pods"}),
            _converse_text("Found 3 pods."),
        ]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pod/foo Running", False)),
    ):
        await _collect(_spec(), client)

    for call in client.converse.call_args_list:
        kw = call.kwargs
        assert kw["messages"][-1]["content"][-1] == CACHE_POINT
        assert sum(1 for m in kw["messages"] for b in m["content"] if "cachePoint" in b) == 1
    # Iteration 2's last message is the toolResult user turn.
    second = client.converse.call_args_list[1].kwargs["messages"][-1]
    assert "toolResult" in second["content"][0]


@pytest.mark.asyncio
async def test_run_turn_cache_points_survive_truncation():
    """The marker rides the post-truncation last message (window slid, prefix intact)."""
    history = [
        {"role": "user", "content": f"question {i}"}
        if i % 2 == 0
        else {"role": "assistant", "content": [{"type": "text", "text": f"answer {i}"}]}
        for i in range(8)
    ]
    client = _client([_converse_text("ok")])
    await _collect(_spec(history=history, max_conversation_messages=4), client)

    kw = client.converse.call_args.kwargs
    assert len(kw["messages"]) <= 4
    assert kw["messages"][-1]["content"][-1] == CACHE_POINT
    assert sum(1 for m in kw["messages"] for b in m["content"] if "cachePoint" in b) == 1


@pytest.mark.asyncio
async def test_run_turn_cache_rejection_strips_and_retries_once():
    """(b) The captured signature → one marker-free retry, same call, no extra usage."""
    from inkwest_agent.converse import strip_cache_points

    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[_cache_rejection(), _converse_text("ok", tokens=(12, 7))]
    )
    events = await _collect(_spec(), client)

    assert [e.type for e in events] == ["text_delta", "done"]
    assert client.converse.call_count == 2
    first = client.converse.call_args_list[0].kwargs
    retry = client.converse.call_args_list[1].kwargs
    _assert_marker_free(retry)
    # The retry is the EXACT same call minus every marker — full-kwargs identity, so a
    # regression rebuilding toolConfig/inferenceConfig/modelId on retry cannot slip past.
    assert strip_cache_points(first) == retry
    # The rejected call is pre-inference: only the successful call is counted.
    meta = events[-1].metadata
    assert meta["iterations"] == 1
    assert meta["input_tokens"] == 12
    assert meta["output_tokens"] == 7


@pytest.mark.asyncio
async def test_run_turn_cache_rejection_on_second_iteration_preserves_usage():
    """A rejection mid-turn (iteration 2) retries marker-free and keeps iteration-1 usage."""
    from inkwest_agent.converse import strip_cache_points

    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            _converse_tool("tu-1", "kubectl_get", {"resource": "pods"}),
            _cache_rejection(),
            _converse_text("Found 3 pods.", tokens=(20, 9)),
        ]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pod/foo Running", False)),
    ):
        events = await _collect(_spec(), client)

    assert events[-1].type == "done"
    assert client.converse.call_count == 3
    rejected = client.converse.call_args_list[1].kwargs
    retry = client.converse.call_args_list[2].kwargs
    assert strip_cache_points(rejected) == retry
    _assert_marker_free(retry)
    # Iteration-1 usage (8 in / 4 out) survives through the mid-turn retry.
    meta = events[-1].metadata
    assert meta["iterations"] == 2
    assert meta["input_tokens"] == 28
    assert meta["output_tokens"] == 13


@pytest.mark.asyncio
async def test_run_turn_cache_rejection_memo_skips_markers_for_later_iterations():
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            _cache_rejection(),
            _converse_tool("tu-1", "kubectl_get", {"resource": "pods"}),
            _converse_text("done."),
        ]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("ok", False)),
    ):
        events = await _collect(_spec(), client)

    assert events[-1].type == "done"
    # 1 rejection + 1 retry + 1 second iteration; the memo prevents further marker sends.
    assert client.converse.call_count == 3
    _assert_marker_free(client.converse.call_args_list[2].kwargs)


@pytest.mark.asyncio
async def test_run_turn_cache_rejection_twice_surfaces_error_with_zero_usage():
    """One full-strip retry max: a second rejection surfaces as the AI service error,
    and both rejections being pre-inference the reported usage is all-zero."""
    client = MagicMock()
    client.converse = MagicMock(side_effect=[_cache_rejection(), _cache_rejection()])
    events = await _collect(_spec(), client)

    assert client.converse.call_count == 2
    assert events[-1].type == "error"
    assert "AI service error" in events[-1].content
    meta = events[-1].metadata
    assert meta["input_tokens"] == 0
    assert meta["output_tokens"] == 0
    assert meta["cache_read_tokens"] == 0
    assert meta["cache_write_tokens"] == 0


@pytest.mark.asyncio
async def test_run_turn_tools_marker_rejection_downgrades_and_keeps_caching():
    """The Nova placement signature drops ONLY the tools marker — caching stays on."""
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[_placement_rejection(), _converse_text("ok", tokens=(5, 2))]
    )
    events = await _collect(_spec(), client)

    assert [e.type for e in events] == ["text_delta", "done"]
    assert client.converse.call_count == 2
    first = client.converse.call_args_list[0].kwargs
    retry = client.converse.call_args_list[1].kwargs
    assert first["toolConfig"]["tools"][-1] == CACHE_POINT  # rejected shape had it
    _assert_markers_no_tools(retry)


@pytest.mark.asyncio
async def test_run_turn_tools_marker_memo_holds_for_later_iterations():
    """After the placement downgrade, later iterations keep system+message markers only."""
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            _placement_rejection(),
            _converse_tool("tu-1", "kubectl_get", {"resource": "pods"}),
            _converse_text("done."),
        ]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("ok", False)),
    ):
        events = await _collect(_spec(), client)

    assert events[-1].type == "done"
    assert client.converse.call_count == 3
    _assert_markers_no_tools(client.converse.call_args_list[2].kwargs)


@pytest.mark.asyncio
async def test_run_turn_double_placement_rejection_strips_everything():
    """A model refusing marker placement twice ends up marker-free — turn still completes."""
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            _placement_rejection(),
            _placement_rejection(),
            _converse_text("ok", tokens=(5, 2)),
        ]
    )
    events = await _collect(_spec(), client)

    assert [e.type for e in events] == ["text_delta", "done"]
    assert client.converse.call_count == 3
    _assert_marker_free(client.converse.call_args_list[2].kwargs)
    meta = events[-1].metadata
    assert meta["iterations"] == 1
    assert meta["input_tokens"] == 5


@pytest.mark.asyncio
async def test_run_turn_placement_then_unsupported_rejection_strips_everything():
    """Mixed chain: tools-marker downgrade, then the no-caching signature → marker-free."""
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            _placement_rejection(),
            _cache_rejection(),
            _converse_text("ok"),
        ]
    )
    events = await _collect(_spec(), client)

    assert events[-1].type == "done"
    assert client.converse.call_count == 3
    _assert_marker_free(client.converse.call_args_list[2].kwargs)


@pytest.mark.asyncio
async def test_run_turn_plain_access_denied_does_not_retry():
    """(c) AccessDeniedException WITHOUT 'prompt caching' = real IAM failure — no retry."""
    err = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "User is not authorized"}},
        "Converse",
    )
    client = MagicMock()
    client.converse = MagicMock(side_effect=err)
    events = await _collect(_spec(), client)

    assert client.converse.call_count == 1
    assert events[-1].type == "error"
    assert "not authorized" in events[-1].content


@pytest.mark.asyncio
async def test_run_turn_cache_message_with_other_code_does_not_retry():
    """The match needs BOTH halves: the no-caching message under another code → no retry
    (and the placement matcher needs its own message half, so it doesn't fire either)."""
    err = ClientError(
        {"Error": {"Code": "ValidationException", "Message": CACHE_REJECTION_MSG}}, "Converse"
    )
    client = MagicMock()
    client.converse = MagicMock(side_effect=err)
    events = await _collect(_spec(), client)

    assert client.converse.call_count == 1
    assert events[-1].type == "error"


@pytest.mark.asyncio
async def test_run_turn_error_event_masks_aws_identifiers():
    """ARNs / account ids in a Bedrock error stay out of the user-facing transcript."""
    err = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": (
                    "User arn:aws:sts::123456789012:assumed-role/inkwest/session is not "
                    "authorized to invoke arn:aws:bedrock:eu-west-1::foundation-model/x"
                ),
            }
        },
        "Converse",
    )
    client = MagicMock()
    client.converse = MagicMock(side_effect=err)
    events = await _collect(_spec(), client)

    assert events[-1].type == "error"
    assert "arn:aws" not in events[-1].content
    assert "123456789012" not in events[-1].content
    assert "[arn]" in events[-1].content


def test_public_error_message_masks_arn_and_account():
    masked = loop_mod._public_error_message(
        "denied for arn:aws:iam::210987654321:role/foo (account 210987654321)"
    )
    assert masked == "denied for [arn] (account [account])"


@pytest.mark.asyncio
async def test_run_turn_history_stays_marker_free():
    """(d) Markers live on the converse-shaped copies only — history is never mutated."""
    history = [
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": [{"type": "text", "text": "earlier answer"}]},
    ]
    snapshot = copy.deepcopy(history)
    spec = _spec(history=history)
    client = _client(
        [
            _converse_tool("tu-1", "kubectl_get", {"resource": "pods"}),
            _converse_text("done."),
        ]
    )
    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("ok", False)),
    ):
        await _collect(spec, client)

    assert spec.context.history == snapshot
    assert "cachePoint" not in json.dumps(spec.context.history)


def test_add_cache_points_is_pure_and_places_markers():
    from inkwest_agent.converse import add_cache_points

    kwargs = {
        "modelId": "m",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "system": [{"text": "sys"}],
        "toolConfig": {"tools": [{"toolSpec": {"name": "t"}}]},
        "inferenceConfig": {"maxTokens": 5},
    }
    snapshot = copy.deepcopy(kwargs)
    out = add_cache_points(kwargs)

    assert kwargs == snapshot  # input untouched
    assert out["system"][-1] == CACHE_POINT
    assert out["toolConfig"]["tools"][-1] == CACHE_POINT
    assert out["messages"][-1]["content"][-1] == CACHE_POINT
    # Markers are fresh dicts per insertion — never a shared module-level object that an
    # in-place kwargs mutation could corrupt everywhere at once.
    assert out["system"][-1] is not out["toolConfig"]["tools"][-1]
    assert out["system"][-1] is not add_cache_points(kwargs)["system"][-1]


def test_add_cache_points_tools_marker_opt_out():
    """tools_marker=False (Nova downgrade) marks system+message but leaves tools alone."""
    from inkwest_agent.converse import add_cache_points

    kwargs = {
        "modelId": "m",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "system": [{"text": "sys"}],
        "toolConfig": {"tools": [{"toolSpec": {"name": "t"}}]},
        "inferenceConfig": {"maxTokens": 5},
    }
    out = add_cache_points(kwargs, tools_marker=False)
    _assert_markers_no_tools(out)


def test_add_cache_points_skips_tools_marker_when_no_tools():
    from inkwest_agent.converse import add_cache_points

    kwargs = {
        "modelId": "m",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        "system": [{"text": "sys"}],
        "toolConfig": {"tools": []},
        "inferenceConfig": {"maxTokens": 5},
    }
    out = add_cache_points(kwargs)
    assert out["toolConfig"]["tools"] == []
    assert out["system"][-1] == CACHE_POINT


def test_strip_cache_points_removes_all_markers_and_is_pure():
    from inkwest_agent.converse import add_cache_points, cache_point, strip_cache_points

    marked = add_cache_points(
        {
            "modelId": "m",
            "messages": [
                {"role": "user", "content": [{"text": "hi"}]},
                {"role": "assistant", "content": [{"text": "yo"}, cache_point()]},
            ],
            "system": [{"text": "sys"}],
            "toolConfig": {"tools": [{"toolSpec": {"name": "t"}}]},
            "inferenceConfig": {"maxTokens": 5},
        }
    )
    snapshot = copy.deepcopy(marked)
    out = strip_cache_points(marked)

    assert marked == snapshot  # input untouched
    _assert_marker_free(out)
    # Idempotent — stripping a stripped call changes nothing.
    assert strip_cache_points(out) == out


def test_is_cache_unsupported_rejection_matches_both_halves():
    from inkwest_agent.converse import is_cache_unsupported_rejection

    assert is_cache_unsupported_rejection(_cache_rejection())
    assert not is_cache_unsupported_rejection(
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Converse")
    )
    assert not is_cache_unsupported_rejection(
        ClientError(
            {"Error": {"Code": "ValidationException", "Message": CACHE_REJECTION_MSG}}, "Converse"
        )
    )
    # A malformed error payload (no Error/Message) must not crash the matcher.
    assert not is_cache_unsupported_rejection(ClientError({}, "Converse"))


def test_is_cache_placement_rejection_matches_both_halves():
    from inkwest_agent.converse import is_cache_placement_rejection

    assert is_cache_placement_rejection(_placement_rejection())
    # Same message under the other code, or another ValidationException → no match.
    assert not is_cache_placement_rejection(
        ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": PLACEMENT_REJECTION_MSG}},
            "Converse",
        )
    )
    assert not is_cache_placement_rejection(
        ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Malformed input request"}},
            "Converse",
        )
    )
    assert not is_cache_placement_rejection(ClientError({}, "Converse"))
