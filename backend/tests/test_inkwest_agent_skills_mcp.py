"""Tests for the inkwest-agent skills + MCP surfaces (P1.7).

Skills-as-tools (progressive disclosure), MCP tool translation / routing / graceful
degradation when the optional `mcp` SDK is absent, and the loop wiring that routes
read_skill / MCP tool calls.
"""

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.events import ChatEvent
from inkwest_agent import clock as clock_mod
from inkwest_agent import converse as converse_mod
from inkwest_agent import loop as loop_mod
from inkwest_agent import mcp as mcp_mod
from inkwest_agent import skills as skills_mod
from inkwest_agent.converse import (
    CURRENT_DATETIME_TOOL,
    READ_SKILL_TOOL,
    skill_tool_spec,
    to_converse_tools,
)
from inkwest_agent.mcp import MCPManager, load_mcp_config
from inkwest_agent.protocol import (
    PROTOCOL_VERSION,
    SkillSpec,
    TurnSpec,
    TurnSpecError,
    parse_turn_spec,
)

SESSION_ID = "11111111-1111-1111-1111-111111111111"

_BASE_SPEC = {
    "session_id": SESSION_ID,
    "user_message": "x",
    "model": "m",
    "system_prompt": "s",
    "region": "eu-west-1",
}


# --------------------------------------------------------------------------- #
# protocol — skills field
# --------------------------------------------------------------------------- #


def test_parse_turn_spec_skills():
    spec = parse_turn_spec(
        {
            **_BASE_SPEC,
            "skills": [{"name": "triage-pod", "description": "diagnose a crashlooping pod"}],
            "skills_root": "/home/agent/.claude/skills",
            "mcp_config_path": "/home/agent/.claude/inkwest-agent-mcp.json",
        }
    )
    assert spec.skills == [SkillSpec(name="triage-pod", description="diagnose a crashlooping pod")]
    assert spec.skills_root == "/home/agent/.claude/skills"
    assert spec.mcp_config_path == "/home/agent/.claude/inkwest-agent-mcp.json"


def test_parse_turn_spec_skills_must_be_list():
    with pytest.raises(TurnSpecError, match="'skills' must be a list"):
        parse_turn_spec({**_BASE_SPEC, "skills": {}})


def test_parse_turn_spec_skill_entry_needs_name():
    with pytest.raises(TurnSpecError, match="'skills' entry missing 'name'"):
        parse_turn_spec({**_BASE_SPEC, "skills": [{"description": "no name"}]})


def test_parse_turn_spec_skill_entry_rejects_unknown_field():
    with pytest.raises(TurnSpecError, match="'skills' entry has unknown"):
        parse_turn_spec({**_BASE_SPEC, "skills": [{"name": "s", "body": "leak"}]})


def test_skillspec_description_defaults_empty():
    # A null description normalizes to "" so the tool catalog never renders "None".
    spec = parse_turn_spec({**_BASE_SPEC, "skills": [{"name": "s", "description": None}]})
    assert spec.skills[0].description == ""


# --------------------------------------------------------------------------- #
# converse — synthetic skill tool + merge
# --------------------------------------------------------------------------- #


def test_skill_tool_spec_none_when_no_skills():
    assert skill_tool_spec([]) is None


def test_skill_tool_spec_lists_descriptions_and_enums_names():
    skills = [
        SkillSpec(name="triage-pod", description="diagnose a crashlooping pod"),
        SkillSpec(name="audit-iam", description="review IAM drift"),
    ]
    spec = skill_tool_spec(skills)
    assert spec is not None
    ts = spec["toolSpec"]
    assert ts["name"] == READ_SKILL_TOOL
    # Progressive disclosure: each short description is in the always-visible tool desc.
    assert "diagnose a crashlooping pod" in ts["description"]
    assert "review IAM drift" in ts["description"]
    # The enum constrains the name to the advertised set (no arbitrary file reads).
    assert ts["inputSchema"]["json"]["properties"]["name"]["enum"] == ["triage-pod", "audit-iam"]


