#!/usr/bin/env bash

# Safe parity preflight helper.
# Intentionally runs only setup and baseline capture (Step 0 + Step 1).
# Step 2 (k1s mini-matrix) and Step 3 (k3s probes) are printed for manual execution.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

RUN_ID_DEFAULT="$(date -u +%Y%m%dT%H%M%SZ)-parity"
RUN_ID="$RUN_ID_DEFAULT"
ROOT=""
PRINT_EXPORTS=0
JSON_OUTPUT=0
CHECK_CORE_PROXY=1
CORE_PROXY_HTTP_URL="http://127.0.0.1:10080/"

STATUS=0
WARNINGS=0
IS_SOURCED=0
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  IS_SOURCED=1
fi

usage() {
  cat <<'USAGE'
Usage: scripts/dev/parity_preflight.sh [options]

Safe helper for docs/ops/perf-parity-k1s-vs-k3s.md.
Runs setup and environment baseline capture only (before Step 2 and Step 3).

Options:
  --run-id <id>                Override run id (default: <utc-timestamp>-parity)
  --root <path>                Override output root path
  --print-exports              Print export lines for RUN_ID and ROOT
  --json                       Print machine-readable summary JSON
  --core-proxy-url <url>       URL for core-proxy readiness check (default: http://127.0.0.1:10080/)
  --skip-core-proxy-check      Skip core-proxy listener readiness check
  -h, --help                   Show help
USAGE
}

log() {
  printf '[parity-preflight] %s\n' "$*"
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  printf '[parity-preflight] WARN: %s\n' "$*" >&2
}

err() {
  STATUS=1
  printf '[parity-preflight] ERROR: %s\n' "$*" >&2
}

resolve_path() {
  local raw="$1"
  if [[ "$raw" = /* ]]; then
    printf '%s\n' "$raw"
  else
    printf '%s/%s\n' "$ROOT_DIR" "$raw"
  fi
}

safe_write_cmd() {
  local out_path="$1"
  shift
  if "$@" > "$out_path" 2>/dev/null; then
    return 0
  fi
  return 1
}

listener_present() {
  local port="$1"
  ss -ltn 2>/dev/null | rg -q ":${port}\\b"
}

core_proxy_bootstrap_check() {
  if [[ "$CHECK_CORE_PROXY" -eq 0 ]]; then
    return 0
  fi

  if ! command -v ss >/dev/null 2>&1; then
    err "missing required command: ss (needed for core-proxy readiness check)"
    return 0
  fi

  if listener_present "10080"; then
    log "core-proxy readiness OK: listener 10080 present"
  else
    err "core-proxy readiness failed: listener 10080 missing"
    printf '[parity-preflight] HINT: start the core-proxy lane before Step 2:\n' >&2
    printf '  AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core\n' >&2
    printf '[parity-preflight] HINT: strict-CRI profile alternative:\n' >&2
    printf '  AE_DEV_LOCAL=1 EDGE_INGRESS_MODE=core-proxy make k1s-core-cri\n' >&2
    printf '[parity-preflight] HINT: validate ingress env before rerun:\n' >&2
    printf '  scripts/dev/validate_ingress_env.sh --lane core-proxy --watchdog\n' >&2
    return 0
  fi

  if command -v curl >/dev/null 2>&1; then
    local code="000"
    code="$(curl -sS -k --connect-timeout 2 --max-time 5 -o /dev/null -w '%{http_code}' "$CORE_PROXY_HTTP_URL" 2>/dev/null || printf '000')"
    if [[ "$code" == "000" ]]; then
      warn "core-proxy url probe failed for $CORE_PROXY_HTTP_URL (listener exists, but HTTP probe did not connect)"
    else
      log "core-proxy url probe code=$code url=$CORE_PROXY_HTTP_URL"
    fi
  fi
}

early_exit() {
  local code="${1:-0}"
  if [[ "$IS_SOURCED" -eq 1 ]]; then
    return "$code"
  fi
  exit "$code"
}

EXIT_REQUESTED=0
EXIT_CODE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        err "--run-id requires a non-empty value"
        EXIT_REQUESTED=1
        EXIT_CODE=2
        break
      fi
      RUN_ID="$2"
      shift 2
      ;;
    --root)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        err "--root requires a non-empty value"
        EXIT_REQUESTED=1
        EXIT_CODE=2
        break
      fi
      ROOT="$(resolve_path "$2")"
      shift 2
      ;;
    --print-exports)
      PRINT_EXPORTS=1
      shift
      ;;
    --json)
      JSON_OUTPUT=1
      shift
      ;;
    --core-proxy-url)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        err "--core-proxy-url requires a non-empty value"
        EXIT_REQUESTED=1
        EXIT_CODE=2
        break
      fi
      CORE_PROXY_HTTP_URL="$2"
      shift 2
      ;;
    --skip-core-proxy-check)
      CHECK_CORE_PROXY=0
      shift
      ;;
    -h|--help)
      usage
      EXIT_REQUESTED=1
      EXIT_CODE=0
      shift
      break
      ;;
    *)
      err "unknown argument: $1"
      usage
      EXIT_REQUESTED=1
      EXIT_CODE=2
      break
      ;;
  esac
done

if [[ "$EXIT_REQUESTED" -eq 1 ]]; then
  early_exit "$EXIT_CODE"
fi

if [[ -z "$RUN_ID" ]]; then
  err "--run-id cannot be empty"
  early_exit 2
fi

if [[ -z "$ROOT" ]]; then
  ROOT="$ROOT_DIR/state/test-results/parity/$RUN_ID"
fi

for cmd in jq python rg; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    err "missing required command: $cmd"
  fi
done

if mkdir -p "$ROOT/env" "$ROOT/k1s" "$ROOT/k3s" "$ROOT/summary"; then
  :
else
  err "failed to create output directories under $ROOT"
fi

if safe_write_cmd "$ROOT/env/uname.txt" uname -a; then
  :
else
  err "failed to capture uname -a to $ROOT/env/uname.txt"
fi

if safe_write_cmd "$ROOT/env/lscpu.txt" lscpu; then
  :
else
  err "failed to capture lscpu to $ROOT/env/lscpu.txt"
fi

if safe_write_cmd "$ROOT/env/mem.txt" free -h; then
  :
else
  err "failed to capture free -h to $ROOT/env/mem.txt"
fi

if command -v rg >/dev/null 2>&1; then
  if env | rg '^AE_|^KUBECONFIG|^PATH' > "$ROOT/env/env.txt" 2>/dev/null; then
    :
  else
    warn "failed to filter env with rg; writing full env snapshot instead"
    if ! env > "$ROOT/env/env.txt" 2>/dev/null; then
      err "failed to capture env to $ROOT/env/env.txt"
    fi
  fi
else
  warn "rg not available; using grep fallback for env capture"
  if env | grep -E '^AE_|^KUBECONFIG|^PATH' > "$ROOT/env/env.txt" 2>/dev/null; then
    :
  else
    warn "grep fallback failed; writing full env snapshot instead"
    if ! env > "$ROOT/env/env.txt" 2>/dev/null; then
      err "failed to capture env to $ROOT/env/env.txt"
    fi
  fi
fi

core_proxy_bootstrap_check

if [[ "$STATUS" -ne 0 ]]; then
  log "preflight found blocking issues; fix the errors above before running Step 2"
fi

log "preflight setup complete (stopped before Step 2 and Step 3)"
log "run_id=$RUN_ID"
log "root=$ROOT"
log "env_dir=$ROOT/env"

if [[ "$PRINT_EXPORTS" -eq 1 ]]; then
  printf 'export RUN_ID=%q\n' "$RUN_ID"
  printf 'export ROOT=%q\n' "$ROOT"
fi

cat <<EOF2

Manual Step 2 (k1s shakedown mini-matrix):
RESULTS_DIR="$ROOT/k1s" \\
RUN_STAMP_BASE="${RUN_ID}-k1s-r1" \\
CONCURRENCIES_CSV=30,50,70 \\
PERF_MIN_RPS=220 \\
PERF_MAX_P95_MS=300 \\
PERF_MAX_P99_MS=500 \\
PERF_MAX_ERROR_RATE=0.01 \\
WS_MIN_CONNECTED_RATIO=1 \\
WS_MAX_CONNECT_FAILURE_RATE=0 \\
WS_MAX_MESSAGE_LOSS=0 \\
scripts/dev/run_ingress_kpi_minimatrix.sh

Manual Step 3 (k3s equivalent lanes):
make bench-k3s-up K3S_NAME=bench
kubectl config use-context k3d-bench
kubectl delete ingress k3s-parity-sticky-cookie --ignore-not-found
kubectl apply -f specs/examples/k3s-ingress-parity.yaml

kubectl rollout status deployment/k3s-parity-ws-echo --timeout=180s
kubectl rollout status deployment/k3s-parity-lb-distribution --timeout=180s
kubectl rollout status deployment/k3s-parity-sticky-cookie --timeout=180s
kubectl get ingress -n default
kubectl get ingressroute.traefik.io -n default k3s-parity-sticky-cookie

# k3d helper exposes k3s ingress on host :443
K3S_BASE_URL="https://127.0.0.1"
mkdir -p "$ROOT/k3s"

python scripts/dev/ingress_deep_probe.py ws_soak --url "\$K3S_BASE_URL/ws" --host "ws-echo-core-proxy.home.arpa" --duration-seconds 600 --connections 50 --heartbeat-seconds 5 > "$ROOT/k3s/k3s-r1-ws-echo-deep.json"
python scripts/dev/ingress_deep_probe.py lb_sample --url "\$K3S_BASE_URL/id" --host "lb-distribution-core-proxy.home.arpa" --strategy round_robin --requests 5000 --min-backends 2 --max-skew-ratio 0.35 > "$ROOT/k3s/k3s-r1-lb-distribution-deep.json"
python scripts/dev/ingress_deep_probe.py sticky_probe --url "\$K3S_BASE_URL/id" --host "sticky-cookie-core-proxy.home.arpa" --requests-per-client 100 > "$ROOT/k3s/k3s-r1-sticky-deep.json"

for c in 30 50 70; do
  python scripts/dev/ingress_deep_probe.py http_bench --url "\$K3S_BASE_URL/id" --host "ws-echo-core-proxy.home.arpa" --duration-seconds 180 --warmup-seconds 20 --concurrency "\$c" > "$ROOT/k3s/k3s-r1-ws-echo-c\${c}.json"
  python scripts/dev/ingress_deep_probe.py http_bench --url "\$K3S_BASE_URL/id" --host "lb-distribution-core-proxy.home.arpa" --duration-seconds 180 --warmup-seconds 20 --concurrency "\$c" > "$ROOT/k3s/k3s-r1-lb-c\${c}.json"
  python scripts/dev/ingress_deep_probe.py http_bench --url "\$K3S_BASE_URL/id" --host "sticky-cookie-core-proxy.home.arpa" --duration-seconds 180 --warmup-seconds 20 --concurrency "\$c" > "$ROOT/k3s/k3s-r1-sticky-c\${c}.json"
done

Expected Step 3 artifacts:
- $ROOT/k3s/k3s-r1-ws-echo-deep.json
- $ROOT/k3s/k3s-r1-lb-distribution-deep.json
- $ROOT/k3s/k3s-r1-sticky-deep.json
- $ROOT/k3s/k3s-r1-ws-echo-c30.json
- $ROOT/k3s/k3s-r1-ws-echo-c50.json
- $ROOT/k3s/k3s-r1-ws-echo-c70.json
- $ROOT/k3s/k3s-r1-lb-c30.json
- $ROOT/k3s/k3s-r1-lb-c50.json
- $ROOT/k3s/k3s-r1-lb-c70.json
- $ROOT/k3s/k3s-r1-sticky-c30.json
- $ROOT/k3s/k3s-r1-sticky-c50.json
- $ROOT/k3s/k3s-r1-sticky-c70.json

EOF2

if [[ "$JSON_OUTPUT" -eq 1 ]]; then
  if command -v python >/dev/null 2>&1; then
    RUN_ID="$RUN_ID" ROOT="$ROOT" STATUS="$STATUS" WARNINGS="$WARNINGS" CHECK_CORE_PROXY="$CHECK_CORE_PROXY" python - <<'PY'
import json
import os

payload = {
    "run_id": os.environ.get("RUN_ID", ""),
    "root": os.environ.get("ROOT", ""),
    "status": "ok" if os.environ.get("STATUS") == "0" else "error",
    "warnings": int(os.environ.get("WARNINGS", "0")),
    "core_proxy_check_enabled": os.environ.get("CHECK_CORE_PROXY") == "1",
    "stopped_before_step2": True,
    "stopped_before_step3": True,
}
print(json.dumps(payload, sort_keys=True))
PY
  else
    warn "python unavailable; skipping --json output"
  fi
fi

early_exit "$STATUS"
