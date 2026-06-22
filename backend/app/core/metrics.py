"""Application Prometheus metrics — the instrument definitions.

One module owning every ``Counter``/``Gauge``/``Histogram`` so label cardinality is
auditable in one place (deliberately NO per-session / per-user labels — those are
unbounded). Instruments register into prometheus_client's global registry on import and
are served by the dedicated-port exporter (see :mod:`app.core.metrics_server`). Defining
them is side-effect-free, so call sites record unconditionally; only the exporter is gated
by ``metrics_enabled`` — a turn's accounting is correct whether or not anyone scrapes it.

App request/DB/outbound spans stay in OpenTelemetry traces (see
:mod:`app.core.telemetry`); these metrics cover what traces don't aggregate well:
saturation gauges, per-model token/cost rates, and tool/turn counters.
"""

try:
    from prometheus_client import Counter, Gauge, Histogram
except ModuleNotFoundError:
    # The standalone inkwest-agent shares importers of this module (tool_execution) but runs
    # on the lean `requirements-agent.txt` without prometheus_client — and is never scraped.
    # Degrade to no-op instruments so the shared code imports cleanly there, rather than
    # bloating the agent image. Mirrors the agent's optional-MCP "degrade if absent" stance.
    from typing import Any

    class _NoopMetric:
        def labels(self, *args: Any, **kwargs: Any) -> "_NoopMetric":
            return self

        def inc(self, *args: Any, **kwargs: Any) -> None: ...
        def dec(self, *args: Any, **kwargs: Any) -> None: ...
        def observe(self, *args: Any, **kwargs: Any) -> None: ...
        def set(self, *args: Any, **kwargs: Any) -> None: ...

    def _noop(*args: Any, **kwargs: Any) -> _NoopMetric:
        return _NoopMetric()

    Counter = Gauge = Histogram = _noop  # type: ignore[assignment,misc]

# --- WebSocket -------------------------------------------------------------------------
ws_connections_active = Gauge(
    "inkwest_ws_connections_active", "Currently connected chat WebSocket clients"
)
ws_connections_total = Counter(
    "inkwest_ws_connections_total", "Chat WebSocket connections opened (total)"
)

# --- Sessions & turns ------------------------------------------------------------------
chat_sessions_created_total = Counter(
    "inkwest_chat_sessions_created_total", "Chat sessions created"
)
# Unlabeled: the resolved engine/model isn't known at the turn-runner boundary (it's
# decided per-turn inside the orchestrator). Per-model breakdown comes from `tokens_total`.
chat_turns_total = Counter("inkwest_chat_turns_total", "Chat turns started")
chat_turns_active = Gauge(
    "inkwest_chat_turns_active", "Chat turns currently being orchestrated (in-flight)"
)
turn_duration_seconds = Histogram(
    "inkwest_turn_duration_seconds",
    "Wall-clock duration of a chat turn",
    # A multi-tool LLM turn runs seconds to minutes — the prometheus default top bucket (10s)
    # would dump most turns into +Inf and make p95/p99 meaningless. Span up to 10 min.
    buckets=(1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)
turn_errors_total = Counter(
    "inkwest_turn_errors_total", "Chat turns that ended in an unhandled error"
)
# Fibonacci-ish buckets: most turns finish in a few model round-trips; the tail matters.
orchestration_iterations = Histogram(
    "inkwest_orchestration_iterations",
    "Model loop iterations per turn (API orchestrators)",
    buckets=(1, 2, 3, 5, 8, 13, 21, 34),
)

# --- Tools -----------------------------------------------------------------------------
tool_calls_total = Counter("inkwest_tool_calls_total", "Tool executions", ["tool", "is_error"])
tool_duration_seconds = Histogram(
    "inkwest_tool_duration_seconds",
    "Tool execution duration",
    ["tool"],
    # kubectl/connector calls run sub-second to tens of seconds.
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# --- Tokens & cost (per resolved model) ------------------------------------------------
tokens_total = Counter("inkwest_tokens_total", "Model tokens processed", ["model", "direction"])
model_cost_usd_total = Counter(
    "inkwest_model_cost_usd_total", "Estimated model cost in USD", ["model"]
)

# --- Sandbox (only moves when sandbox_enabled) -----------------------------------------
sandbox_active = Gauge(
    "inkwest_sandbox_active", "Provisioned conversation sandboxes currently running"
)
sandbox_provision_total = Counter(
    "inkwest_sandbox_provision_total", "Sandbox provision attempts", ["result"]
)
sandbox_provision_duration_seconds = Histogram(
    "inkwest_sandbox_provision_duration_seconds",
    "Time to provision a conversation sandbox",
    # Provision waits on container+proxy startup, bounded by sandbox_provision_timeout (60s).
    buckets=(1, 2.5, 5, 10, 20, 30, 45, 60, 90),
)

# --- MCP -------------------------------------------------------------------------------
mcp_tool_calls_total = Counter(
    "inkwest_mcp_tool_calls_total",
    "MCP tool calls brokered for a conversation",
    ["server", "is_error"],
)

# --- Auth ------------------------------------------------------------------------------
auth_logins_total = Counter("inkwest_auth_logins_total", "Login attempts", ["result"])
