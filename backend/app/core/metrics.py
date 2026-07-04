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

import contextvars

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

# Resolved engine for the turn currently executing in THIS task. The per-turn dispatcher
# (ai_client.get_orchestrator) sets it once the engine is known; deep metric sites
# (token/cost in budget_service, tools in tool_execution, MCP in mcp_client, turn
# duration/errors in turn_runner) read it to label "by engine" without threading the value
# through every call. Each turn runs in its own asyncio task (see turn_runner), which gets
# an isolated copy of the context — so a turn's engine never leaks across turns.
_current_engine: contextvars.ContextVar[str] = contextvars.ContextVar(
    "inkwest_current_engine", default="unknown"
)


_current_model: contextvars.ContextVar[str] = contextvars.ContextVar(
    "inkwest_current_model", default="unknown"
)


def set_turn_engine(engine: str) -> None:
    """Record the engine for the current turn's task (called by the dispatcher)."""
    _current_engine.set(engine)


def turn_engine() -> str:
    """The current turn's engine, or 'unknown' outside a turn."""
    return _current_engine.get()


def set_turn_model(model: str) -> None:
    """Record the resolved model for the current turn's task (called by the dispatcher)."""
    _current_model.set(model)


def turn_model() -> str:
    """The current turn's resolved model, or 'unknown' outside a turn."""
    return _current_model.get()


# --- WebSocket / sessions / auth -------------------------------------------------------
# No engine/model labels here: these happen before (or independently of) a turn, so no
# model is resolved yet — labeling them would be meaningless.
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
# Labeled by the resolved engine + model — recorded in the per-turn dispatcher (ai_client)
# where both are known — so turn volume is visible per engine/model.
chat_turns_total = Counter("inkwest_chat_turns_total", "Chat turns started", ["engine", "model"])
# Unlabeled by engine/model on purpose: the gauge is inc'd at turn start — before the
# dispatcher resolves the engine — and dec'd at end, so a label would inc/dec under
# different values and drift. Saturation only.
chat_turns_active = Gauge(
    "inkwest_chat_turns_active", "Chat turns currently being orchestrated (in-flight)"
)
turn_duration_seconds = Histogram(
    "inkwest_turn_duration_seconds",
    "Wall-clock duration of a chat turn",
    ["engine", "model"],
    # A multi-tool LLM turn runs seconds to minutes — the prometheus default top bucket (10s)
    # would dump most turns into +Inf and make p95/p99 meaningless. Span up to 10 min.
    buckets=(1, 2.5, 5, 10, 30, 60, 120, 300, 600),
)
turn_errors_total = Counter(
    "inkwest_turn_errors_total",
    "Chat turns that ended in an unhandled error",
    ["engine", "model"],
)
# Fibonacci-ish buckets: most turns finish in a few model round-trips; the tail matters.
orchestration_iterations = Histogram(
    "inkwest_orchestration_iterations",
    "Model loop iterations per turn (API orchestrators)",
    ["engine", "model"],
    buckets=(1, 2, 3, 5, 8, 13, 21, 34),
)

# --- Tools -----------------------------------------------------------------------------
tool_calls_total = Counter(
    "inkwest_tool_calls_total", "Tool executions", ["tool", "is_error", "engine", "model"]
)
tool_duration_seconds = Histogram(
    "inkwest_tool_duration_seconds",
    # API-executed tools only (kubectl/connector via execute_tool_calls). Agent/CLI tools
    # arrive as streamed events with no reliable timing, so they're counted but not timed.
    # `tool` is clamped to the known set (see tool_execution) to bound histogram cardinality.
    "Tool execution duration (API-executed tools)",
    ["tool", "engine", "model"],
    # kubectl/connector calls run sub-second to tens of seconds.
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# --- Tokens & cost (per resolved model + engine) ---------------------------------------
# direction: input (fresh) | output | cache_read | cache_write — prompt-cache tokens are
# reported separately on the wire and never included in "input".
tokens_total = Counter(
    "inkwest_tokens_total", "Model tokens processed", ["model", "direction", "engine"]
)
model_cost_usd_total = Counter(
    "inkwest_model_cost_usd_total", "Estimated model cost in USD", ["model", "engine"]
)
# Broker metering failures = billed Bedrock calls that produced NO usage row (broker-on
# also skips the engine row, so each increment is real unaccounted spend). Alert on a
# sustained rate: a wire-format drift would flip EVERY call to "no_usage" silently.
# reason: no_usage (parse failure / no terminal frame) | no_session | persist_failed
llm_metering_failures_total = Counter(
    "inkwest_llm_metering_failures_total",
    "Broker metering failures (no usage row persisted)",
    ["reason"],
)

# --- Sandbox (only moves when sandbox_enabled) -----------------------------------------
# No engine/model labels: a sandbox is provisioned once per conversation and reused across
# many turns and models, so attributing it to a single engine/model would be misleading.
sandbox_active = Gauge(
    "inkwest_sandbox_active", "Provisioned conversation sandboxes currently running"
)
sandbox_provision_total = Counter(
    "inkwest_sandbox_provision_total", "Sandbox provision attempts", ["result"]
)
sandbox_evicted_user_cap_total = Counter(
    "inkwest_sandbox_evicted_user_cap_total",
    "Idle sandboxes soft-reaped to keep a user under the per-user concurrency cap",
)
sandbox_cap_refused_total = Counter(
    "inkwest_sandbox_cap_refused_total",
    "Turns refused because the user hit the hard sandbox ceiling with all pairs busy",
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
    ["server", "tool", "is_error", "engine", "model"],
)

# --- Auth ------------------------------------------------------------------------------
auth_logins_total = Counter("inkwest_auth_logins_total", "Login attempts", ["result"])


def record_tool_call(tool: str, is_error: bool) -> None:
    """Count one tool call, labeled with the current turn's engine+model (from context).

    Routes MCP-brokered tools — the model sees them as ``mcp__<server>__<tool>`` — to
    ``mcp_tool_calls_total`` (parsing the server) and everything else to
    ``tool_calls_total``, so MCP usage isn't conflated with native tools. Used by every
    count site (API executor, agent relay, CLI relay) so the split is defined once.
    """
    engine, model = turn_engine(), turn_model()
    if tool.startswith("mcp__"):
        # mcp__<server>__<tool>; keep both the server and the specific tool. Fall back to
        # 'unknown' if the shape is unexpected (tool names may themselves contain '__').
        parts = tool.split("__")
        server = parts[1] if len(parts) >= 3 and parts[1] else "unknown"
        mcp_tool = "__".join(parts[2:]) if len(parts) >= 3 else "unknown"
        mcp_tool_calls_total.labels(
            server=server, tool=mcp_tool, is_error=str(is_error), engine=engine, model=model
        ).inc()
    else:
        tool_calls_total.labels(tool=tool, is_error=str(is_error), engine=engine, model=model).inc()
