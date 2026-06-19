#!/usr/bin/env bash
#
# Diagnose the dind sandbox egress chain for a TARGET (hostname or IP), the way a
# per-conversation toolbox would experience it. Each layer is checked independently so a
# failure pinpoints the cause instead of surfacing as one opaque timeout:
#
#   1. DNS      — does the proxy's resolver (SANDBOX_PROXY_DNS) resolve the name?
#   2. route    — does dind have an L3 route to the resolved IP(s), and via which leg?
#   3. egress   — does a throwaway squid proxy built from the LIVE config let the request
#                 through (allowlist) and reach the target? (TCP_DENIED vs timeout vs OK)
#
# It spins up a temporary proxy + client inside dind mirroring how the backend provisions
# them (same squid.conf / conf.d / errors / resolver / images), runs the checks, tears it
# all down. Read-only w.r.t. the running stack; creates only ephemeral, labelled objects.
#
#   ./scripts/check-dind-connectivity.sh <target> [--http]
#
# Env overrides:  DIND_CONTAINER (default: the inkwest compose dind service)
set -euo pipefail

readonly TAG="dindcheck_$$"
SCHEME="https"

usage() { echo "usage: $0 <target-host-or-ip> [--http]" >&2; exit 2; }

TARGET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --http) SCHEME="http" ;;
    --https) SCHEME="https" ;;
    -h|--help) usage ;;
    -*) echo "unknown flag: $1" >&2; usage ;;
    *) if [[ -z "$TARGET" ]]; then TARGET="$1"; else usage; fi ;;
  esac
  shift
done
[[ -n "$TARGET" ]] || usage

# Run from the repo root so `docker compose` resolves the project regardless of CWD.
cd "$(dirname "$0")/.."

DIND="${DIND_CONTAINER:-$(docker ps \
  --filter 'label=com.docker.compose.project=inkwest' \
  --filter 'name=dind' --format '{{.Names}}' | head -n1)}"
[[ -n "$DIND" ]] || { echo "ERROR: dind container not found (set DIND_CONTAINER)"; exit 1; }

din() { docker compose exec -T dind docker "$@"; }  # run a docker command inside dind

# Single source of truth: the backend's resolved settings — exactly what provision() uses.
read_settings() {
  docker compose exec -T -w /app backend python - <<'PY'
from app.core.config import get_settings
s = get_settings()
for k, v in {
    "PROXY_DNS": s.sandbox_proxy_dns,
    "SQUID_IMAGE": s.sandbox_squid_image,
    "TOOLBOX_IMAGE": s.sandbox_toolbox_image,
    "SQUID_CONF": s.sandbox_squid_conf,
    "SQUID_CONFD": s.sandbox_squid_confd,
    "SQUID_ERRORS": s.sandbox_squid_errors,
}.items():
    print(f"{k}={v}")
PY
}

CFG="$(read_settings 2>/dev/null)" || { echo "ERROR: could not read backend settings (is the stack up?)"; exit 1; }
val() { grep "^$1=" <<<"$CFG" | cut -d= -f2-; }
PROXY_DNS="$(val PROXY_DNS)"; SQUID_IMAGE="$(val SQUID_IMAGE)"; TOOLBOX_IMAGE="$(val TOOLBOX_IMAGE)"
SQUID_CONF="$(val SQUID_CONF)"; SQUID_CONFD="$(val SQUID_CONFD)"; SQUID_ERRORS="$(val SQUID_ERRORS)"
[[ -n "$SQUID_IMAGE" && -n "$TOOLBOX_IMAGE" ]] || { echo "ERROR: sandbox images not configured"; exit 1; }

