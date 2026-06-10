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
REGISTRY="${IMAGE%%/*}"

echo "==> Logging dind into $REGISTRY (AWS profile: $AWS_PROFILE)"
AWS_PROFILE="$AWS_PROFILE" aws ecr get-login-password --region "$AWS_REGION" \
  | docker compose exec -T dind docker login --username AWS --password-stdin "$REGISTRY"

echo "==> Pulling $IMAGE"
docker compose exec -T dind docker pull "$IMAGE"
