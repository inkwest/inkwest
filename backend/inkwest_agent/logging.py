"""
Structured logging for the standalone agent (edge 3).

The backend logs through ``fastapi_structured_logging`` (drags in fastapi/starlette);
the carved agent must not, so it configures ``structlog`` directly — CLI-style: JSON
when stderr isn't a TTY (the sandbox), a human console renderer when it is (dev).

CRITICAL: every log line goes to **stderr**. ``stdout`` is the agent's wire protocol
(NDJSON ``ChatEvent``s the broker parses, written by ``protocol.emit``); a stray log
line on stdout would corrupt that stream. The handler is pinned to stderr for exactly
this reason. The app/agent modules log via stdlib ``logging.getLogger``, so stdlib
records are routed through structlog's formatter too (not just native structlog calls).
"""

import logging
import sys

import structlog


def setup_logging(*, json_logs: bool | None = None, level: str = "INFO") -> None:
    """Configure structlog + stdlib logging to emit structured records on stderr.

    ``json_logs`` defaults to "JSON unless stderr is a TTY" (sandbox → JSON, dev → console).
    Idempotent enough for a one-shot process: it replaces the root handler each call.
    """
    if json_logs is None:
        json_logs = not sys.stderr.isatty()

    # Shared processors run for BOTH structlog-native and stdlib-originated records, so a
    # `logging.getLogger(__name__).info(...)` in app.* renders the same as a structlog call.
    shared: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    # stderr ONLY — stdout carries the NDJSON ChatEvent protocol.
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
