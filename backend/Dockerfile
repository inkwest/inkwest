FROM python:3.11-slim AS base

ARG BUILD_TIMESTAMP="1970-01-01T00:00:00+00:00"
ARG COMMIT_HASH="00000000-dirty"
ARG PROJECT_URL="https://github.com/inkwest/inkwest"
ARG VERSION="v0.0.0"

LABEL org.opencontainers.image.source=${PROJECT_URL}
LABEL org.opencontainers.image.created=${BUILD_TIMESTAMP}
LABEL org.opencontainers.image.version=${VERSION}
LABEL org.opencontainers.image.revision=${COMMIT_HASH}

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -ms /bin/bash -d /home/app app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

# Install Claude CLI as app user so it lands in /home/app/.local/bin
# Note: Claude CLI runs with --dangerously-skip-permissions for non-interactive use.
# This is safe in container context as the container provides isolation.
USER app
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root
ENV PATH="/home/app/.local/bin:${PATH}"

# Install Python dependencies (better caching with UV)
COPY pyproject.toml README.md ./
COPY app/ ./app/
RUN uv pip install --system --no-cache .

# Development stage
FROM base AS development
RUN uv pip install --system --no-cache ".[dev]"
COPY . .
RUN chown -R app:app /app /home/app
USER app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# Production stage
FROM base AS production
COPY . .
RUN chmod +x /app/entrypoint.sh /app/entrypoint-migrate.sh && \
    chown -R app:app /app /home/app

# Use opentelemetry-instrument for auto-instrumentation
# Set defaults (can be overridden at runtime)
ENV OTEL_SERVICE_NAME=inkwest-api \
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS= \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VERSION=${VERSION} \
    COMMIT_HASH=${COMMIT_HASH} \
    BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

USER app
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
