#!/usr/bin/env bash
set -euo pipefail

# Refresh k1s rootless + rootful + k1nd benchmarks, then rebuild charts/docs.

if [[ "$(id -u)" -eq 0 ]]; then
  echo "[full-refresh] do not run as root; run as your user (sudo is used internally for rootful steps)" >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
bench_specs_abs="$repo_root/state/bench-env/specs"
bench_specs_rel="state/bench-env/specs"
auto_kill_controllers="${BENCH_AUTOKILL_CONTROLLERS:-1}"

sanitize_env_podman() {
  unset DOCKER_HOST CONTAINER_HOST PODMAN_HOST
  export AE_RUNTIME_BACKEND="podman"
  export AE_APISHIM_RUNTIME="podman"
}

sanitize_env_docker() {
  unset PODMAN_HOST
  unset AE_STATE_DSN
  export AE_RUNTIME_BACKEND="docker"
  export AE_APISHIM_RUNTIME="docker"
}

port_is_free() {
  local port="$1"
  python - "$port" <<'PY' >/dev/null 2>&1
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
try:
    s.bind(("0.0.0.0", port))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

find_free_port() {
  local start="${1:-18080}"
  local end="${2:-18180}"
  local p
  for p in $(seq "$start" "$end"); do
    if port_is_free "$p"; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

patch_service_port() {
  local manifest="$1"
  local new_port="$2"
  local tmp="${manifest}.tmp"
  awk -v port="$new_port" '
  function indent(line) { match(line, /^[ ]*/); return RLENGTH }
  {
    if ($0 ~ /^[[:space:]]*service:[[:space:]]*$/) {
      in_service=1; svc_indent=indent($0); print; next
    }
    if (in_service) {
      cur_indent=indent($0)
      if (cur_indent <= svc_indent) { in_service=0 }
    }
    if (in_service && $0 ~ /^[[:space:]]*port:[[:space:]]*[0-9]+([[:space:]]*#.*)?$/) {
      sub(/port:[[:space:]]*[0-9]+/, \"port: \" port)
      in_service=0
    }
    print
  }' "$manifest" >"$tmp" && mv "$tmp" "$manifest"
}

remove_service_port() {
  local manifest="$1"
  local tmp="${manifest}.tmp"
  awk '
  function indent(line) { match(line, /^[ ]*/); return RLENGTH }
  {
    if ($0 ~ /^[[:space:]]*service:[[:space:]]*$/) {
      in_service=1; svc_indent=indent($0); print; next
    }
    if (in_service) {
      cur_indent=indent($0)
      if (cur_indent <= svc_indent) { in_service=0 }
    }
    if (in_service && $0 ~ /^[[:space:]]*port:[[:space:]]*[0-9]+([[:space:]]*#.*)?$/) {
      next
    }
    print
  }' "$manifest" >"$tmp" && mv "$tmp" "$manifest"
}

ensure_rootful_podman_socket() {
  "$repo_root/scripts/bench/podman_rootful_socket.sh" || {
    echo "[full-refresh] rootful podman socket not available (expected /run/podman/podman.sock)" >&2
    return 1
  }
}

stop_docker_dev_stack() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  local ids
  ids=$(docker ps -aq --filter "name=^dev-controller-1$" --filter "name=^dev-apishim-1$" --filter "name=^dev-caddy-1$" 2>/dev/null | sed '/^$/d' | tr '\n' ' ')
  if [[ -n "$ids" ]]; then
    echo "[full-refresh] stopping existing dev containers: $ids" >&2
    # shellcheck disable=SC2086
    docker rm -f $ids >/dev/null 2>&1 || true
  fi
}

ensure_demo_images() {
  local blue="$1"
  local green="$2"
  shift 2
  local runner=("$@")
  if [[ ${#runner[@]} -eq 0 ]]; then
    return 0
  fi
  local images
  images="$("${runner[@]}" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || true)"
  if ! grep -q "^${blue}$" <<<"$images"; then
    echo "[full-refresh] building ${blue}" >&2
    "${runner[@]}" build -t "${blue}" "${repo_root}/samples/servers/blue" >/dev/null 2>&1 || true
  fi
  if ! grep -q "^${green}$" <<<"$images"; then
    echo "[full-refresh] building ${green}" >&2
    "${runner[@]}" build -t "${green}" "${repo_root}/samples/servers/green" >/dev/null 2>&1 || true
  fi
}

clean_podman_rootless() {
  if ! command -v podman >/dev/null 2>&1; then
    return 0
  fi
  local ids
  ids="$(podman ps -aq 2>/dev/null || true)"
  if [[ -n "${ids}" ]]; then
    podman rm -f ${ids} >/dev/null 2>&1 || true
    podman pod rm -fa >/dev/null 2>&1 || true
  fi
}

clean_podman_rootful() {
  if ! command -v sudo >/dev/null 2>&1 || ! command -v podman >/dev/null 2>&1; then
    return 0
  fi
  local ids
  ids="$(sudo podman ps -aq 2>/dev/null || true)"
  if [[ -n "${ids}" ]]; then
    sudo podman rm -f ${ids} >/dev/null 2>&1 || true
    sudo podman pod rm -fa >/dev/null 2>&1 || true
  fi
}

wait_k1nd_controller_ready() {
  local cid
  cid=$(docker ps -q --filter "name=^dev-controller-1$" 2>/dev/null | head -n1 || true)
  if [[ -z "$cid" ]]; then
    echo "[full-refresh] k1nd controller container not found" >&2
    return 1
  fi
  local tries=${K1ND_WAIT_TRIES:-30}
  local delay=${K1ND_WAIT_DELAY:-2}
  for _ in $(seq 1 "$tries"); do
    if docker exec "$cid" python -m ae.cli --help >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done
  echo "[full-refresh] k1nd controller not ready after $((tries * delay))s" >&2
  if command -v docker >/dev/null 2>&1; then
    echo "[full-refresh] k1nd controller logs (tail 200):" >&2
    docker logs "$cid" --tail 200 2>&1 | sed 's/^/[k1nd-log] /' >&2 || true
  fi
  return 1
}

kill_controllers() {
  local pids=("$@")
  if (( ${#pids[@]} == 0 )); then
    return 0
  fi
  echo "[full-refresh] stopping ${#pids[@]} existing controller(s)" >&2
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || sudo kill "$pid" >/dev/null 2>&1 || true
    fi
  done
  sleep 2
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      kill -9 "$pid" >/dev/null 2>&1 || sudo kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  done
}

detect_controllers() {
  local found=()
  local nonbench=()
  local bench=()
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local pid cmd
    pid="${line%% *}"
    cmd="${line#* }"
    found+=("$pid")
    if [[ "$cmd" == *"$bench_specs_abs"* || "$cmd" == *"$bench_specs_rel"* ]]; then
      bench+=("$pid")
    else
      nonbench+=("$pid")
    fi
  done < <(pgrep -af "python -m ae.controller" 2>/dev/null || true)

  if (( ${#found[@]} == 0 )); then
    return 0
  fi

  echo "[full-refresh] detected running controller(s):" >&2
  pgrep -af "python -m ae.controller" >&2 || true

  if (( ${#nonbench[@]} > 0 )); then
    if [[ "$auto_kill_controllers" == "1" ]]; then
      kill_controllers "${found[@]}"
    else
      echo "[full-refresh] non-bench controller(s) detected; set BENCH_AUTOKILL_CONTROLLERS=1 to auto-stop or stop manually." >&2
      exit 4
    fi
  else
    # Only bench controllers detected; stop them to ensure a clean run.
    kill_controllers "${bench[@]}"
  fi
}

detect_controllers

sanitize_env_podman

if [[ "${BENCH_PRE_CLEAN:-1}" == "1" ]]; then
  echo "[full-refresh] pre-clean: sanitize engines and labs stack" >&2
  stop_docker_dev_stack || true
  ./scripts/bench/k1nd_sanitize.sh pre >/dev/null 2>&1 || true
fi

DATE="r$(date +%Y%m%d)"
DURATION=30
REPLICAS=1,5,10
ROLL_REPLICAS=5
APP=specs/examples/echo.yaml
APP_NAME=echo
bench_specs_minimal="${BENCH_SPECS_MINIMAL:-1}"
bench_specs_empty="${BENCH_SPECS_EMPTY:-1}"

# Prefer repo virtualenv if present so grpc is available for controller auto-start.
if [[ -z "${PYTHON_BIN:-}" ]]; then
  if [[ -x "$PWD/.venv-demo/bin/python" ]]; then
    export PYTHON_BIN="$PWD/.venv-demo/bin/python"
    export PATH="$PWD/.venv-demo/bin:$PATH"
  elif [[ -x "$PWD/.venv/bin/python" ]]; then
    export PYTHON_BIN="$PWD/.venv/bin/python"
    export PATH="$PWD/.venv/bin:$PATH"
  fi
fi

# Ensure local node is registered so pods can schedule.
export AE_REGISTER_LOCAL_NODE=1
# Prevent local node from going stale during long bench runs.
export AE_NODE_NOTREADY_AFTER="${BENCH_NODE_NOTREADY_AFTER:-${AE_NODE_NOTREADY_AFTER:-600}}"
# Bench runs typically use plaintext demo secrets.
export AE_ALLOW_PLAINTEXT_SECRETS=1

# Rootless run in an isolated sandbox to avoid readonly state DB.
ROOTLESS_ENV_FILE="$(BENCH_SPECS_MINIMAL="$bench_specs_minimal" BENCH_SPECS_EMPTY="$bench_specs_empty" ./scripts/bench/bench_env_prep.sh --manifest "$APP" --metrics-port 9210)"
# shellcheck disable=SC1090
source "$ROOTLESS_ENV_FILE"

ensure_demo_images "localhost/demo-blue:latest" "localhost/demo-green:latest" podman

AE_ENGINE_STRICT=1 \
LABEL_SUITE="${DATE}+podman+crun+rootless+cg2" \
APP="$BENCH_PRIMARY_MANIFEST" APP_NAME="$BENCH_PRIMARY_APP" \
REPLICAS="$REPLICAS" ROLL_REPLICAS="$ROLL_REPLICAS" DURATION="$DURATION" \
AE_SPECS_DIR="$AE_SPECS_DIR" AE_STATE_DB="$AE_STATE_DB" AE_CADDY_DIR="$AE_CADDY_DIR" \
make bench-mem-e2e-k1s

./scripts/bench/bench_env_teardown.sh --env "$ROOTLESS_ENV_FILE"

# Clean up any rootless podman containers before rootful run (engines_clear will fail otherwise).
if command -v podman >/dev/null 2>&1; then
  ids="$(podman ps -aq 2>/dev/null || true)"
  if [[ -n "${ids}" ]]; then
    podman rm -f ${ids} >/dev/null 2>&1 || true
    podman pod rm -fa >/dev/null 2>&1 || true
  fi
  left="$(podman ps -aq 2>/dev/null | wc -l | tr -d ' \t' || echo 0)"
  if [[ "${left}" != "0" ]]; then
    echo "[full-refresh] rootless podman still has ${left} container(s); clear them before rootful run." >&2
    podman ps -a --format '{{.ID}} {{.Names}} {{.Status}}' >&2 || true
    exit 4
  fi
fi

# Start a rootful controller (via bench env) so rootful Podman containers are created.
sanitize_env_podman
if ! ensure_rootful_podman_socket; then
  exit 4
fi
ENV_FILE="$(BENCH_SPECS_MINIMAL="$bench_specs_minimal" BENCH_SPECS_EMPTY="$bench_specs_empty" ./scripts/bench/bench_env_prep.sh --manifest "$APP" --metrics-port 9211 --sudo-controller)"
# shellcheck disable=SC1090
source "$ENV_FILE"

ensure_demo_images "localhost/demo-blue:latest" "localhost/demo-green:latest" sudo podman

# Fail fast if rootful Podman isn't creating app containers (skip when specs dir is empty).
if [[ "$bench_specs_empty" != "1" ]]; then
  sleep 5
  if ! sudo podman ps -a --filter label=ae.app --format '{{.Names}}' | grep -q '.'; then
    echo "[full-refresh] rootful podman has no ae.app containers; check $BENCH_CONTROLLER_LOG" >&2
    ./scripts/bench/bench_env_teardown.sh --env "$ENV_FILE"
    exit 4
  fi
else
  echo "[full-refresh] skipping rootful ae.app precheck (BENCH_SPECS_EMPTY=1)" >&2
fi

AE_ENGINE_STRICT=1 AE_COLLECT_PODMAN_SUDO=1 BENCH_CONTROLLER_SUDO=1 \
LABEL_SUITE="${DATE}+podman+crun+priv+cg2" \
APP="$BENCH_PRIMARY_MANIFEST" APP_NAME="$BENCH_PRIMARY_APP" \
REPLICAS="$REPLICAS" ROLL_REPLICAS="$ROLL_REPLICAS" DURATION="$DURATION" \
make bench-mem-e2e-k1s-sudo

./scripts/bench/bench_env_teardown.sh --env "$ENV_FILE"

# Clear Podman before k1nd to avoid foreign-engine contamination.
clean_podman_rootless
clean_podman_rootful

export AE_RUNTIME_BACKEND=docker
export AE_APISHIM_RUNTIME=docker

sanitize_env_docker
stop_docker_dev_stack || true
scripts/bench/k1nd_sanitize.sh pre

ensure_demo_images "demo-blue:latest" "demo-green:latest" docker
k1nd_specs_rel="${BENCH_K1ND_SPECS_DIR:-state/bench-k1nd-specs}"
k1nd_apply_rel="${BENCH_K1ND_APPLY_DIR:-state/bench-k1nd-apply}"
k1nd_specs_empty="${BENCH_K1ND_SPECS_EMPTY:-$bench_specs_empty}"
k1nd_specs_abs="${repo_root}/${k1nd_specs_rel}"
rm -rf "$k1nd_specs_abs"
mkdir -p "$k1nd_specs_abs"
k1nd_apply_abs="${repo_root}/${k1nd_apply_rel}"
rm -rf "$k1nd_apply_abs"
mkdir -p "$k1nd_apply_abs"
app_src="$APP"
if [[ "$app_src" != /* ]]; then
  app_src="${repo_root}/${app_src}"
fi
if [[ -f "$app_src" ]]; then
  # Always copy a standalone manifest for apply/scale operations.
  cp -f "$app_src" "$k1nd_apply_abs/$(basename "$app_src")"
  # Optionally keep the controller spec dir empty to avoid file reconcile resets.
  if [[ "$k1nd_specs_empty" != "1" ]]; then
    cp -f "$app_src" "$k1nd_specs_abs/$(basename "$app_src")"
  fi
fi
k1nd_state_rel="${BENCH_K1ND_STATE_DIR:-state/bench-k1nd-state}"
k1nd_state_abs="${repo_root}/${k1nd_state_rel}"
rm -rf "$k1nd_state_abs"
mkdir -p "$k1nd_state_abs"
k1nd_state_db="${k1nd_state_rel}/controller.db"
k1nd_manifest_rel="${k1nd_apply_rel}/$(basename "$app_src")"
k1nd_manifest="${k1nd_apply_abs}/$(basename "$app_src")"
k1nd_port_start="${BENCH_K1ND_PORT_START:-18080}"
k1nd_port_end="${BENCH_K1ND_PORT_END:-18180}"
if [[ -f "$k1nd_manifest" ]]; then
  if [[ "${BENCH_K1ND_EPHEMERAL_PORTS:-0}" == "1" ]]; then
    remove_service_port "$k1nd_manifest"
    echo "[full-refresh] k1nd service port removed (ephemeral ports enabled)" >&2
  else
    if port_is_free "$k1nd_port_start"; then
      patch_service_port "$k1nd_manifest" "$k1nd_port_start"
      echo "[full-refresh] k1nd service port set to ${k1nd_port_start}" >&2
    else
      free_port="$(find_free_port "$k1nd_port_start" "$k1nd_port_end" || true)"
      if [[ -n "$free_port" ]]; then
        patch_service_port "$k1nd_manifest" "$free_port"
        echo "[full-refresh] k1nd service port set to ${free_port}" >&2
      else
        echo "[full-refresh] no free port found in ${k1nd_port_start}-${k1nd_port_end} for k1nd service" >&2
        exit 4
      fi
    fi
  fi
fi
AE_SPECS_DIR="$k1nd_specs_rel" \
AE_APISHIM_DSN="${k1nd_state_rel}/apishim.db" \
AE_STATE_DB="${k1nd_state_db}" \
make labs-aio-up
if ! wait_k1nd_controller_ready; then
  echo "[full-refresh] k1nd controller not ready; check docker logs dev-controller-1" >&2
fi
AE_CLI_IN_CONTAINER=1 AE_COLLECT_ENGINE=docker AE_ENGINE_STRICT=1 AE_SERIAL_SERVICE_ROLLOUT=1 \
AE_STATE_DB="/tmp/k1s-bench-$(id -un).db" SKIP_GUARDS=1 \
AE_SPECS_DIR="$k1nd_specs_rel" \
AE_APISHIM_DSN="${k1nd_state_rel}/apishim.db" AE_STATE_DB="${k1nd_state_db}" \
LABEL_SUITE="${DATE}+docker+runc+k1nd" \
APP="$k1nd_manifest_rel" APP_NAME="$APP_NAME" \
REPLICAS="$REPLICAS" DURATION="$DURATION" \
bash ./scripts/bench/run_matrix.sh \
  --label-suite "${DATE}+docker+runc+k1nd" \
  --app "$k1nd_manifest_rel" \
  --app-name "$APP_NAME" \
  --replicas "$REPLICAS" \
  --duration "$DURATION" \
  --sudo
AE_CLI_IN_CONTAINER=1 AE_COLLECT_ENGINE=docker AE_ENGINE_STRICT=1 AE_SERIAL_SERVICE_ROLLOUT=1 \
AE_STATE_DB="/tmp/k1s-bench-$(id -un).db" SKIP_GUARDS=1 \
AE_SPECS_DIR="$k1nd_specs_rel" \
AE_APISHIM_DSN="${k1nd_state_rel}/apishim.db" AE_STATE_DB="${k1nd_state_db}" \
LABEL_SUITE_ROLL="${DATE}+docker+runc+k1nd" \
APP="$k1nd_manifest_rel" APP_NAME="$APP_NAME" \
ROLL_REPLICAS="$ROLL_REPLICAS" DURATION="$DURATION" \
bash ./scripts/bench/run_rollout_k1s.sh \
  --label-suite "${DATE}+docker+runc+k1nd" \
  --app "$k1nd_manifest_rel" \
  --app-name "$APP_NAME" \
  --replicas "$ROLL_REPLICAS" \
  --duration "$DURATION" \
  --sudo
scripts/bench/k1nd_sanitize.sh post

make bench-mem-docs
