# Self-contained sandbox image for the standalone `inkwest_agent` worker.
#
# Folds the "toolbox" (debian + net/k8s/aws debug tooling + the Claude
# Code CLI) and the LEAN inkwest_agent bake into ONE build owned by this repo — no
# `FROM <external toolbox>`, no cross-repo git+token pull of the backend package, no
# pinned-branch rot. The build context is `backend/`, so the agent code is COPYed
# locally (the whole reason this is self-contained): the package is not pip-installed —
# only the carved runtime deps (requirements-agent.txt) go into a venv, and the agent
# runs via a `python -m inkwest_agent` launcher. So the image ships no DB ORM / web
# stack and no inkwest-backend dep metadata (`uv pip check` stays clean).
#
# The toolbox half mirrors the separate toolbox image Dockerfile (kept independent on purpose — this
# repo owns its sandbox image). The broker spawns the agent per turn:
#   inkwest-agent < turn-spec.json     # turn-spec stdin -> NDJSON ChatEvent stdout -> exit=status
#
# Lives at the CI image-build variant path (<variant>/Dockerfile at repo root) so CI's
# `variants: ["agent"]` instance pushes a distinct sub-path image .../inkwest/agent (not a
# tag-suffixed sibling of the combined image). Context is backend/ (COPY paths are relative
# to it); the Dockerfile sits outside that context, which kaniko/docker `-f` allow.
# Build locally:
#   docker build -f agent/Dockerfile -t inkwest-agent backend/

FROM debian:trixie-slim

# OCI provenance is injected by the CI image-build step as direct
# --label flags (org.opencontainers.image.{source,created,version,revision}) — NOT
# declared here. Duplicating them in the Dockerfile is the anti-pattern
# to avoid: the Dockerfile's VERSION default would stamp `v0.0.0` on branch builds
# (the component passes the VERSION build-arg only on tag builds) and fight the
# component's `image.version=$CI_COMMIT_REF_NAME` label. So the component owns them.

# pipefail so a failed `curl` in a `curl … | sh` installer (helm/uv/claude below) fails
# the build loudly instead of the shell reading EOF and exiting 0 — a silent toolless
# image otherwise (hadolint DL4006). bash is in the debian base.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# ── Toolbox: debug/network/k8s/aws tooling (mirrors the toolbox image) ──────────────
# --no-install-recommends + cache cleanup to keep the image lean.
RUN apt-get update && apt-get install -y --no-install-recommends \
    # HTTP / transfer
    curl \
    wget \
    # DNS
    bind9-dnsutils \
    ldnsutils \
    # TCP/UDP probing
    inetutils-telnet \
    netcat-openbsd \
    socat \
    nmap \
    # ICMP / routing
    iputils-ping \
    iputils-tracepath \
    traceroute \
    mtr-tiny \
    # Interfaces / sockets / firewall
    iproute2 \
    net-tools \
    ethtool \
    conntrack \
    tcpdump \
    # TLS
    openssl \
    ca-certificates \
    # Performance
    iperf3 \
    # Process / system
    procps \
    lsof \
    strace \
    ltrace \
    htop \
    sysstat \
    file \
    # Data wrangling / editors
    jq \
    yq \
    less \
    emacs-nox \
    # Python (system-wide; the agent runs from a dedicated venv below)
    python3 \
    # Misc clients
    openssh-client \
    rsync \
    git \
    # unzip: required by the AWS CLI v2 bundle installer below
    unzip \
    && rm -rf /var/lib/apt/lists/*

# kubectl — official binary, latest stable, arch-aware (dpkg arch matches k8s arch)
RUN ARCH="$(dpkg --print-architecture)" && \
    curl -fsSL "https://dl.k8s.io/release/$(curl -fsSL https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH}/kubectl" \
        -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl

# helm — official installer (handles arch detection + checksum verification itself)
RUN curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# glab (GitLab CLI) — official release tarball, latest stable, arch-aware
RUN ARCH="$(dpkg --print-architecture)" && \
    VER="$(curl -fsSL https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/releases/permalink/latest | \
        sed -n 's/.*"tag_name":"v\?\([^"]*\)".*/\1/p')" && \
    curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${VER}/downloads/glab_${VER}_linux_${ARCH}.tar.gz" \
        -o /tmp/glab.tar.gz && \
    tar -xzf /tmp/glab.tar.gz -C /tmp bin/glab && \
    install -m 0755 /tmp/bin/glab /usr/local/bin/glab && \
    rm -rf /tmp/glab.tar.gz /tmp/bin

