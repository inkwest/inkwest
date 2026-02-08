#!/bin/bash
# Inkwest API entrypoint
#
# This script handles:
# - Environment validation
# - OpenTelemetry configuration
# - Graceful shutdown
#
# Environment variables:
#   DATABASE_URL: PostgreSQL connection string (required)
#   OTEL_EXPORTER_OTLP_ENDPOINT: OTLP collector endpoint (optional)
#   OTEL_ENABLED: Set to "false" to disable OTEL (default: auto)

set -e

# Trap SIGTERM for graceful shutdown
trap 'echo "Received SIGTERM, shutting down..."; kill -TERM $PID 2>/dev/null' SIGTERM

# Validate required environment
if [ -z "$DATABASE_URL" ]; then
    echo "ERROR: DATABASE_URL environment variable is required"
    exit 1
fi

# Configure OTEL based on environment
if [ -n "$OTEL_EXPORTER_OTLP_ENDPOINT" ]; then
    echo "OpenTelemetry enabled with endpoint: $OTEL_EXPORTER_OTLP_ENDPOINT"

    # Set default exporter protocol if not specified
    export OTEL_EXPORTER_OTLP_PROTOCOL="${OTEL_EXPORTER_OTLP_PROTOCOL:-grpc}"

    # Configure exporters
    export OTEL_TRACES_EXPORTER="${OTEL_TRACES_EXPORTER:-otlp}"
    export OTEL_METRICS_EXPORTER="${OTEL_METRICS_EXPORTER:-otlp}"
    export OTEL_LOGS_EXPORTER="${OTEL_LOGS_EXPORTER:-none}"
else
    echo "OpenTelemetry disabled (no OTEL_EXPORTER_OTLP_ENDPOINT set)"
    export OTEL_TRACES_EXPORTER=none
    export OTEL_METRICS_EXPORTER=none
    export OTEL_LOGS_EXPORTER=none
fi

# Execute the command
echo "Starting Inkwest API..."
exec "$@" &
PID=$!

# Wait for the process
wait $PID
EXIT_CODE=$?

echo "Inkwest API exited with code $EXIT_CODE"
exit $EXIT_CODE
