"""Tests for the dedicated-port Prometheus exporter."""

import urllib.request

import pytest

from app.core import metrics_server


@pytest.fixture
def running_exporter():
    """Start the exporter on an ephemeral port; always stop it afterwards."""
    port = metrics_server.start_metrics_server(0)
    try:
        yield port
    finally:
        metrics_server.stop_metrics_server()


def test_serves_default_collectors(running_exporter):
    """/metrics returns 200 with the default process/Python collectors exposed."""
    with urllib.request.urlopen(f"http://127.0.0.1:{running_exporter}/metrics", timeout=5) as r:
        assert r.status == 200
        body = r.read().decode()
    # python_info is always registered by prometheus_client; a process_* metric confirms
    # the ProcessCollector is wired (Linux). Either proves the global registry is served.
    assert "python_info" in body


def test_app_instruments_are_served(running_exporter):
    """App metrics register into the same global registry the exporter serves, so a
    recorded value shows up in the scrape under its inkwest_ name."""
    from app.core import metrics

    metrics.chat_turns_total.labels(engine="claude-cli", model="claude-test").inc()
    metrics.tool_calls_total.labels(
        tool="kubectl_get", is_error="False", engine="claude-cli", model="claude-test"
    ).inc()
    metrics.tokens_total.labels(model="claude-test", direction="input", engine="claude-cli").inc(5)
    with urllib.request.urlopen(f"http://127.0.0.1:{running_exporter}/metrics", timeout=5) as r:
        body = r.read().decode()
    assert 'inkwest_chat_turns_total{engine="claude-cli",model="claude-test"}' in body
    assert (
        'inkwest_tool_calls_total{engine="claude-cli",is_error="False",'
        'model="claude-test",tool="kubectl_get"}' in body
    )
    assert 'inkwest_tokens_total{direction="input",engine="claude-cli",model="claude-test"}' in body


def test_start_is_idempotent():
    """A second start while running returns the same port and doesn't double-bind."""
    port = metrics_server.start_metrics_server(0)
    try:
        assert metrics_server.start_metrics_server(0) == port
    finally:
        metrics_server.stop_metrics_server()


def test_stop_when_not_running_is_noop():
    """Stopping an already-stopped exporter doesn't raise."""
    metrics_server.stop_metrics_server()
    metrics_server.stop_metrics_server()


def test_stop_frees_the_port(running_exporter):
    """After stop, the same port can be bound again (clean shutdown, no lingering socket)."""
    metrics_server.stop_metrics_server()
    reused = metrics_server.start_metrics_server(running_exporter)
    try:
        assert reused == running_exporter
    finally:
        metrics_server.stop_metrics_server()


def test_record_tool_call_routes_mcp_separately():
    """record_tool_call sends mcp__<server>__<tool> to the MCP metric (parsing the server)
    and native tools to the generic metric — MCP must not pollute tool_calls_total."""
    from prometheus_client import REGISTRY

    from app.core import metrics

    metrics.record_tool_call("mcp__coralogix-dev__get_datetime", is_error=False)
    metrics.record_tool_call("kubectl_get", is_error=False)

    common = {"engine": "unknown", "model": "unknown", "is_error": "False"}
    # MCP routed to the MCP metric, server AND tool parsed from the name.
    assert (
        REGISTRY.get_sample_value(
            "inkwest_mcp_tool_calls_total",
            {"server": "coralogix-dev", "tool": "get_datetime", **common},
        )
        or 0
    ) >= 1
    # Native tool routed to the generic metric.
    assert (
        REGISTRY.get_sample_value("inkwest_tool_calls_total", {"tool": "kubectl_get", **common})
        or 0
    ) >= 1
    # The MCP name must NOT appear as a generic tool.
    assert (
        REGISTRY.get_sample_value(
            "inkwest_tool_calls_total",
            {"tool": "mcp__coralogix-dev__get_datetime", **common},
        )
        is None
    )


def test_landing_page_and_404(running_exporter):
    """/ serves the HTML landing page (version + link); unknown paths 404."""
    import urllib.error

    base = f"http://127.0.0.1:{running_exporter}"
    with urllib.request.urlopen(f"{base}/", timeout=5) as r:
        assert r.status == 200
        body = r.read().decode()
    assert "Inkwest backend" in body
    assert "/metrics" in body
    try:
        urllib.request.urlopen(f"{base}/nope", timeout=5)
        raise AssertionError("expected 404 for unknown path")
    except urllib.error.HTTPError as e:
        assert e.code == 404
