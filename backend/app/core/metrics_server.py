"""Prometheus metrics exporter on a dedicated port.

Runs a tiny WSGI server in a daemon thread, separate from the FastAPI app, so the scrape
endpoint lives on its own port and can be network-isolated from public traffic. Exposes the
default process / Python collectors of prometheus_client's global registry (process CPU and
memory, GC, platform/version info) at ``/metrics`` — the same registry any future
``Counter``/``Gauge`` defined elsewhere in the app registers into, so they appear here for
free. Gated by ``metrics_enabled`` (see ``app.core.config``); a no-op when off.

App request metrics are intentionally not added here: HTTP/DB/outbound spans are already
emitted as OpenTelemetry traces (see ``app.core.telemetry``).
"""

import threading
from wsgiref.simple_server import WSGIServer

from prometheus_client import start_http_server

from app.core.logging import get_logger

logger = get_logger(__name__)

_server: WSGIServer | None = None
_thread: threading.Thread | None = None


def start_metrics_server(port: int, addr: str = "0.0.0.0") -> int:
    """Start the exporter on ``addr:port``; return the actually-bound port.

    Idempotent: a second call while already running is a no-op (returns the live port),
    so a re-entrant lifespan can't double-bind. Pass ``port=0`` to bind an ephemeral port
    (used by tests). Raises ``OSError`` if the port is taken — the caller decides whether
    that is fatal.
    """
    global _server, _thread
    if _server is not None:
        return _server.server_port
    _server, _thread = start_http_server(port, addr=addr)
    logger.info("metrics_server_started", port=_server.server_port, addr=addr)
    return _server.server_port


def stop_metrics_server() -> None:
    """Stop the exporter and join its thread. No-op when not running."""
    global _server, _thread
    if _server is None:
        return
    _server.shutdown()  # unblocks serve_forever() in the daemon thread
    _server.server_close()
    if _thread is not None:
        _thread.join(timeout=5)
    _server, _thread = None, None
    logger.info("metrics_server_stopped")
