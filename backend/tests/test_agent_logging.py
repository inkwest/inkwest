"""Tests for the standalone agent's structlog setup (edge 3).

The load-bearing invariant: log lines go to **stderr**, never stdout — stdout is the
NDJSON ChatEvent protocol the broker parses, and a stray log line would corrupt it.
"""

import json
import logging

import pytest
import structlog

from inkwest_agent.logging import setup_logging


@pytest.fixture
def _restore_root_logging():
    """setup_logging mutates the root logger — snapshot and restore around each test."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)


def test_logs_go_to_stderr_not_stdout(capsys, _restore_root_logging) -> None:
    """The critical invariant: nothing logged ever lands on stdout."""
    setup_logging(json_logs=True)
    # stdlib logger — what the app/agent modules actually use.
    logging.getLogger("inkwest_agent.test").warning("disk_pressure")
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout is reserved for the NDJSON ChatEvent stream
    assert "disk_pressure" in captured.err


def test_json_renderer_emits_parseable_json(capsys, _restore_root_logging) -> None:
    setup_logging(json_logs=True)
    # structlog logger — structured kwargs ride into the JSON event dict.
    structlog.get_logger("x").info("turn_started", iteration=1)
    line = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "turn_started"
    assert record["level"] == "info"
    assert record["iteration"] == 1


def test_console_renderer_is_not_json(capsys, _restore_root_logging) -> None:
    setup_logging(json_logs=False)
    structlog.get_logger("x").info("turn_started")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "turn_started" in line
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)


def test_defaults_to_json_when_stderr_not_a_tty(capsys, _restore_root_logging) -> None:
    """Sandbox stderr isn't a TTY → JSON by default (machine-parseable)."""
    setup_logging()  # json_logs=None → derived from stderr.isatty() (False under capture)
    structlog.get_logger("x").info("evt")
    line = capsys.readouterr().err.strip().splitlines()[-1]
    assert json.loads(line)["event"] == "evt"


def test_setup_is_idempotent(capsys, _restore_root_logging) -> None:
    """One-shot process may call it more than once — handlers don't accumulate."""
    setup_logging(json_logs=True)
    setup_logging(json_logs=True)
    assert len(logging.getLogger().handlers) == 1
    logging.getLogger("x").warning("once")
    # A single handler → exactly one rendered line, not duplicated.
    assert capsys.readouterr().err.strip().count('"event": "once"') == 1
