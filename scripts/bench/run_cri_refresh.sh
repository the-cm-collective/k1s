#!/usr/bin/env bash
set -euo pipefail

# Refresh benchmarks using the CRI backend (containerd).
# Requires: sudo + containerd socket + crictl (for ingress reloads if enabled).

if [[ "$(id -u)" -eq 0 ]]; then
  echo "[cri-refresh] do not run as root; run as your user (sudo used internally)" >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

APP="${APP:-specs/examples/echo.yaml}"
APP_NAME="${APP_NAME:-echo}"
REPLICAS="${REPLICAS:-1,5,10}"
ROLL_REPLICAS="${ROLL_REPLICAS:-5}"
DURATION="${DURATION:-30}"

LABEL_CRI="${LABEL_CRI:-r$(date +%Y%m%d)+cri+containerd}"
metrics_port="${BENCH_CRI_METRICS_PORT:-9212}"
env_file="${BENCH_CRI_ENV_FILE:-state/bench-cri/env.sh}"

bench_specs_minimal="${BENCH_SPECS_MINIMAL:-0}"
bench_specs_empty="${BENCH_SPECS_EMPTY:-0}"

export AE_RUNTIME_BACKEND="${AE_RUNTIME_BACKEND:-cri}"
export AE_APISHIM_RUNTIME="${AE_APISHIM_RUNTIME:-cri}"
export AE_CRI_ENDPOINT="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
export AE_CRI_SANDBOX_IMAGE="${AE_CRI_SANDBOX_IMAGE:-registry.k8s.io/pause:3.9}"
export AE_REGISTER_LOCAL_NODE=1
export AE_NODE_NOTREADY_AFTER="${BENCH_NODE_NOTREADY_AFTER:-${AE_NODE_NOTREADY_AFTER:-600}}"
export AE_ALLOW_PLAINTEXT_SECRETS=1

endpoint_path="${AE_CRI_ENDPOINT#unix://}"
if [[ ! -S "$endpoint_path" ]]; then
  echo "[cri-refresh] warning: CRI endpoint socket not found at $endpoint_path" >&2
fi

if ! command -v sudo >/dev/null 2>&1; then
  echo "[cri-refresh] sudo is required for CRI benchmark (controller uses containerd socket)" >&2
  exit 3
fi

# Guard: ensure CRI benches are not accidentally shortened by pre-set env vars.
if [[ -n "${WAIT_READY_TRIES:-}" || -n "${WAIT_READY_DELAY:-}" ]]; then
  echo "[cri-refresh] clearing WAIT_READY_* overrides for CRI benchmark (use defaults)" >&2
  unset WAIT_READY_TRIES
  unset WAIT_READY_DELAY
fi
if [[ "${AE_BENCH_QUICK:-0}" == "1" ]]; then
  echo "[cri-refresh] disabling AE_BENCH_QUICK (forces short waits) for CRI benchmark" >&2
  unset AE_BENCH_QUICK
fi

ENV_FILE="$(
  BENCH_SPECS_MINIMAL="$bench_specs_minimal" \
  BENCH_SPECS_EMPTY="$bench_specs_empty" \
  ./scripts/bench/bench_env_prep.sh \
    --manifest "$APP" \
    --metrics-port "$metrics_port" \
    --env-file "$env_file" \
    --sudo-controller
)"

# shellcheck disable=SC1090
source "$ENV_FILE"

bench_app_name="${BENCH_PRIMARY_APP:-$APP_NAME}"
python_bin="${PYTHON_BIN:-python}"
sudo_cmd=(sudo)

cri_has_image() {
  local ref="$1"
  if ! command -v crictl >/dev/null 2>&1; then
    return 1
  fi
  "${sudo_cmd[@]}" crictl --runtime-endpoint "$AE_CRI_ENDPOINT" images -o json 2>/dev/null | \
    "$python_bin" - "$ref" <<'PY'
import json, sys
ref = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for img in data.get("images", []):
    for tag in img.get("repoTags") or []:
        if tag == ref:
            sys.exit(0)
sys.exit(1)
PY
}

cri_pull_image() {
  local ref="$1"
  if ! command -v crictl >/dev/null 2>&1; then
    return 1
  fi
  "${sudo_cmd[@]}" crictl --runtime-endpoint "$AE_CRI_ENDPOINT" pull "$ref" >/dev/null 2>&1
}