def test_to_converse_tools_merges_skill_and_mcp(monkeypatch):
    monkeypatch.setattr(
        converse_mod,
        "get_tool_definitions",
        lambda: [{"name": "kubectl_get", "description": "d", "input_schema": {"type": "object"}}],
    )
    mcp_tools = [
        {"toolSpec": {"name": "mcp__cx__query", "description": "q", "inputSchema": {"json": {}}}}
    ]
    cfg = to_converse_tools(skills=[SkillSpec(name="s", description="d")], mcp_tools=mcp_tools)
    names = [t["toolSpec"]["name"] for t in cfg["tools"]]
    # current_datetime + the always-on exec tools are advertised right after the base tools,
    # then the skill loader, then MCP-owned tools.
    assert names == [
        "kubectl_get",
        CURRENT_DATETIME_TOOL,
        "bash",
        "read_file",
        "write_file",
        READ_SKILL_TOOL,
        "mcp__cx__query",
    ]


def test_datetime_tool_always_advertised(monkeypatch):
    # The clock has no skills/MCP precondition — it must be present even with neither.
    monkeypatch.setattr(
        converse_mod,
        "get_tool_definitions",
        lambda: [{"name": "kubectl_get", "description": "d", "input_schema": {"type": "object"}}],
    )
    cfg = to_converse_tools()
    spec = next(
        t["toolSpec"] for t in cfg["tools"] if t["toolSpec"]["name"] == CURRENT_DATETIME_TOOL
    )
    # No-arg tool → empty-object schema (Converse requires an object schema).
    assert spec["inputSchema"]["json"] == {"type": "object", "properties": {}}


# --------------------------------------------------------------------------- #
# skills executor
# --------------------------------------------------------------------------- #


def test_read_skill_returns_body(tmp_path):
    skill_dir = tmp_path / "triage-pod"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: triage-pod\n---\n# Steps\n1. look")
    body, is_error = skills_mod.read_skill(
        {"name": "triage-pod"},
        [SkillSpec(name="triage-pod", description="d")],
        str(tmp_path),
    )
    assert not is_error
    assert "# Steps" in body


def test_read_skill_rejects_unknown_name(tmp_path):
    body, is_error = skills_mod.read_skill(
        {"name": "../../etc/passwd"},
        [SkillSpec(name="triage-pod", description="d")],
        str(tmp_path),
    )
    assert is_error
    assert "unknown skill" in body


def test_read_skill_requires_name(tmp_path):
    body, is_error = skills_mod.read_skill({}, [SkillSpec(name="s")], str(tmp_path))
    assert is_error and "required" in body


def test_read_skill_missing_body_file(tmp_path):
    body, is_error = skills_mod.read_skill(
        {"name": "triage-pod"}, [SkillSpec(name="triage-pod")], str(tmp_path)
    )
    assert is_error and "body is missing" in body


def test_is_skill_tool():
    assert skills_mod.is_skill_tool(READ_SKILL_TOOL)
    assert not skills_mod.is_skill_tool("kubectl_get")


def test_is_datetime_tool():
    assert clock_mod.is_datetime_tool(CURRENT_DATETIME_TOOL)
    assert not clock_mod.is_datetime_tool("kubectl_get")


def test_current_datetime_returns_utc_iso():
    from datetime import datetime, timedelta

    output, is_error = clock_mod.current_datetime({})
    assert is_error is False
    parsed = datetime.fromisoformat(output)  # must round-trip as a valid ISO-8601 instant
    assert parsed.utcoffset() == timedelta(0)  # UTC-aware (never naive) and zero-offset


