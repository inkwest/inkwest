"""
inkwest_agent — the custom agentic engine as a standalone worker.

One agentic loop (Converse-only) packaged to run as a process the broker spawns
per turn in the per-session sandbox: turn-spec on stdin → ``ChatEvent`` NDJSON on
stdout → exit code is the terminal status. DB-free; the broker (portal) owns
persistence, identity, policy, and the capability envelope.

Public surface:
- ``TurnSpec`` / ``parse_turn_spec`` / ``emit`` — the wire protocol (protocol.py)
- ``run_turn`` — the DB-free loop (loop.py)
- ``make_bedrock_client`` — Converse client builder (converse.py)

See docs/SANDBOXED_AGENT_RUNTIME.md. Packaged into the toolbox image in P1.2;
the broker drives it in P1.3; the turn-spec contract is extended in P1.4.
"""

from .loop import run_turn
from .protocol import PROTOCOL_VERSION, TurnSpec, TurnSpecError, emit, parse_turn_spec

__all__ = [
    "PROTOCOL_VERSION",
    "TurnSpec",
    "TurnSpecError",
    "emit",
    "parse_turn_spec",
    "run_turn",
]
