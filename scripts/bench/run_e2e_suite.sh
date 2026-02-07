#!/usr/bin/env bash
set -euo pipefail

MODE="full"
MANIFEST="${BENCH_MANIFEST:-specs/examples/echo.yaml}"
DURATION="${DURATION:-30}"
REPLICAS="${REPLICAS:-1,5,10}"
ROLL_REPLICAS="${ROLL_REPLICAS:-2,5}"
LABEL_ROOTFUL="${LABEL_ROOTFUL:-r$(date +%Y%m%d)+podman+rootful+cg2}"
LABEL_ROOTLESS="${LABEL_ROOTLESS:-r$(date +%Y%m%d)+podman+rootless+cg2}"
LABEL_DEV_MIN="${LABEL_DEV_MIN:-r$(date +%Y%m%d)+podman+dev-min}"
LABEL_K1ND="${LABEL_K1ND:-r$(date +%Y%m%d)+docker+k1nd}"
METRICS_PORT="${BENCH_METRICS_PORT:-9210}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"; shift 2;;
    --manifest)
      MANIFEST="$2"; shift 2;;
    --duration)
      DURATION="$2"; shift 2;;
    --replicas)
      REPLICAS="$2"; shift 2;;
    --roll-replicas)
      ROLL_REPLICAS="$2"; shift 2;;
    --metrics-port)
      METRICS_PORT="$2"; shift 2;;
    *)
      echo "[bench-suite] unknown arg: $1" >&2; exit 2;;
  esac
done

need_sudo() {
  if command -v sudo >/dev/null 2>&1; then
    sudo -v
  fi
}

stop_dev_stacks() {
  if command -v docker >/dev/null 2>&1; then
    sudo docker compose -f ops/dev/docker-compose.yaml down >/dev/null 2>&1 || true
  fi
}

run_make() {
  local target=$1
  local label=$2
  shift 2 || true
  ( . "$ENV_FILE";
    DURATION="$DURATION" REPLICAS="$REPLICAS" ROLL_REPLICAS="$ROLL_REPLICAS" \
    LABEL_SUITE="$label" LABEL_SUITE_ROLL="$label" APP="$APP_PATH" APP_NAME="$APP_NAME" \
    make --no-print-directory "$target" "$@" )
}

run_script() {
  ( . "$ENV_FILE"; "$@" )
}

need_sudo
stop_dev_stacks

prep_args=(--manifest "$MANIFEST" --metrics-port "$METRICS_PORT")
if [[ "$MODE" == "minimal" ]]; then
  prep_args+=(--sudo-controller)
fi
ENV_FILE=$(./scripts/bench/bench_env_prep.sh "${prep_args[@]}")
trap 'scripts/bench/bench_env_teardown.sh --env "$ENV_FILE"' EXIT

# shellcheck disable=SC1090
source "$ENV_FILE"
APP_PATH="${BENCH_PRIMARY_MANIFEST:-$MANIFEST}"
APP_NAME="${BENCH_PRIMARY_APP:-echo}"

if [[ "$MODE" == "minimal" ]]; then
  MIN_REPLICAS="${REPLICAS:-1}"
  MIN_ROLL="${ROLL_REPLICAS:-2}"
  MIN_DURATION="${DURATION:-10}"
  ( . "$ENV_FILE"; \
    DURATION="$MIN_DURATION" \
    WAIT_READY_TRIES="${WAIT_READY_TRIES:-60}" \
    WAIT_READY_DELAY="${WAIT_READY_DELAY:-2}" \
    ./scripts/bench/run_matrix.sh --label-suite "$LABEL_ROOTFUL" --app "$APP_PATH" \
      --app-name "$APP_NAME" --replicas "$MIN_REPLICAS" --duration "$MIN_DURATION" --sudo )
  ( . "$ENV_FILE"; \
    DURATION="$MIN_DURATION" \
    WAIT_READY_TRIES="${WAIT_READY_TRIES:-60}" \
    WAIT_READY_DELAY="${WAIT_READY_DELAY:-2}" \
    ./scripts/bench/run_rollout_k1s.sh --label-suite "$LABEL_ROOTFUL" --app "$APP_PATH" \
      --app-name "$APP_NAME" --replicas "$MIN_ROLL" --duration "$MIN_DURATION" --sudo )
  exit 0
fi

run_make bench-mem-e2e-k1s-sudo "$LABEL_ROOTFUL"
run_make bench-mem-e2e-k1s "$LABEL_ROOTLESS"
# dev-min uses the same rootless path; keep labels aligned for reporting.
run_make bench-mem-e2e-k1s "$LABEL_DEV_MIN"
if command -v docker >/dev/null 2>&1; then
  run_make bench-mem-e2e-k1nd "$LABEL_K1ND"
else
  echo "[bench-suite] docker missing; skipping k1nd stage" >&2
fi

sudo make bench-mem-finalize-sudo
