#!/usr/bin/env bash
set -euo pipefail

# Run only the rootful Podman benchmark stage (no rootless/k1nd).
# Mirrors the rootful block from run_full_refresh.sh.

if [[ "$(id -u)" -eq 0 ]]; then
  echo "[rootful-only] do not run as root; run as your user (sudo is used internally)" >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

APP="${APP:-specs/examples/echo.yaml}"
APP_NAME="${APP_NAME:-echo}"
REPLICAS="${REPLICAS:-1,5,10}"
ROLL_REPLICAS="${ROLL_REPLICAS:-2,5}"
DURATION="${DURATION:-30}"
bench_specs_minimal="${BENCH_SPECS_MINIMAL:-1}"
bench_specs_empty="${BENCH_SPECS_EMPTY:-1}"

# Prefer repo virtualenv if present
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$PWD/.venv-demo/bin/python" ]]; then
    export PYTHON_BIN="$PWD/.venv-demo/bin/python"
    export PATH="$PWD/.venv-demo/bin:$PATH"
  elif [[ -x "$PWD/.venv/bin/python" ]]; then
    export PYTHON_BIN="$PWD/.venv/bin/python"
    export PATH="$PWD/.venv/bin:$PATH"
  fi
fi

unset DOCKER_HOST CONTAINER_HOST PODMAN_HOST
export AE_RUNTIME_BACKEND="podman"
export AE_APISHIM_RUNTIME="podman"
export AE_ALLOW_PLAINTEXT_SECRETS=1
export AE_REGISTER_LOCAL_NODE=1
export AE_NODE_NOTREADY_AFTER="${BENCH_NODE_NOTREADY_AFTER:-${AE_NODE_NOTREADY_AFTER:-600}}"

./scripts/bench/podman_rootful_socket.sh

ENV_FILE="$(BENCH_SPECS_MINIMAL="$bench_specs_minimal" BENCH_SPECS_EMPTY="$bench_specs_empty" \
  ./scripts/bench/bench_env_prep.sh --manifest "$APP" --metrics-port 9211 --sudo-controller)"

# shellcheck disable=SC1090
source "$ENV_FILE"

AE_ENGINE_STRICT=1 AE_COLLECT_PODMAN_SUDO=1 BENCH_CONTROLLER_SUDO=1 \
LABEL_SUITE="r$(date +%Y%m%d)+podman+crun+priv+cg2" \
APP="$BENCH_PRIMARY_MANIFEST" APP_NAME="$BENCH_PRIMARY_APP" \
REPLICAS="$REPLICAS" ROLL_REPLICAS="$ROLL_REPLICAS" DURATION="$DURATION" \
make bench-mem-e2e-k1s-sudo

./scripts/bench/bench_env_teardown.sh --env "$ENV_FILE"