# AWS CLI v2 — official bundle (apt ships the deprecated v1); map dpkg arch to AWS naming
RUN ARCH="$(dpkg --print-architecture)" && \
    case "$ARCH" in \
      amd64) AWS_ARCH=x86_64 ;; \
      arm64) AWS_ARCH=aarch64 ;; \
      *) echo "unsupported arch: $ARCH" >&2; exit 1 ;; \
    esac && \
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}.zip" -o /tmp/awscliv2.zip && \
    unzip -q /tmp/awscliv2.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscliv2.zip /tmp/aws

# uv — Astral installer, into /usr/local/bin so it's on PATH for any user
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Security: non-root user (uid/gid 1000). Privileged tools (tcpdump, strace, nmap raw
# sockets) need CAP_NET_RAW / CAP_SYS_PTRACE granted at runtime (--cap-add or k8s
# securityContext); they degrade gracefully when absent.
RUN groupadd --gid 1000 sandbox && \
    useradd --uid 1000 --gid 1000 --home-dir /home/sandbox --create-home --shell /bin/bash sandbox

# Claude Code CLI — native installer (no Node.js), as the sandbox user (lands in
# $HOME/.local/bin). HOME set explicitly: kaniko (CI builder) doesn't derive $HOME from
# the USER's passwd entry the way BuildKit does (kaniko#507), and the installer needs it.
USER sandbox
ENV HOME=/home/sandbox
WORKDIR /home/sandbox
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/sandbox/.local/bin:$PATH"

# opencode CLI — second in-sandbox coding-agent engine (anomalyco/opencode). Official
# installer ships a self-contained compiled binary (bun; no Node runtime) into
# $HOME/.opencode/bin. The broker spawns `opencode run --format json` per turn.
RUN curl -fsSL https://opencode.ai/install | bash
ENV PATH="/home/sandbox/.opencode/bin:$PATH"
# Warm opencode's one-time config/plugin bootstrap (@opencode-ai/sdk+plugin+zod from npm)
# and prime the models.dev catalog, so the first real turn doesn't pay that round-trip.
# Best-effort: guarded with `|| true` so an offline/registry-flaky build still succeeds
# (npm + models.dev are squid-allowlisted, so a cold container just refetches at runtime).
RUN opencode --version && (opencode models >/dev/null 2>&1 || true)

# ── inkwest_agent — LEAN bake (carved runtime deps only) ─────────────
USER root
ENV INKWEST_AGENT_HOME=/opt/inkwest-agent

# Lean deps first (own layer — changes far less often than the code). The package is
# NOT pip-installed: only requirements-agent.txt (boto3, httpx, pydantic, pydantic-
# settings, structlog, mcp; ~7) — no DB ORM / web stack, no inkwest-backend metadata.
COPY requirements-agent.txt ${INKWEST_AGENT_HOME}/requirements-agent.txt
RUN uv venv "${INKWEST_AGENT_HOME}/.venv" && \
    uv pip install --python "${INKWEST_AGENT_HOME}/.venv/bin/python" --no-cache \
        --requirement "${INKWEST_AGENT_HOME}/requirements-agent.txt"

# The code — COPYed from the build context (backend/), not installed.
COPY app/ ${INKWEST_AGENT_HOME}/app/
COPY inkwest_agent/ ${INKWEST_AGENT_HOME}/inkwest_agent/

# `inkwest-agent` launcher on PATH: run the COPYed package with the lean venv. PYTHONPATH
# carries the code (not pip-installed). `uv pip check` (a uv venv has no pip, so not
# `python -m pip`) asserts the dep set is self-consistent at build time.
RUN printf '#!/bin/sh\nexec env PYTHONPATH=%s %s/.venv/bin/python -m inkwest_agent "$@"\n' \
        "${INKWEST_AGENT_HOME}" "${INKWEST_AGENT_HOME}" > /usr/local/bin/inkwest-agent && \
    chmod +x /usr/local/bin/inkwest-agent && \
    uv pip check --python "${INKWEST_AGENT_HOME}/.venv/bin/python"

USER sandbox
# Idle by default: the broker execs `inkwest-agent < turn-spec.json` per turn; the
# container itself runs as an ephemeral/sidecar pod.
CMD ["sleep", "infinity"]
