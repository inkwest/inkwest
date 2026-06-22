"""
Entrypoint: play exactly one turn.

Reads a turn-spec (JSON) from stdin, runs the Converse loop, writes the
``ChatEvent`` stream as NDJSON to stdout, and exits with the terminal status
(0 = clean ``done``, 1 = ``error`` / bad spec). The broker spawns this per turn
and cancels by killing the process (P1.3). Runnable standalone for dev:

    echo '{"session_id": "...", "user_message": "hi", "model": "...",
           "system_prompt": "...", "region": "eu-west-1"}' | python -m inkwest_agent
"""

import asyncio
import json
import sys

from app.services.events import error_event

from .converse import make_bedrock_client
from .logging import setup_logging
from .loop import run_turn
from .protocol import TurnSpecError, emit, parse_turn_spec


async def _run() -> int:
    raw = sys.stdin.read()
    try:
        spec = parse_turn_spec(json.loads(raw))
    except (json.JSONDecodeError, TurnSpecError) as e:
        emit(error_event(f"Invalid turn-spec: {e}"))
        return 1

    try:
        client = await asyncio.to_thread(make_bedrock_client, spec.region)
    except Exception as e:  # boto3/botocore client creation failures
        emit(error_event(f"AI service unavailable: {e}"))
        return 1

    status = 1  # no terminal event → treat as error (the loop always emits one)
    async for event in run_turn(spec, client):
        emit(event)
        if event.type == "done":
            status = 0
        elif event.type == "error":
            status = 1
    return status


def main() -> None:
    """Console-script / ``python -m inkwest_agent`` entrypoint."""
    # Logs go to stderr — stdout is the NDJSON ChatEvent channel the broker parses.
    setup_logging()
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
