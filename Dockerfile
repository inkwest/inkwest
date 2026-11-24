# Combined production image: backend (FastAPI) serving the built frontend SPA.
# Built by CI (.github/workflows/ci.yml) from the repo root; dev keeps backend/Dockerfile + frontend/Dockerfile
# via docker compose (hot reload). Last stage = default build target.
# KEEP IN SYNC with backend/Dockerfile for the tooling layers (kubectl, AWS CLI,
# docker CLI, Claude CLI) — same versions, same install commands.

# ---- Frontend build ----
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# Empty (not unset) -> same-origin API/WS URLs: this image serves the SPA itself
ENV VITE_API_URL="" \
    VITE_WS_URL=""
RUN npm run build

# ---- Backend + SPA ----
FROM python:3.11-slim AS production

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
# Redistribution of Claude Code is allowed only as published by Anthropic: never patch,
# repackage or strip its auth methods (README "Claude Code terms").
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root
ENV PATH="/home/app/.local/bin:${PATH}"

# Install Python dependencies from the lockfile (reproducible builds) before the
# app code lands, so the dependency layer survives code-only changes.
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements.txt -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY backend/ .
RUN uv pip install --system --no-cache --no-deps .
# Built SPA served by FastAPI (see SPAStaticFiles mount in app/main.py)
COPY --from=frontend-build /build/dist ./static
RUN chmod +x /app/entrypoint.sh /app/entrypoint-migrate.sh && \
    chown -R app:app /app /home/app

# Use opentelemetry-instrument for auto-instrumentation
# Set defaults (can be overridden at runtime)
ENV OTEL_SERVICE_NAME=inkwest \
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS= \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VERSION=${VERSION} \
    COMMIT_HASH=${COMMIT_HASH} \
    BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

USER app
# 8000 = API/SPA; 9090 = Prometheus metrics exporter (METRICS_PORT, on by default).
EXPOSE 8000 9090
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["opentelemetry-instrument", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