def test_read_skill_os_error(tmp_path):
    # A non-FileNotFound OSError (e.g. permission) degrades to an error tool_result.
    with patch("builtins.open", side_effect=PermissionError("denied")):
        body, is_error = skills_mod.read_skill(
            {"name": "triage-pod"}, [SkillSpec(name="triage-pod")], str(tmp_path)
        )
    assert is_error and "error reading skill" in body.lower()


def test_read_skill_no_skills_root():
    body, is_error = skills_mod.read_skill(
        {"name": "triage-pod"}, [SkillSpec(name="triage-pod")], None
    )
    assert is_error and "not available" in body


# --------------------------------------------------------------------------- #
# MCP — config load + schema translation + degradation
# --------------------------------------------------------------------------- #


def test_load_mcp_config_none_path():
    assert load_mcp_config(None) == {}


def test_load_mcp_config_missing_file(tmp_path):
    assert load_mcp_config(str(tmp_path / "nope.json")) == {}


def test_load_mcp_config_malformed(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text("{not json")
    assert load_mcp_config(str(p)) == {}


def test_load_mcp_config_reads_servers(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {"cx": {"type": "http", "url": "https://x/mcp"}}}))
    assert load_mcp_config(str(p)) == {"cx": {"type": "http", "url": "https://x/mcp"}}


def test_fit_tool_name_passthrough_when_short():
    # A name within Converse's 64-char ToolName limit is left verbatim (Claude Code parity).
    assert converse_mod.fit_tool_name("mcp__aws__list-buckets") == "mcp__aws__list-buckets"


def test_fit_tool_name_sanitizes_illegal_chars():
    # ToolName forbids '.'/':'/space etc.; an upstream tool name we don't control that uses
    # them must be folded to '_' or Converse 400s on the pattern (not just the length).
    assert converse_mod.fit_tool_name("mcp__aws__list.buckets") == "mcp__aws__list_buckets"
    got = converse_mod.fit_tool_name("mcp__x__get user/all:v2")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", got)


def test_fit_tool_name_empty_stays_valid():
    # ToolName is {1,64}: an empty (or all-illegal-then-stripped) name must NOT fold to ""
    # (which fails the min-length and 400s Converse — the very thing fit_tool_name guards).
    got = converse_mod.fit_tool_name("")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", got)


def test_fit_tool_name_idempotent():
    # A fitted name is already a legal ToolName, so re-fitting (every replay turn) is a no-op.
    # Guards replay stability against a future hash-length bump that could push past 64.
    for raw in ("mcp__aws__s3-ls", "mcp__srv__weird.name:x", "mcp__" + "z" * 80):
        once = converse_mod.fit_tool_name(raw)
        assert converse_mod.fit_tool_name(once) == once
        assert len(once) <= converse_mod._MAX_TOOL_NAME_LEN


def test_fit_tool_name_boundary_exactly_64_passthrough():
    # Exactly 64 is legal → verbatim; 65 folds. Pins the off-by-one on the '<=' bound.
    at_64 = "m" * converse_mod._MAX_TOOL_NAME_LEN
    assert converse_mod.fit_tool_name(at_64) == at_64
    over = "m" * (converse_mod._MAX_TOOL_NAME_LEN + 1)
    assert len(converse_mod.fit_tool_name(over)) == converse_mod._MAX_TOOL_NAME_LEN


def test_fit_tool_name_folds_overflow_to_64():
    # Bedrock Converse rejects a tool name > 64 chars (ToolName shape) and fails the whole
    # turn; a long name we don't control must be folded down deterministically.
    full = "mcp__AcmeOpsHub-production__get-tenant-account-member-permissions"
    assert len(full) > converse_mod._MAX_TOOL_NAME_LEN  # the reported failure (65 chars)
    name = converse_mod.fit_tool_name(full)
    assert len(name) == converse_mod._MAX_TOOL_NAME_LEN
    # Deterministic (replay coherence) + collision-resistant (hash of the FULL name).
    assert name == converse_mod.fit_tool_name(full)
    assert name != converse_mod.fit_tool_name(full + "-other")
    # Only chars Converse allows ([a-zA-Z0-9_-]).
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)