cri_import_image() {
  local ref="$1"
  if ! command -v ctr >/dev/null 2>&1; then
    return 1
  fi
  local tmp=""
  if command -v podman >/dev/null 2>&1 && podman image exists "$ref" >/dev/null 2>&1; then
    tmp=$(mktemp)
    podman save "$ref" -o "$tmp" >/dev/null 2>&1 || true
    "${sudo_cmd[@]}" ctr -n k8s.io images import "$tmp" >/dev/null 2>&1 || true
    rm -f "$tmp"
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker image inspect "$ref" >/dev/null 2>&1; then
    tmp=$(mktemp)
    docker save "$ref" -o "$tmp" >/dev/null 2>&1 || true
    "${sudo_cmd[@]}" ctr -n k8s.io images import "$tmp" >/dev/null 2>&1 || true
    rm -f "$tmp"
    return 0
  fi
  return 1
}

ensure_cri_image() {
  local ref="$1"
  if cri_has_image "$ref"; then
    return 0
  fi
  cri_import_image "$ref" || true
  if cri_has_image "$ref"; then
    return 0
  fi
  if [[ "$ref" != localhost/* ]]; then
    cri_pull_image "$ref" || true
  fi
  cri_has_image "$ref"
}

resolve_rollout_images() {
  local manifest_path="$1"
  local base_image=""
  if [[ -f "$manifest_path" ]]; then
    base_image=$(awk '/^[[:space:]]*image:/ {print $2; exit}' "$manifest_path" | tr -d '"')
  fi
  if [[ -z "$base_image" ]]; then
    base_image="mendhak/http-https-echo:37"
  fi
  local blue="${AE_ROLLOUT_IMAGE_BLUE:-localhost/demo-blue:latest}"
  local green="${AE_ROLLOUT_IMAGE_GREEN:-localhost/demo-green:latest}"

  if ! ensure_cri_image "$blue"; then
    echo "[cri-refresh] rollout image missing in containerd: $blue; falling back to $base_image" >&2
    blue="$base_image"
    ensure_cri_image "$blue" || true
  fi

  if ! ensure_cri_image "$green"; then
    local alt="$base_image"
    if [[ "$base_image" != *:latest ]]; then
      alt="${base_image%:*}:latest"
    fi
    if [[ "$alt" != "$base_image" ]] && ensure_cri_image "$alt"; then
      green="$alt"
    else
      echo "[cri-refresh] rollout image missing in containerd: $green; falling back to $base_image" >&2
      green="$base_image"
      ensure_cri_image "$green" || true
    fi
  fi

  export AE_ROLLOUT_IMAGE_BLUE="$blue"
  export AE_ROLLOUT_IMAGE_GREEN="$green"
}

resolve_rollout_images "${BENCH_PRIMARY_MANIFEST:-$APP}"

AE_ENGINE_STRICT=1 \
LABEL_SUITE="$LABEL_CRI" \
APP="$BENCH_PRIMARY_MANIFEST" APP_NAME="$bench_app_name" \
REPLICAS="$REPLICAS" DURATION="$DURATION" AE_COLLECT_ENGINE=cri \
./scripts/bench/run_matrix.sh \
  --label-suite "$LABEL_CRI" \
  --app "$BENCH_PRIMARY_MANIFEST" \
  --app-name "$bench_app_name" \
  --replicas "$REPLICAS" \
  --duration "$DURATION" \
  --sudo

AE_ENGINE_STRICT=1 \
LABEL_SUITE_ROLL="$LABEL_CRI" \
APP="$BENCH_PRIMARY_MANIFEST" APP_NAME="$bench_app_name" \
ROLL_REPLICAS="$ROLL_REPLICAS" DURATION="$DURATION" AE_COLLECT_ENGINE=cri \
./scripts/bench/run_rollout_k1s.sh \
  --label-suite "$LABEL_CRI" \
  --app "$BENCH_PRIMARY_MANIFEST" \
  --app-name "$bench_app_name" \
  --replicas "$ROLL_REPLICAS" \
  --duration "$DURATION" \
  --sudo

./scripts/bench/bench_env_teardown.sh --env "$ENV_FILE"

make bench-mem-docs
