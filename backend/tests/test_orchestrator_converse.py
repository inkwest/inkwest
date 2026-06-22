"""Tests for the Bedrock Converse orchestrator (open-weight models in chat)."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import orchestrator_common as ocom
from app.services import orchestrator_converse as oc
from app.services import tool_execution as tex


async def _collect(gen):
    return [e async for e in gen]


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def test_to_converse_message_text_and_tool_use():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "kubectl_get", "input": {"resource": "pods"}},
        ],
    }
    out = oc._to_converse_message(msg)
    assert out["role"] == "assistant"
    assert out["content"][0] == {"text": "hi"}
    assert out["content"][1] == {
        "toolUse": {"toolUseId": "t1", "name": "kubectl_get", "input": {"resource": "pods"}}
    }


def test_to_converse_message_tool_result():
    msg = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": "pods listing",
                "is_error": False,
            }
        ],
    }
    out = oc._to_converse_message(msg)
    tr = out["content"][0]["toolResult"]
    assert tr["toolUseId"] == "t1"
    assert tr["content"] == [{"text": "pods listing"}]
    assert tr["status"] == "success"


def test_to_converse_message_string_content():
    out = oc._to_converse_message({"role": "user", "content": "hello"})
    assert out["content"] == [{"text": "hello"}]


def test_to_converse_message_drops_thinking_and_stubs_empty():
    msg = {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}]}
    out = oc._to_converse_message(msg)
    # thinking dropped -> empty -> stub keeps Converse happy
    assert out["content"] == [{"text": "(no content)"}]


def test_to_converse_message_error_tool_result_status():
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "boom", "is_error": True}
        ],
    }
    assert oc._to_converse_message(msg)["content"][0]["toolResult"]["status"] == "error"


def test_to_converse_tools_shape():
    with patch.object(
        oc,
        "get_tool_definitions",
        return_value=[
            {"name": "kubectl_get", "description": "d", "input_schema": {"type": "object"}}
        ],
    ):
        cfg = oc._to_converse_tools()
    assert cfg["tools"][0]["toolSpec"] == {
        "name": "kubectl_get",
        "description": "d",
        "inputSchema": {"json": {"type": "object"}},
    }


# ---------------------------------------------------------------------------
# Orchestration loop
# ---------------------------------------------------------------------------


def _patches(model="meta.llama3-1-70b-instruct-v1:0", entry=None):
    """Patch the orchestrator's collaborators. `entry` is the registry lookup result."""
    return [
        patch.object(oc, "get_session_messages", AsyncMock(return_value=[])),
        patch.object(oc, "save_message", AsyncMock()),
        patch.object(oc, "ensure_session_title", AsyncMock()),
        patch.object(oc, "resolve_session_system_prompt", AsyncMock(return_value="base")),
        patch.object(oc, "get_system_prompt", MagicMock(return_value="SYS")),
        patch.object(oc, "resolve_session_model", AsyncMock(return_value=model)),
        # usage/stats persistence lives in orchestrator_common.finalize_turn.
        patch.object(ocom, "record_usage", AsyncMock()),
        patch.object(ocom, "update_session_stats", AsyncMock()),
        patch.object(oc, "get_tool_definitions", MagicMock(return_value=[])),
        patch.object(oc.model_registry, "get_entry", MagicMock(return_value=entry)),
    ]