def test_advertised_name_matches_fit_of_full():
    # _advertised_name delegates to fit_tool_name on the full namespaced string, so it agrees
    # with what to_converse_message produces for a replayed history name (cross-engine).
    server, tool = "AcmeOpsHub-production", "get-tenant-account-member-permissions"
    full = f"mcp__{server}__{tool}"
    assert mcp_mod._advertised_name(server, tool) == converse_mod.fit_tool_name(full)
    assert mcp_mod._advertised_name("aws", "s3-ls") == "mcp__aws__s3-ls"  # short: verbatim


def test_to_converse_message_folds_long_tool_use_name():
    # Cross-engine: Claude Code persists the FULL >64 name into the shared messages table;
    # replaying it to Converse must fold ToolUseBlock.name identically to the advertised name,
    # else the turn 400s on messages[].content[].toolUse.name (same ToolName cap as toolConfig).
    full = "mcp__AcmeOpsHub-production__get-tenant-account-member-permissions"
    msg = {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "tu1", "name": full, "input": {"a": 1}}],
    }
    out = converse_mod.to_converse_message(msg)
    tu = out["content"][0]["toolUse"]
    assert tu["name"] == converse_mod.fit_tool_name(full)
    assert len(tu["name"]) == converse_mod._MAX_TOOL_NAME_LEN
    assert tu["toolUseId"] == "tu1" and tu["input"] == {"a": 1}


def test_mcp_schema_to_converse_normalizes_missing_schema():
    spec = mcp_mod._mcp_schema_to_converse("cx__q", "desc", {})
    assert spec["toolSpec"]["inputSchema"]["json"] == {"type": "object", "properties": {}}
    assert spec["toolSpec"]["description"] == "desc"


def test_mcp_result_to_text_concatenates_text_blocks():
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text="line1"), SimpleNamespace(text="line2")],
    )
    text, is_error = mcp_mod._result_to_text(result)
    assert text == "line1\nline2"
    assert not is_error


def test_mcp_result_to_text_flags_error_and_nontext():
    result = SimpleNamespace(isError=True, content=[SimpleNamespace(type="image", text=None)])
    text, is_error = mcp_mod._result_to_text(result)
    assert is_error
    assert "[image content]" in text


@pytest.mark.asyncio
async def test_mcp_manager_degrades_when_sdk_absent():
    # The crux of the dependency decision: a missing `mcp` SDK must NOT break the turn —
    # the manager opens to an empty tool set and owns nothing.
    with patch.object(mcp_mod, "mcp_sdk_available", return_value=False):
        async with MCPManager({"cx": {"type": "http", "url": "https://x/mcp"}}) as mgr:
            assert mgr.converse_tools == []
            assert not mgr.owns("cx__query")


@pytest.mark.asyncio
async def test_mcp_manager_empty_servers_is_noop():
    async with MCPManager({}) as mgr:
        assert mgr.converse_tools == []