# shellcheck disable=SC2329  # invoked indirectly via `trap cleanup EXIT`
cleanup() {
  din rm -f "${TAG}_box" "${TAG}_proxy" >/dev/null 2>&1 || true
  din network rm "${TAG}_int" "${TAG}_ext" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "target: $TARGET   scheme: $SCHEME   dind: $DIND   resolver: ${PROXY_DNS:-<container default>}"
echo

# --- build the throwaway proxy + client, mirroring provision()'s mounts ---
din network create --internal "${TAG}_int" >/dev/null
din network create "${TAG}_ext" >/dev/null
proxy_run=(run -d --name "${TAG}_proxy" --network "${TAG}_int")
[[ -n "$SQUID_CONF"   ]] && proxy_run+=(-v "${SQUID_CONF}:/etc/squid/squid.conf:ro")
[[ -n "$SQUID_CONFD"  ]] && proxy_run+=(-v "${SQUID_CONFD}:/conf.d:ro")
[[ -n "$SQUID_ERRORS" ]] && proxy_run+=(-v "${SQUID_ERRORS}:/etc/squid/errors:ro")
[[ -n "$PROXY_DNS"    ]] && proxy_run+=(--dns "$PROXY_DNS")
proxy_run+=("$SQUID_IMAGE")
din "${proxy_run[@]}" >/dev/null
din network connect "${TAG}_ext" "${TAG}_proxy"

# Wait for squid to accept connections (or fail loudly if its config FATALs).
ready=""
for _ in $(seq 1 15); do
  if din exec "${TAG}_proxy" squid -k check >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [[ -z "$ready" ]]; then
  echo "FAIL: proxy did not become ready — squid config error? Last cache.log lines:"
  din logs "${TAG}_proxy" 2>&1 | tail -5 | sed 's/^/    /'
  exit 1
fi
din run -d --name "${TAG}_box" --network "${TAG}_int" \
  -e "http_proxy=http://${TAG}_proxy:3128" -e "https_proxy=http://${TAG}_proxy:3128" \
  "$TOOLBOX_IMAGE" sleep 120 >/dev/null
box() { din exec "${TAG}_box" "$@"; }

rc=0
is_ip() { [[ "$1" =~ ^[0-9]+(\.[0-9]+){3}$ ]]; }

# --- 1. DNS ---
# Probe from the EXTERNAL network, not the client: the client is on the --internal net and
# can only reach squid, whereas squid resolves from its egress side. A throwaway probe on
# ext_net with the same resolver mirrors squid's resolution path.
ips=""
if is_ip "$TARGET"; then
  ips="$TARGET"
  echo "[1/3] DNS      : skipped (target is a literal IP)"
else
  dig_cmd=(dig +short +timeout=3); [[ -n "$PROXY_DNS" ]] && dig_cmd+=("@$PROXY_DNS"); dig_cmd+=("$TARGET")
  ips="$(din run --rm --network "${TAG}_ext" "$TOOLBOX_IMAGE" "${dig_cmd[@]}" 2>/dev/null \
        | grep -E '^[0-9]+(\.[0-9]+){3}$' || true)"
  if [[ -n "$ips" ]]; then
    echo "[1/3] DNS      : OK   $TARGET -> $(echo "$ips" | paste -sd, -)  (via ${PROXY_DNS:-container default})"
  else
    echo "[1/3] DNS      : FAIL  $TARGET did not resolve via ${PROXY_DNS:-container default} (NXDOMAIN / no A record / resolver unreachable)"
    rc=1
  fi
fi

# --- 2. route in dind ---
# `ip route get` must run in the dind container's own netns (host daemon), NOT via a nested
# `docker exec`. Iterate with a for-loop so the docker call can't swallow the loop's stdin.
if [[ -n "$ips" ]]; then
  echo "[2/3] route    :"
  for ip in $ips; do
    r="$(docker compose exec -T dind ip route get "$ip" 2>&1 | head -n1 || true)"
    echo "               $ip -> ${r:-<no route>}"
    [[ "$r" == *"unreachable"* || -z "$r" ]] && rc=1
  done
else
  echo "[2/3] route    : skipped (no IP to test)"
fi

# --- 3. egress through the live-config proxy ---
# squid is authoritative: it resolves + applies the allowlist + connects, and tags its own
# failures with X-Squid-Error (the custom error pages in sandbox/errors) — so this single
# request classifies the failing layer precisely.
hdr="$(box curl -sS -D - -o /dev/null --max-time 12 "${SCHEME}://${TARGET}/" 2>/dev/null || true)"
# awk (not `… | head`) to avoid a SIGPIPE that would trip set -e/pipefail on multi-status
# responses (HTTPS via proxy emits `CONNECT 200` then the real status). Take the LAST HTTP
# status (upstream's real code) and the last X-Squid-Error, if any.
code="$(printf '%s\n' "$hdr" | awk '/^HTTP\/[0-9]/{c=$2} END{print c}' | tr -dc '0-9')"
squid_err="$(printf '%s\n' "$hdr" | awk -F': ' 'tolower($1)=="x-squid-error"{e=$2} END{print e}' | tr -d '\r')"
case "$squid_err" in
  *ERR_ACCESS_DENIED*|*ERR_PORT_BLOCKED*)
    echo "[3/3] egress   : DENIED by allowlist (HTTP $code, $squid_err) — target not permitted in squid.conf / conf.d"; rc=1 ;;
  *ERR_DNS_FAIL*)
    echo "[3/3] egress   : FAIL DNS (squid could not resolve, $squid_err) — check the resolver (SANDBOX_PROXY_DNS)"; rc=1 ;;
  *ERR_CONNECT_FAIL*|*ERR_SECURE_CONNECT_FAIL*|*ERR_READ_TIMEOUT*|*ERR_ZERO_SIZE_OBJECT*)
    echo "[3/3] egress   : FAIL connect ($squid_err) — allowlist OK but no L3 path / upstream down"; rc=1 ;;
  "")
    if [[ -z "$code" ]]; then
      echo "[3/3] egress   : FAIL  no response within timeout — L3 route or upstream unreachable"; rc=1
    else
      echo "[3/3] egress   : OK   reached target, upstream returned HTTP $code (allowlist + route OK)"
    fi ;;
  *)
    echo "[3/3] egress   : proxy error ($squid_err, HTTP $code)"; rc=1 ;;
esac

echo
if [[ "$rc" -eq 0 ]]; then
  echo "RESULT: reachable — all layers OK."
else
  echo "RESULT: NOT reachable — see the failing layer above (DNS / route / egress)."
fi
exit "$rc"
