#!/usr/bin/env bash
# Pull the sandbox toolbox image straight into the dind daemon — no host-side
# pull, no save/load tarball. Layers are incremental (dind's /var/lib/docker is
# persisted in the sandbox-dind-data volume) and dind selects its native arch
# from the manifest, so arm64 Macs get arm64 layers once the image is multi-arch.
set -euo pipefail
trap 'echo "error: line $LINENO: \`$BASH_COMMAND\` failed (rc=$?)" >&2' ERR

cd "$(dirname "$0")/.."

# backend/.env is the pydantic env file — the single source of truth for the image ref.
ENV_FILE="backend/.env"
AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_REGION="${AWS_REGION:-eu-west-1}"

IMAGE="$(grep -E '^SANDBOX_TOOLBOX_IMAGE=' "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
if [[ -z "$IMAGE" ]]; then
  echo "error: SANDBOX_TOOLBOX_IMAGE is not set in $ENV_FILE" >&2
  exit 1
fi
# A registry-hosted ref carries a host before the first '/' that contains a '.' or ':'
# (ECR: <acct>.dkr.ecr.<region>.amazonaws.com/path:tag). Anything else — a bare
# name:tag, or a host-less path like org/img:tag — is a locally-built image loaded
# straight into dind (.local/sandbox-agent-build-local.sh): no registry to log into,
# and `docker login`/`docker pull` on it would mis-parse the tag as a port. Verify it's
# present in dind and skip the pull.
REGISTRY="${IMAGE%%/*}"
if [[ "$IMAGE" != */* || ( "$REGISTRY" != *.* && "$REGISTRY" != *:* ) ]]; then
  echo "==> $IMAGE is a local image (no registry host) — skipping ECR login/pull."
  if ! docker compose exec -T dind docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "error: $IMAGE not found in dind — build it with .local/sandbox-agent-build-local.sh" >&2
    exit 1
  fi
  echo "==> $IMAGE already present in dind."
  exit 0
fi

echo "==> Logging dind into $REGISTRY (AWS profile: $AWS_PROFILE)"
AWS_PROFILE="$AWS_PROFILE" aws ecr get-login-password --region "$AWS_REGION" \
  | docker compose exec -T dind docker login --username AWS --password-stdin "$REGISTRY"

echo "==> Pulling $IMAGE"
docker compose exec -T dind docker pull "$IMAGE"