@pytest.mark.asyncio
async def test_converse_text_only_turn():
    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        return_value={
            "output": {"message": {"content": [{"text": "Hello there."}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 4},
        }
    )
    record_usage = AsyncMock()
    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
        patch.object(ocom, "record_usage", record_usage),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    types = [e.type for e in events]
    assert "text_delta" in types
    assert types[-1] == "done"
    assert any(e.content == "Hello there." for e in events if e.type == "text_delta")
    record_usage.assert_awaited_once()
    assert record_usage.await_args.kwargs["input_tokens"] == 10


@pytest.mark.asyncio
async def test_converse_tool_use_then_finish():
    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            {
                "output": {
                    "message": {
                        "content": [
                            {"text": "Checking."},
                            {
                                "toolUse": {
                                    "toolUseId": "t1",
                                    "name": "kubectl_get",
                                    "input": {"resource": "pods"},
                                }
                            },
                        ]
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 10, "outputTokens": 5},
            },
            {
                "output": {"message": {"content": [{"text": "All good."}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 8, "outputTokens": 3},
            },
        ]
    )
    execute_tool = AsyncMock(return_value=("pod1 Running", False))
    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
        patch.object(tex, "execute_tool", execute_tool),
        patch.object(tex, "connector_for_tool", MagicMock(return_value=None)),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name="c",
                kubeconfig_path="/k",
                context_name=None,
                user_message="check pods",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    types = [e.type for e in events]
    assert "tool_start" in types
    assert "tool_result" in types
    assert types[-1] == "done"
    execute_tool.assert_awaited_once()
    assert client.converse.call_count == 2
    # done metadata accumulates both iterations' tokens
    done = events[-1]
    assert done.metadata["input_tokens"] == 18
    assert done.metadata["tool_calls"] == 1


@pytest.mark.asyncio
async def test_converse_uses_arn_and_max_tokens_from_entry():
    """A custom row pins the invoke target (ARN) and a max_tokens cap."""
    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        return_value={
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
    )
    entry = SimpleNamespace(arn="arn:aws:bedrock:eu-west-1::model/x", options={"max_tokens": 256})
    ctx = [
        *_patches(model="custom-llama", entry=entry),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
    ]
    for p in ctx:
        p.start()
    try:
        await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    kwargs = client.converse.call_args.kwargs
    assert kwargs["modelId"] == "arn:aws:bedrock:eu-west-1::model/x"
    assert kwargs["inferenceConfig"]["maxTokens"] == 256


@pytest.mark.asyncio
async def test_converse_client_error_yields_error_event():
    from botocore.exceptions import ClientError

    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=ClientError(
            {"Error": {"Code": "ValidationException", "Message": "unsupported model"}}, "Converse"
        )
    )
    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    assert events[-1].type == "error"
    assert "unsupported model" in events[-1].content


@pytest.mark.asyncio
async def test_converse_cancelled_mid_tool_loop():
    """Cancel firing during tool execution stops the turn (shared loop branch)."""
    import asyncio

    cancel = asyncio.Event()

    async def exec_then_cancel(*a, **k):
        cancel.set()  # next loop-top check trips
        return ("out", False)

    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        return_value={
            "output": {
                "message": {
                    "content": [
                        {"toolUse": {"toolUseId": "t1", "name": "kubectl_get", "input": {}}},
                        {"toolUse": {"toolUseId": "t2", "name": "kubectl_get", "input": {}}},
                    ]
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
    )
    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
        patch.object(tex, "execute_tool", AsyncMock(side_effect=exec_then_cancel)),
        patch.object(tex, "connector_for_tool", MagicMock(return_value=None)),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
                cancel_event=cancel,
            )
        )
    finally:
        for p in ctx:
            p.stop()

    assert events[-1].type == "done"
    assert events[-1].metadata.get("stopped") is True
    # Only the first tool ran before cancel tripped the second iteration.
    assert client.converse.call_count == 1


@pytest.mark.asyncio
async def test_converse_tool_exception_surfaces_error_result():
    """A tool raising becomes an is_error tool_result, not a turn crash."""
    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            {
                "output": {
                    "message": {
                        "content": [
                            {"toolUse": {"toolUseId": "t1", "name": "kubectl_get", "input": {}}}
                        ]
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
            {
                "output": {"message": {"content": [{"text": "recovered"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
        ]
    )
    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
        patch.object(tex, "execute_tool", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(tex, "connector_for_tool", MagicMock(return_value=None)),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    err = next(e for e in events if e.type == "tool_result")
    assert err.is_error is True
    assert "Error executing tool" in err.content
    assert events[-1].type == "done"  # turn still completes


@pytest.mark.asyncio
async def test_converse_usage_failure_flagged_in_done():
    """If stats/usage persistence fails, the turn still completes with the flag."""
    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        return_value={
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
    )
    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
        patch.object(ocom, "record_usage", AsyncMock(side_effect=RuntimeError("db down"))),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    assert events[-1].type == "done"
    assert events[-1].metadata.get("usage_tracking_failed") is True


@pytest.mark.asyncio
async def test_converse_emits_connector_required():
    """A needs-auth tool result surfaces a first-class connector_required event."""
    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            {
                "output": {
                    "message": {
                        "content": [
                            {"toolUse": {"toolUseId": "t1", "name": "gitlab_x", "input": {}}}
                        ]
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
            {
                "output": {"message": {"content": [{"text": "done"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
        ]
    )
    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
        patch.object(tex, "execute_tool", AsyncMock(return_value=("needs auth", True))),
        patch.object(tex, "connector_for_tool", MagicMock(return_value="gitlab")),
        patch.object(tex, "is_needs_auth", MagicMock(return_value=True)),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    conn = next(e for e in events if e.type == "connector_required")
    assert conn.metadata["connector"] == "gitlab"


@pytest.mark.asyncio
async def test_converse_tool_result_save_failure_continues():
    """A failed tool_result persist is logged-and-ignored; the turn still finishes."""
    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        side_effect=[
            {
                "output": {
                    "message": {
                        "content": [
                            {"toolUse": {"toolUseId": "t1", "name": "kubectl_get", "input": {}}}
                        ]
                    }
                },
                "stopReason": "tool_use",
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
            {
                "output": {"message": {"content": [{"text": "ok"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
        ]
    )

    async def _save(db, session_id, role, *a, **k):
        if role == "tool_result":
            raise RuntimeError("db down")  # only the tool_result persist fails
        return None

    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
        patch.object(oc, "save_message", AsyncMock(side_effect=_save)),
        patch.object(tex, "execute_tool", AsyncMock(return_value=("out", False))),
        patch.object(tex, "connector_for_tool", MagicMock(return_value=None)),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    assert events[-1].type == "done"  # tool_result save failure doesn't abort the turn


@pytest.mark.asyncio
async def test_converse_ignores_non_int_max_tokens():
    """A non-int options.max_tokens is ignored — the default cap applies."""
    from app.core.constants import MAX_RESPONSE_TOKENS

    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    client.converse = MagicMock(
        return_value={
            "output": {"message": {"content": [{"text": "ok"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }
    )
    entry = SimpleNamespace(arn=None, options={"max_tokens": "8192"})  # string, not int
    ctx = [
        *_patches(entry=entry),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
    ]
    for p in ctx:
        p.start()
    try:
        await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    finally:
        for p in ctx:
            p.stop()

    assert client.converse.call_args.kwargs["inferenceConfig"]["maxTokens"] == MAX_RESPONSE_TOKENS


@pytest.mark.asyncio
async def test_converse_cancelled_before_call():
    import asyncio

    settings = MagicMock(aws_region="eu-west-1", aws_access_key_id=None, aws_secret_access_key=None)
    client = MagicMock()
    cancel = asyncio.Event()
    cancel.set()
    ctx = [
        *_patches(),
        patch.object(oc, "get_settings", MagicMock(return_value=settings)),
        patch.object(oc, "_make_bedrock_client", MagicMock(return_value=client)),
    ]
    for p in ctx:
        p.start()
    try:
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
                cancel_event=cancel,
            )
        )
    finally:
        for p in ctx:
            p.stop()

    assert events[-1].type == "done"
    assert events[-1].metadata.get("stopped") is True
    client.converse.assert_not_called()


@pytest.mark.asyncio
async def test_converse_missing_region_errors():
    settings = MagicMock(aws_region=None)
    with patch.object(oc, "get_settings", MagicMock(return_value=settings)):
        events = await _collect(
            oc.orchestrate_chat_converse(
                db=AsyncMock(),
                session_id=uuid.uuid4(),
                cluster_name=None,
                kubeconfig_path=None,
                context_name=None,
                user_message="hi",
            )
        )
    assert events[0].type == "error"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def _route_for(model: str, entry=None) -> str:
    """Run the bedrock dispatcher for a model; return which path it picked.

    `entry` is the registry lookup result (None = not in catalog -> ID heuristic).
    """
    from app.services.ai_client import get_orchestrator

    captured = {}

    async def fake_converse(**kwargs):
        captured["called"] = "converse"
        yield
        return

    async def fake_anthropic(**kwargs):
        captured["called"] = "anthropic"
        yield
        return

    # Patch the source modules BEFORE get_orchestrator runs: it does
    # `from … import …` at call time, so the dispatcher closes over these.
    with (
        patch(
            "app.services.ai_client.get_settings",
            MagicMock(return_value=MagicMock(ai_provider="bedrock")),
        ),
        patch("app.services.ai_client.resolve_session_model", AsyncMock(return_value=model)),
        patch("app.services.ai_client.get_entry", MagicMock(return_value=entry)),
        patch("app.services.orchestrator_converse.orchestrate_chat_converse", fake_converse),
        patch("app.services.orchestrator.orchestrate_chat", fake_anthropic),
    ):
        dispatch = get_orchestrator()
        await _collect(dispatch(db=AsyncMock(), session_id="s1", user_message="x"))
    return captured["called"]


@pytest.mark.asyncio
async def test_get_orchestrator_routes_open_model_to_converse():
    # Not in the registry -> falls back to the ID heuristic.
    assert await _route_for("meta.llama3-1-70b-instruct-v1:0") == "converse"


@pytest.mark.asyncio
async def test_get_orchestrator_routes_claude_model_to_anthropic():
    assert await _route_for("eu.anthropic.claude-opus-4-8") == "anthropic"


@pytest.mark.asyncio
async def test_routing_honors_registry_provider_over_id_heuristic():
    # ID contains 'claude' (would fool the heuristic) but the admin tagged it open.
    entry = SimpleNamespace(provider="bedrock-open")
    assert await _route_for("my-claude-gateway-alias", entry=entry) == "converse"


@pytest.mark.asyncio
async def test_routing_per_vendor_provider_tag():
    # Per-vendor tags: a non-Anthropic vendor routes via Converse...
    assert await _route_for("x", entry=SimpleNamespace(provider="meta")) == "converse"
    assert await _route_for("x", entry=SimpleNamespace(provider="mistral")) == "converse"
    # ...while the anthropic vendor tag stays on the Messages path.
    assert await _route_for("x", entry=SimpleNamespace(provider="anthropic")) == "anthropic"
