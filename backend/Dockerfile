# Dev/compose image. KEEP IN SYNC with the root Dockerfile (combined prod image)
# for the tooling layers (kubectl, AWS CLI, docker CLI, Claude CLI).
FROM python:3.11-slim AS base

ARG BUILD_TIMESTAMP="1970-01-01T00:00:00+00:00"
ARG COMMIT_HASH="00000000-dirty"
ARG PROJECT_URL="https://github.com/kubepanion/kubepanion"
ARG VERSION="v0.0.0"

LABEL org.opencontainers.image.source=${PROJECT_URL}
LABEL org.opencontainers.image.created=${BUILD_TIMESTAMP}
LABEL org.opencontainers.image.version=${VERSION}
LABEL org.opencontainers.image.revision=${COMMIT_HASH}

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git unzip && \
    rm -rf /var/lib/apt/lists/* && \
    useradd -ms /bin/bash -d /home/app app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

# Install AWS CLI v2 (used by the AWS external-access connector's aws_call tool).
# $(uname -m) -> x86_64 on amd64, aarch64 on arm64; AWS's bundle names match exactly,
# so the same line builds correctly on both architectures.
RUN curl -s "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscli.zip

# Install the Docker CLI client only (no daemon). app.services.sandbox shells out to it
# to run the per-conversation sandbox containers; it targets the dind sidecar over
# TCP+TLS (DOCKER_HOST), so only the `docker` client is needed here — no engine.
ARG DOCKER_CLI_VERSION=27.3.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/$(uname -m)/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz

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
