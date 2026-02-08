"""
OpenTelemetry helpers for Inkwest.

Auto-instrumentation is handled via the `opentelemetry-instrument` CLI wrapper
in the Dockerfile/entrypoint. This module provides:

1. Tracer acquisition for manual span creation
2. Shutdown helper for graceful cleanup

Run the application with auto-instrumentation:
    opentelemetry-instrument uvicorn app.main:app --host 0.0.0.0 --port 8000

Environment variables (standard OTEL, configured in entrypoint.sh):
- OTEL_SERVICE_NAME: Service name (default: inkwest-api)
- OTEL_EXPORTER_OTLP_ENDPOINT: OTLP collector endpoint
- OTEL_EXPORTER_OTLP_PROTOCOL: Protocol (grpc or http/protobuf)
- OTEL_TRACES_EXPORTER: Exporter type (otlp, console, none)
"""

from contextlib import nullcontext
from typing import Any


def get_tracer(name: str) -> Any:
    """
    Get a tracer instance for manual span creation.

    Args:
        name: Name for the tracer (usually __name__)

    Returns:
        OpenTelemetry Tracer instance (or NoOpTracer if not available)
    """
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except ImportError:
        return _NoOpTracer()


def shutdown_telemetry() -> None:
    """Shutdown OpenTelemetry provider and flush pending spans."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        pass  # Ignore errors during shutdown


class _NoOpTracer:
    """No-op tracer for when OTEL is not installed."""

    def start_span(self, *args: Any, **kwargs: Any) -> "_NoOpSpan":
        return _NoOpSpan()

    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Any:
        return nullcontext(_NoOpSpan())


class _NoOpSpan:
    """No-op span for when OTEL is not installed."""

    def set_attribute(self, *args: Any, **kwargs: Any) -> None:
        pass

    def add_event(self, *args: Any, **kwargs: Any) -> None:
        pass

    def end(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *args: Any) -> None:
        pass
