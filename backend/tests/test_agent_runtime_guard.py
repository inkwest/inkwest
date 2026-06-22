"""Runtime guard for the ``inkwest_agent`` carve-out (P1).

The import guard (test_agent_import_guard) proves no DB/web stack is pulled *at import*.
This proves it stays clean *through a real turn*: a lazy ``import`` buried in a tool path
would slip past the import guard and only fire at runtime — the classic carve failure.

Runs the loop end-to-end in a fresh subprocess with a fake Converse client that drives one
tool round (kubectl) then ends the turn, and asserts that (a) the expected ChatEvent stream
came out and (b) sqlalchemy/fastapi/asyncpg never got imported along the way. Subprocess-
isolated for the same reason as the import guard: pytest already has those deps loaded.
"""

import json
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Drives a full run_turn with a fake bedrock client: turn 1 → a kubectl tool_use, turn 2 →
# end_turn. A kubeconfig_path is set so dispatch does NOT short-circuit on "no cluster" —
# execute_kubectl's body actually runs (firing any in-function/lazy imports it adds), then
# fails fast on the missing binary/config. That's the point: a lazy `import` buried in the
# executor would slip past the import guard and only fire here. We assert on the import
# surface, not kubectl's output. After the run, report event types + any forbidden package.
_DRIVER = """
import asyncio, json, sys, tempfile
from inkwest_agent.protocol import parse_turn_spec
from inkwest_agent.loop import run_turn

class FakeClient:
    def __init__(self):
        self.calls = 0
    def converse(self, **kw):
        self.calls += 1
        if self.calls == 1:
            return {"usage": {"inputTokens": 8, "outputTokens": 4},
                    "output": {"message": {"content": [
                        {"toolUse": {"toolUseId": "tu-1", "name": "kubectl_get",
                                     "input": {"resource": "pods"}}}]}},
                    "stopReason": "tool_use"}
        return {"usage": {"inputTokens": 10, "outputTokens": 5},
                "output": {"message": {"content": [{"text": "all clear"}]}},
                "stopReason": "end_turn"}

# A path that doesn't exist drives execute_kubectl past dispatch's "no cluster configured"
# guard; kubectl then fails fast (missing binary or unreadable config) without any network.
kubeconfig = tempfile.gettempdir() + "/inkwest-guard-nonexistent-kubeconfig"

spec = parse_turn_spec({
    "session_id": "11111111-1111-1111-1111-111111111111",
    "user_message": "why is my pod crashing?",
    "model": "meta.llama3-1-70b-instruct-v1:0",
    "system_prompt": "You are an SRE assistant.",
    "region": "eu-west-1",
    "kubeconfig_path": kubeconfig,
})

async def main():
    return [ev async for ev in run_turn(spec, FakeClient())]

events = asyncio.run(main())
forbidden = ("sqlalchemy", "fastapi", "asyncpg")
tops = {n.split(".")[0] for n in sys.modules}
print(json.dumps({
    "events": [e.type for e in events],
    "leaked": sorted(m for m in forbidden if m in tops),
}))
"""


def _run_turn_in_subprocess() -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _DRIVER],
        cwd=_BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, (
        f"agent turn driver failed (rc={proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    lines = proc.stdout.strip().splitlines()
    assert lines, f"driver printed nothing:\n--- stderr ---\n{proc.stderr}"
    return json.loads(lines[-1])


def test_full_turn_imports_no_db_or_web_stack() -> None:
    """A real tool-using turn must not import sqlalchemy/fastapi/asyncpg at runtime."""
    result = _run_turn_in_subprocess()
    assert result["leaked"] == [], (
        "a runtime code path (not import-time) pulled forbidden backend deps: "
        f"{', '.join(result['leaked'])}. Look for a lazy `import` in the tool path."
    )


def test_full_turn_emits_expected_event_stream() -> None:
    """Sanity that the carved loop still runs a tool round and terminates with `done`."""
    events = _run_turn_in_subprocess()["events"]
    # One tool round: a tool_start + tool_result, then a terminal done from the 2nd turn.
    assert "tool_start" in events, events
    assert "tool_result" in events, events
    assert events[-1] == "done", events