@pytest.mark.asyncio
async def test_mcp_manager_connects_and_routes(monkeypatch):
    # Connection is exercised with a stubbed SDK so it runs without the real `mcp` package:
    # connect → list_tools → merge into Converse → route a call → translate the result.
    monkeypatch.setattr(mcp_mod, "mcp_sdk_available", lambda: True)

    fake_session = AsyncMock()
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(
        return_value=SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="query", description="run a query", inputSchema={"type": "object"}
                )
            ]
        )
    )
    fake_session.call_tool = AsyncMock(
        return_value=SimpleNamespace(isError=False, content=[SimpleNamespace(text="42 rows")])
    )

    # _connect_one does `from mcp import ClientSession`; inject a fake `mcp` module whose
    # ClientSession is an async context manager yielding our fake session, so the real
    # per-server AsyncExitStack path runs without the optional SDK installed.
    class _FakeClientSession:
        def __init__(self, read, write):
            pass

        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *exc):
            return None

    fake_mcp = SimpleNamespace(ClientSession=_FakeClientSession)
    monkeypatch.setitem(__import__("sys").modules, "mcp", fake_mcp)

    mgr = MCPManager({"cx": {"type": "http", "url": "https://x/mcp"}})

    async def fake_open_transport(stack, server_def):
        return (MagicMock(), MagicMock())

    monkeypatch.setattr(mgr, "_open_transport", fake_open_transport)

    await mgr.open()
    try:
        # mcp__<server>__<tool> (Claude Code parity).
        assert mgr.owns("mcp__cx__query")
        names = [t["toolSpec"]["name"] for t in mgr.converse_tools]
        assert names == ["mcp__cx__query"]
        text, is_error = await mgr.call("mcp__cx__query", {"q": "SELECT 1"})
        assert text == "42 rows" and not is_error
        fake_session.call_tool.assert_awaited_once_with("query", {"q": "SELECT 1"})
        # Unknown tool routes to an error, not a crash.
        err, is_err = await mgr.call("mcp__cx__missing", {})
        assert is_err and "unknown MCP tool" in err
    finally:
        await mgr.aclose()


@pytest.mark.asyncio
async def test_mcp_manager_isolates_one_bad_server(monkeypatch):
    # One server failing to connect must not stop the others (or the turn).
    monkeypatch.setattr(mcp_mod, "mcp_sdk_available", lambda: True)
    mgr = MCPManager({"bad": {"type": "http", "url": "https://x"}})

    async def boom(key, server_def):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(mgr, "_connect_one", boom)
    await mgr.open()  # must not raise
    assert mgr.converse_tools == []


# --------------------------------------------------------------------------- #
# loop wiring — read_skill + MCP routing
# --------------------------------------------------------------------------- #


def _spec_with(**overrides) -> TurnSpec:
    return parse_turn_spec({**_BASE_SPEC, **overrides})


def _client(responses: list) -> MagicMock:
    client = MagicMock()
    client.converse = MagicMock(side_effect=responses)
    return client


def _tool_use(tool_use_id: str, name: str, tool_input: dict) -> dict:
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


def _final(text: str) -> dict:
    return {
        "usage": {"inputTokens": 1, "outputTokens": 1},
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": "end_turn",
    }


async def _collect(spec: TurnSpec, client) -> list[ChatEvent]:
    return [ev async for ev in loop_mod.run_turn(spec, client)]


@pytest.mark.asyncio
async def test_loop_routes_read_skill_tool(tmp_path):
    skill_dir = tmp_path / "triage-pod"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Triage steps")
    spec = _spec_with(
        skills=[{"name": "triage-pod", "description": "diagnose"}],
        skills_root=str(tmp_path),
    )
    client = _client([_tool_use("t1", READ_SKILL_TOOL, {"name": "triage-pod"}), _final("done")])
    events = await _collect(spec, client)
    by_type = [e.type for e in events]
    assert "tool_start" in by_type and "tool_result" in by_type and by_type[-1] == "done"
    result = next(e for e in events if e.type == "tool_result")
    assert result.tool_name == READ_SKILL_TOOL
    assert "Triage steps" in (result.content or "")


@pytest.mark.asyncio
async def test_loop_routes_datetime_tool():
    # The loop must dispatch current_datetime to the synthetic clock, not the base executor.
    spec = _spec_with()
    client = _client([_tool_use("t1", CURRENT_DATETIME_TOOL, {}), _final("done")])
    events = await _collect(spec, client)
    result = next(e for e in events if e.type == "tool_result")
    assert result.tool_name == CURRENT_DATETIME_TOOL
    assert result.is_error is False
    from datetime import datetime

    datetime.fromisoformat(result.content or "")  # the dispatched result is a valid ISO instant


