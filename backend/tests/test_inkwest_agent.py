"""Tests for the standalone inkwest_agent package (loop + protocol + entrypoint)."""

import io
import json
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
