"""
Structured logging configuration for Inkwest.

Uses fastapi-structured-logging for:
- JSON format (production)
- Pretty console format (development)
- Access log middleware for FastAPI
- OpenTelemetry trace context integration (automatic)

Environment variables:
- LOG_LEVEL: DEBUG, INFO, WARNING, ERROR (default: INFO)
- LOG_FORMAT: Set to "json" to force JSON output (default: auto-detect TTY)
"""

import logging
import os
from typing import Any

import fastapi_structured_logging
from fastapi_structured_logging import AccessLogConfig


def setup_logging() -> None:
    """
    Configure structured logging based on environment.

    Uses fastapi-structured-logging which:
    - Automatically detects TTY for console/JSON format
    - Integrates OpenTelemetry trace context into logs
    - Provides structlog configuration
    """
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = os.getenv("LOG_FORMAT", "auto").lower()

    # Force JSON if explicitly requested
    json_logs = True if log_format == "json" else None  # None = auto-detect

    fastapi_structured_logging.setup_logging(
        log_level=log_level,
        json_logs=json_logs,
    )

    # Suppress noisy loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # SQLAlchemy: route through structured logging pipeline
    sa_log_level = os.getenv("SQLALCHEMY_LOG_LEVEL", "WARNING").upper()
    logging.getLogger("sqlalchemy.engine").setLevel(getattr(logging, sa_log_level, logging.WARNING))


def get_logger(name: str | None = None) -> Any:
    """Get a configured logger instance."""
    return fastapi_structured_logging.get_logger(name)


# Access log middleware configuration
access_log_config = AccessLogConfig(
    include_method=True,
    include_path=True,
    include_query_params=True,
    include_client_ip=True,
    include_user_agent=True,
    include_status_code=True,
    include_process_time=True,
    exclude_paths=["/health", "/health/ready", "/metrics"],
)

# Re-export AccessLogMiddleware with our config
AccessLogMiddleware = fastapi_structured_logging.AccessLogMiddleware
