"""Prometheus metrics exporter on a dedicated port.

Serves a small landing page at ``/`` (build/version info + a link to the metrics) and the
prometheus_client registry at ``/metrics``, in a daemon thread separate from the FastAPI
app so the scrape endpoint lives on its own port and can be network-isolated from public
traffic. The registry carries the default process/Python collectors plus the inkwest_* app
metrics. Gated by ``metrics_enabled`` (see ``app.core.config``); a no-op when off.

App request metrics are intentionally not added here: HTTP/DB/outbound spans are already
emitted as OpenTelemetry traces (see ``app.core.telemetry``).
"""

import html
import threading
from collections.abc import Callable, Iterable
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import make_wsgi_app

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_server: WSGIServer | None = None
_thread: threading.Thread | None = None

# Bound to the global registry: serves whatever counters/gauges are registered at scrape time.
_metrics_app = make_wsgi_app()


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Serve a scrape and the landing page concurrently; worker threads die with the process."""

    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # Silence the per-request stderr line — scrapes happen on an interval and would spam.
        pass


def _index_page() -> bytes:
    s = get_settings()
    rows = "".join(
        f"<tr><td style='padding-right:1rem;color:#666'>{k}</td>"
        f"<td><code>{html.escape(v)}</code></td></tr>"
        for k, v in (
            ("version", s.version),
            ("commit", s.commit_hash),
            ("build", s.build_timestamp),
        )
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Inkwest metrics</title>"
        "<style>body{font:14px system-ui,sans-serif;margin:3rem;color:#222}"
        "code{background:#f4f4f4;padding:.1rem .35rem;border-radius:3px}"
        "a{color:#2563eb}</style></head><body>"
        "<h1>Inkwest backend</h1>"
        f"<table>{rows}</table>"
        "<p><a href='/metrics'>/metrics</a> — Prometheus exposition</p>"
        "</body></html>"
    ).encode()


def _app(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    """WSGI router: landing page at /, metrics at /metrics, 404 otherwise."""
    path = environ.get("PATH_INFO", "/")
    if path == "/metrics":
        return _metrics_app(environ, start_response)
    if path == "/":
        body = _index_page()
        start_response(
            "200 OK",
            [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))],
        )
        return [body]
    body = b"Not Found\n"
    start_response(
        "404 Not Found",
        [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))],
    )
    return [body]


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
    _server = make_server(addr, port, _app, _ThreadingWSGIServer, _QuietHandler)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
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
