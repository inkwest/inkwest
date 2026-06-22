"""Lean-dependency sufficiency guard for the sandbox agent (M2).

`requirements-agent.txt` is hand-maintained and decoupled from pyproject, so a new
runtime import in the agent could be added to the code but forgotten in the lean list
— and the image would only break at runtime (the import guard catches *forbidden*
deps, not *missing-needed* ones). This closes that gap: build a venv with ONLY
requirements-agent.txt installed, put the COPYed code on PYTHONPATH (mirroring how
Dockerfile.agent runs it — package not pip-installed), and drive a full tool-using
turn. A dep the agent imports but the lean list omits surfaces here as an ImportError,
not in production.

Marked e2e and skipped when uv is unavailable or the install can't run (offline) — it
builds a real venv and needs network, so it doesn't belong in the fast unit path's
critical assertions, but it runs by default wherever the toolchain + network exist.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_REQS = _BACKEND_ROOT / "requirements-agent.txt"

# Same full-turn driver as the runtime guard: one kubectl tool round then end_turn,
# exercising the dispatch→executor path. Runs under the lean venv with the code on
# PYTHONPATH. Prints the event types so the test can assert a full turn completed.
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
            return {"usage": {"inputTokens": 1, "outputTokens": 1},
                    "output": {"message": {"content": [
                        {"toolUse": {"toolUseId": "t", "name": "kubectl_get",
                                     "input": {"resource": "pods"}}}]}},
                    "stopReason": "tool_use"}
        return {"usage": {"inputTokens": 1, "outputTokens": 1},
                "output": {"message": {"content": [{"text": "ok"}]}},
                "stopReason": "end_turn"}

spec = parse_turn_spec({
    "session_id": "11111111-1111-1111-1111-111111111111",
    "user_message": "hi", "model": "m", "system_prompt": "x", "region": "eu-west-1",
    "kubeconfig_path": tempfile.gettempdir() + "/none",
})

async def main():
    return [e async for e in run_turn(spec, FakeClient())]

print(json.dumps({"events": [e.type for e in asyncio.run(main())]}))
"""


@pytest.fixture(scope="module")
def lean_python() -> str:
    """A venv with ONLY requirements-agent.txt installed; returns its python path."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available — cannot build the lean dep venv")
    import tempfile

    venv = Path(tempfile.mkdtemp(prefix="inkwest-lean-")) / "venv"
    created = subprocess.run([uv, "venv", str(venv)], capture_output=True, text=True)
    if created.returncode != 0:
        pytest.skip(f"uv venv failed: {created.stderr}")
    python = str(venv / "bin" / "python")
    installed = subprocess.run(
        [uv, "pip", "install", "--python", python, "--no-cache", "-r", str(_REQS)],
        capture_output=True,
        text=True,
    )
    if installed.returncode != 0:
        # Most likely offline / index unreachable — an infra gap, not a code failure.
        pytest.skip(f"installing lean deps failed (offline?):\n{installed.stderr}")
    return python


def test_requirements_agent_is_sufficient_for_a_full_turn(lean_python: str) -> None:
    """With only the lean deps installed, the agent must run a full tool turn — no
    ImportError for a dep that's used in code but missing from requirements-agent.txt."""
    proc = subprocess.run(
        [lean_python, "-c", _DRIVER],
        cwd=_BACKEND_ROOT,  # code on path (package not installed, like the image)
        env={**os.environ, "PYTHONPATH": str(_BACKEND_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "agent failed under the lean dep set — likely a dep used in code but missing from "
        f"requirements-agent.txt:\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    events = json.loads(proc.stdout.strip().splitlines()[-1])["events"]
    assert "tool_result" in events and events[-1] == "done", events