@pytest.mark.asyncio
async def test_loop_routes_mcp_tool(monkeypatch):
    # The loop must dispatch an MCP-owned tool call to the manager, not the base executor.
    spec = _spec_with(mcp_config_path="/whatever")

    fake_mgr = MagicMock()
    fake_mgr.converse_tools = [
        {"toolSpec": {"name": "mcp__cx__query", "description": "q", "inputSchema": {"json": {}}}}
    ]
    fake_mgr.owns = lambda n: n == "mcp__cx__query"
    fake_mgr.call = AsyncMock(return_value=("ok rows", False))
    fake_mgr.__aenter__ = AsyncMock(return_value=fake_mgr)
    fake_mgr.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr(loop_mod, "MCPManager", lambda servers: fake_mgr)
    monkeypatch.setattr(loop_mod, "load_mcp_config", lambda p: {"cx": {}})

    client = _client([_tool_use("t1", "mcp__cx__query", {"q": "SELECT 1"}), _final("done")])
    events = await _collect(spec, client)
    fake_mgr.call.assert_awaited_once_with("mcp__cx__query", {"q": "SELECT 1"})
    result = next(e for e in events if e.type == "tool_result")
    assert result.content == "ok rows"


@pytest.mark.asyncio
async def test_loop_three_way_routing_in_one_turn(monkeypatch, tmp_path):
    # One assistant turn emitting read_skill + an MCP tool + a base kubectl tool must yield
    # three tool_result events in input order, each aligned to its tool_use_id, and the base
    # branch (execute_tool_calls → execute_tool) must be exercised.
    skill_dir = tmp_path / "triage-pod"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Triage")
    spec = _spec_with(
        skills=[{"name": "triage-pod", "description": "diagnose"}],
        skills_root=str(tmp_path),
        mcp_config_path="/whatever",
    )

    fake_mgr = MagicMock()
    fake_mgr.converse_tools = []
    fake_mgr.owns = lambda n: n == "mcp__cx__query"
    fake_mgr.call = AsyncMock(return_value=("mcp ok", False))
    fake_mgr.__aenter__ = AsyncMock(return_value=fake_mgr)
    fake_mgr.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(loop_mod, "MCPManager", lambda servers: fake_mgr)
    monkeypatch.setattr(loop_mod, "load_mcp_config", lambda p: {"cx": {}})

    multi = {
        "usage": {"inputTokens": 5, "outputTokens": 3},
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "s1",
                            "name": READ_SKILL_TOOL,
                            "input": {"name": "triage-pod"},
                        }
                    },
                    {"toolUse": {"toolUseId": "m1", "name": "mcp__cx__query", "input": {"q": "x"}}},
                    {
                        "toolUse": {
                            "toolUseId": "k1",
                            "name": "kubectl_get",
                            "input": {"resource": "pods"},
                        }
                    },
                ]
            }
        },
        "stopReason": "tool_use",
    }
    client = _client([multi, _final("done")])

    with patch(
        "app.services.tool_execution.execute_tool",
        new=AsyncMock(return_value=("pod/foo Running", False)),
    ) as base_exec:
        events = await _collect(spec, client)

    results = [e for e in events if e.type == "tool_result"]
    assert [r.tool_use_id for r in results] == ["s1", "m1", "k1"]  # input order preserved
    assert "Triage" in (results[0].content or "")
    assert results[1].content == "mcp ok"
    assert results[2].content == "pod/foo Running"
    base_exec.assert_awaited_once()  # base branch exercised
    fake_mgr.call.assert_awaited_once_with("mcp__cx__query", {"q": "x"})


# --------------------------------------------------------------------------- #
# protocol v3
# --------------------------------------------------------------------------- #


def test_protocol_version_is_6():
    # P1.8 bumped 3 -> 4 (image attachments); the LLM broker bumped 4 -> 5
    # (bedrock_endpoint_url); the exec tools bumped 5 -> 6 (limits.bash_timeout_seconds).
    # Bump this when the wire shape changes again.
    assert PROTOCOL_VERSION == 6


