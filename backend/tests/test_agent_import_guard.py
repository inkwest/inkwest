"""Import-guard for the standalone ``inkwest_agent`` carve-out (P0).

The sandbox image ships *claude + agent only*: importing the agent must NOT drag in
the backend's DB ORM, the web stack, or the Postgres driver. This is the structural
guard that drives the carve and locks it against regression.

It MUST run in a fresh subprocess: pytest itself (via conftest/app imports) already
loads sqlalchemy/fastapi/asyncpg into this process's ``sys.modules``, so an in-process
check would always see them and prove nothing. The subprocess imports the agent's
entrypoint module — the exact surface ``python -m inkwest_agent`` runs — and reports
which forbidden top-level packages ended up imported.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Top-level packages the lean agent must never import at module load. sqlalchemy +
# asyncpg = the DB engine (built at import of app.core.database); fastapi = the web
# stack (dragged in by fastapi_structured_logging via app.core.logging).
FORBIDDEN = ("sqlalchemy", "fastapi", "asyncpg")

# backend/ — parent of tests/. The subprocess runs here so `inkwest_agent`/`app`
# resolve the same way they do for the rest of the suite.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Import the entrypoint module (pulls the whole eager agent surface: loop, protocol,
# converse, skills) and dump every forbidden top-level package now in sys.modules.
_PROBE = f"""
import json, sys
import inkwest_agent.__main__  # noqa: F401  -- entrypoint = full eager surface
forbidden = {FORBIDDEN!r}
leaked = sorted(m for m in forbidden if m in {{n.split('.')[0] for n in sys.modules}})
print(json.dumps(leaked))
"""


def _leaked_forbidden() -> list[str]:
    """Run the probe in a clean interpreter; return the forbidden pkgs it imported."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"probe failed to import inkwest_agent (rc={proc.returncode}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    # A guard that silently returns [] on empty output would be a false green — the worst
    # failure mode here. Treat "rc=0 but nothing printed" as a hard error, not "no leaks".
    lines = proc.stdout.strip().splitlines()
    assert lines, (
        f"probe imported cleanly (rc=0) but printed nothing — cannot trust the result:\n"
        f"--- stderr ---\n{proc.stderr}"
    )
    return json.loads(lines[-1])


def test_agent_import_pulls_no_db_or_web_stack() -> None:
    """`import inkwest_agent` must not transitively import sqlalchemy/fastapi/asyncpg."""
    leaked = _leaked_forbidden()
    assert leaked == [], (
        "inkwest_agent eagerly imports forbidden backend deps: "
        f"{', '.join(leaked)}. The agent must ship without the DB ORM / web stack."
    )


@pytest.mark.parametrize("pkg", FORBIDDEN)
def test_individual_forbidden_dep_absent(pkg: str) -> None:
    """Per-dep view so a regression names the offending package directly."""
    assert pkg not in _leaked_forbidden(), f"inkwest_agent must not import {pkg!r}"