def test_p14_shape_spec_has_empty_p17_fields():
    # A minimal P1.4-shape spec (no skills/MCP) defaults the new fields to empty/None —
    # backward compatible, the agent simply advertises no skill/MCP tools.
    spec = parse_turn_spec(_BASE_SPEC)
    assert spec.skills == []
    assert spec.skills_root is None
    assert spec.mcp_config_path is None


# --------------------------------------------------------------------------- #
# MCP transport selection + eviction + collision dedupe
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "server_def, expected, has_headers",
    [
        ({"command": "uvx", "args": ["srv"], "env": {"K": "v"}}, "stdio", False),
        ({"type": "stdio", "command": "x"}, "stdio", False),
        (
            {"type": "sse", "url": "https://x/sse", "headers": {"Authorization": "Bearer t"}},
            "sse",
            True,
        ),
        (
            {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer t"}},
            "http",
            True,
        ),
        ({"url": "https://x/mcp"}, "http", False),  # bare url -> streamable http
    ],
)
@pytest.mark.asyncio
async def test_open_transport_selects_right_transport(
    monkeypatch, server_def, expected, has_headers
):
    import contextlib

    calls = {}

    def _record(kind):
        def factory(*args, **kwargs):
            calls["kind"] = kind
            calls["args"] = args
            calls["kwargs"] = kwargs
            return MagicMock()  # the real stack.enter_async_context is also stubbed below

        return factory

    fake_mcp = SimpleNamespace(StdioServerParameters=lambda **kw: ("params", kw))
    fake_stdio = SimpleNamespace(stdio_client=_record("stdio"))
    fake_sse = SimpleNamespace(sse_client=_record("sse"))
    fake_http = SimpleNamespace(streamablehttp_client=_record("http"))
    monkeypatch.setitem(__import__("sys").modules, "mcp", fake_mcp)
    monkeypatch.setitem(__import__("sys").modules, "mcp.client", SimpleNamespace())
    monkeypatch.setitem(__import__("sys").modules, "mcp.client.stdio", fake_stdio)
    monkeypatch.setitem(__import__("sys").modules, "mcp.client.sse", fake_sse)
    monkeypatch.setitem(__import__("sys").modules, "mcp.client.streamable_http", fake_http)

    mgr = MCPManager({})
    stack = contextlib.AsyncExitStack()
    monkeypatch.setattr(
        stack, "enter_async_context", AsyncMock(return_value=(MagicMock(), MagicMock()))
    )
    await mgr._open_transport(stack, server_def)

    assert calls["kind"] == expected
    if has_headers:
        assert calls["kwargs"].get("headers") == {"Authorization": "Bearer t"}


@pytest.mark.asyncio
async def test_call_timeout_evicts_server():
    mgr = MCPManager({})
    session = AsyncMock()

    async def slow(*a, **k):
        raise TimeoutError

    session.call_tool = slow
    mgr._sessions["cx"] = session
    mgr._routes["mcp__cx__q"] = ("cx", "q")
    mgr._converse_tools = [{"toolSpec": {"name": "mcp__cx__q"}}]
    mgr._stacks["cx"] = __import__("contextlib").AsyncExitStack()

    text, is_error = await mgr.call("mcp__cx__q", {})
    assert is_error and "timed out" in text
    # evicted: the route, session and advertised tool are gone → next call fast-fails
    assert not mgr.owns("mcp__cx__q")
    assert mgr.converse_tools == []
    text2, is_error2 = await mgr.call("mcp__cx__q", {})
    assert is_error2 and "unknown MCP tool" in text2


@pytest.mark.asyncio
async def test_call_generic_error_evicts_server():
    mgr = MCPManager({})
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("protocol error"))
    mgr._sessions["cx"] = session
    mgr._routes["mcp__cx__q"] = ("cx", "q")
    mgr._converse_tools = [{"toolSpec": {"name": "mcp__cx__q"}}]
    mgr._stacks["cx"] = __import__("contextlib").AsyncExitStack()

    text, is_error = await mgr.call("mcp__cx__q", {})
    assert is_error and "protocol error" in text
    assert not mgr.owns("mcp__cx__q")


def test_load_mcp_config_keeps_only_dict_entries(tmp_path):
    # A mixed map: one dict server, one non-dict (e.g. a stray string) → only the dict survives.
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {"good": {"url": "u"}, "bad": "not-a-dict"}}))
    assert load_mcp_config(str(p)) == {"good": {"url": "u"}}


@pytest.mark.asyncio
async def test_connect_dedupes_colliding_and_base_tool_names(monkeypatch):
    # Two upstream tools mapping to the same advertised name, plus one colliding with a base
    # tool, are skipped on insert (Converse rejects duplicate names → whole turn would fail).
    monkeypatch.setattr(mcp_mod, "mcp_sdk_available", lambda: True)
    monkeypatch.setattr(
        MCPManager, "_base_tool_names", staticmethod(lambda: {"mcp__cx__kubectl_get"})
    )

    fake_session = AsyncMock()
    fake_session.initialize = AsyncMock()
    fake_session.list_tools = AsyncMock(
        return_value=SimpleNamespace(
            tools=[
                SimpleNamespace(name="query", description="d", inputSchema={"type": "object"}),
                SimpleNamespace(name="query", description="dup", inputSchema={"type": "object"}),
                SimpleNamespace(
                    name="kubectl_get", description="clashes base", inputSchema={"type": "object"}
                ),
            ]
        )
    )

    class _FakeCS:
        def __init__(self, r, w):
            pass

        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *e):
            return None

    monkeypatch.setitem(__import__("sys").modules, "mcp", SimpleNamespace(ClientSession=_FakeCS))

    mgr = MCPManager({"cx": {"type": "http", "url": "https://x/mcp"}})

    async def fake_open_transport(stack, server_def):
        return (MagicMock(), MagicMock())

    monkeypatch.setattr(mgr, "_open_transport", fake_open_transport)
    await mgr.open()
    try:
        names = [t["toolSpec"]["name"] for t in mgr.converse_tools]
        # only the first "query" survives; the duplicate and the base-name clash are skipped
        assert names == ["mcp__cx__query"]
    finally:
        await mgr.aclose()


@pytest.mark.asyncio
async def test_open_connects_servers_concurrently(monkeypatch):
    # Concurrent connect: budget is max-not-sum. Two servers each "taking" a tick still both
    # connect, and neither blocks the other (asyncio.gather).
    import asyncio

    monkeypatch.setattr(mcp_mod, "mcp_sdk_available", lambda: True)
    order = []

    async def fake_connect(key, server_def):
        order.append(("start", key))
        await asyncio.sleep(0)
        order.append(("done", key))

    mgr = MCPManager({"a": {"url": "u"}, "b": {"url": "u"}})
    monkeypatch.setattr(mgr, "_connect_one", fake_connect)
    await mgr.open()
    # Both started before either finished → truly concurrent, not serial.
    assert order[0][0] == "start" and order[1][0] == "start"


def test_safe_error_masks_secret_carriers_and_caps_length():
    """Exception text reaching structured logs (rendered since ExtraAdder) must not leak
    credential material from upstream URLs/headers, and stays bounded."""
    masked = mcp_mod._safe_error(
        Exception("401 for https://mcp.example?apikey=sk-secret123 (Authorization: Bearer tok.abc)")
    )
    assert "sk-secret123" not in masked
    assert "tok.abc" not in masked
    assert "[redacted]" in masked
    assert len(mcp_mod._safe_error(Exception("x" * 1000))) == 300
