#!/usr/bin/env bash
set -uo pipefail

# Short benchmark sanity check for rootless, rootful, and k1nd.
# Designed to detect zero metrics or leaked containers without full runtimes.

if [[ "$(id -u)" -eq 0 ]]; then
  echo "[bench-debug] do not run as root; run as your user (sudo used internally for rootful steps)" >&2
  exit 2
fi

RUN_ID="${BENCH_DEBUG_ID:-dbg$(date +%Y%m%d-%H%M%S)}"
repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
DEBUG_ROOT="${BENCH_DEBUG_DIR:-state/bench-debug/$RUN_ID}"
mkdir -p "$DEBUG_ROOT"

log() { echo "[bench-debug] $*" >&2; }

auto_kill_controllers="${BENCH_AUTOKILL_CONTROLLERS:-1}"

kill_controllers() {
  local pids=("$@")
  if (( ${#pids[@]} == 0 )); then
    return 0
  fi
  log "stopping ${#pids[@]} existing controller(s)"
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
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    local pid
    pid="${line%% *}"
    found+=("$pid")
  done < <(pgrep -af "python -m ae.controller" 2>/dev/null || true)

  if (( ${#found[@]} == 0 )); then
    return 0
  fi

  log "detected running controller(s):"
  pgrep -af "python -m ae.controller" >&2 || true

  if [[ "$auto_kill_controllers" == "1" ]]; then
    kill_controllers "${found[@]}"
  else
    echo "[bench-debug] controller(s) detected; set BENCH_AUTOKILL_CONTROLLERS=1 to auto-stop or stop manually." >&2
    return 1
  fi
}

APP="${APP:-specs/examples/echo.yaml}"
APP_NAME="${APP_NAME:-echo}"
DURATION="${DURATION:-10}"
REPLICAS="${REPLICAS:-1}"
WAIT_READY_TRIES="${WAIT_READY_TRIES:-30}"
WAIT_READY_DELAY="${WAIT_READY_DELAY:-1}"
K1ND_DEBUG_KEEP="${K1ND_DEBUG_KEEP:-1}"
bench_specs_minimal="${BENCH_SPECS_MINIMAL:-1}"
bench_specs_empty="${BENCH_SPECS_EMPTY:-1}"

failures=0

export AE_ALLOW_PLAINTEXT_SECRETS="${AE_ALLOW_PLAINTEXT_SECRETS:-1}"
export AE_REGISTER_LOCAL_NODE="${AE_REGISTER_LOCAL_NODE:-1}"
# Prevent local node from going stale during long bench runs.
export AE_NODE_NOTREADY_AFTER="${BENCH_NODE_NOTREADY_AFTER:-${AE_NODE_NOTREADY_AFTER:-600}}"
export AE_RUNTIME_BACKEND="podman"
export AE_APISHIM_RUNTIME="podman"

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
      sub(/port:[[:space:]]*[0-9]+/, "port: " port)
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
  if ! command -v sudo >/dev/null 2>&1 || ! command -v podman >/dev/null 2>&1; then
    return 0
  fi
  if sudo podman info >/dev/null 2>&1; then
    return 0
  fi
  sudo systemctl start podman.socket >/dev/null 2>&1 || true
  sleep 1
  if sudo podman info >/dev/null 2>&1; then
    return 0
  fi
  # Fallback: run a transient rootful service
  sudo podman system service -t 0 >/dev/null 2>&1 &
  sleep 2
  if sudo podman info >/dev/null 2>&1; then
    return 0
  fi
  log "rootful podman socket not available (expected /run/podman/podman.sock)"
  return 1
}

stop_docker_dev_stack() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  local ids
  ids=$(docker ps -aq --filter "name=^dev-controller-1$" --filter "name=^dev-apishim-1$" --filter "name=^dev-caddy-1$" 2>/dev/null | sed '/^$/d' | tr '\n' ' ')
  if [[ -n "$ids" ]]; then
    log "stopping existing dev containers: $ids"
    # shellcheck disable=SC2086
    docker rm -f $ids >/dev/null 2>&1 || true
  fi
}

wait_k1nd_controller_ready() {
  local cid
  cid=$(docker ps -q --filter "name=^dev-controller-1$" 2>/dev/null | head -n1 || true)
  if [[ -z "$cid" ]]; then
    log "k1nd controller container not found"
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
  log "k1nd controller not ready after $((tries * delay))s"
  if command -v docker >/dev/null 2>&1; then
    log "k1nd controller logs (tail 200):"
    docker logs "$cid" --tail 200 2>&1 | sed 's/^/[k1nd-log] /' >&2 || true
  fi
  return 1
}

dump_system() {
  {
    echo "run_id=$RUN_ID"
    date
    uname -a
    id
    echo "python: $(command -v ${PYTHON_BIN:-python} 2>/dev/null || true)"
    python --version 2>/dev/null || true
    podman --version 2>/dev/null || true
    docker --version 2>/dev/null || true
  } > "$DEBUG_ROOT/system.txt" 2>&1
}

dump_containers() {
  local outdir="$1"
  mkdir -p "$outdir"
  podman ps -a --format '{{.ID}} {{.Names}} {{.Status}} {{.Ports}}' >"$outdir/podman_ps.txt" 2>&1 || true
  docker ps -a --format '{{.ID}} {{.Names}} {{.Status}} {{.Ports}}' >"$outdir/docker_ps.txt" 2>&1 || true
  sudo podman ps -a --format '{{.ID}} {{.Names}} {{.Status}} {{.Ports}}' >"$outdir/podman_sudo_ps.txt" 2>&1 || true
  sudo docker ps -a --format '{{.ID}} {{.Names}} {{.Status}} {{.Ports}}' >"$outdir/docker_sudo_ps.txt" 2>&1 || true
  pgrep -af "python -m ae.controller" >"$outdir/controllers.txt" 2>&1 || true
}

clean_rootless() {
  local ids
  ids=$( {
    podman ps -aq --filter label=ae.app 2>/dev/null || true
    podman ps -aq --filter name=ae- 2>/dev/null || true
  } | sed '/^$/d' | sort -u)
  if [[ -n "$ids" ]]; then
    if [[ "${BENCH_AUTOCLEAN_PODMAN:-0}" == "1" ]]; then
      log "cleaning rootless ae.app containers"
      # shellcheck disable=SC2086
      podman rm -f $ids >/dev/null 2>&1 || true
      local left_after
      left_after=$( {
        podman ps -aq --filter label=ae.app 2>/dev/null || true
        podman ps -aq --filter name=ae- 2>/dev/null || true
      } | sed '/^$/d' | sort -u)
      if [[ -n "$left_after" ]]; then
        # shellcheck disable=SC2086
        podman kill -s KILL $left_after >/dev/null 2>&1 || true
        # shellcheck disable=SC2086
        podman rm -f $left_after >/dev/null 2>&1 || true
      fi
    else
      echo "[bench-debug] rootless ae.app containers exist; set BENCH_AUTOCLEAN_PODMAN=1 to auto-remove" >&2
      return 4
    fi
  fi
  local left
  left=$( {
    podman ps -aq --filter label=ae.app 2>/dev/null || true
    podman ps -aq --filter name=ae- 2>/dev/null || true
  } | sed '/^$/d' | sort -u)
  if [[ -n "$left" ]]; then
    echo "[bench-debug] rootless ae.app containers remain: $left" >&2
    return 3
  fi
}

clean_rootful() {
  local ids
  ids=$( {
    sudo podman ps -aq --filter label=ae.app 2>/dev/null || true
    sudo podman ps -aq --filter name=ae- 2>/dev/null || true
  } | sed '/^$/d' | sort -u)
  if [[ -n "$ids" ]]; then
    if [[ "${BENCH_AUTOCLEAN_PODMAN:-0}" == "1" ]]; then
      log "cleaning rootful ae.app containers"
      # shellcheck disable=SC2086
      sudo podman rm -f $ids >/dev/null 2>&1 || true
      local left_after
      left_after=$( {
        sudo podman ps -aq --filter label=ae.app 2>/dev/null || true
        sudo podman ps -aq --filter name=ae- 2>/dev/null || true
      } | sed '/^$/d' | sort -u)
      if [[ -n "$left_after" ]]; then
        # shellcheck disable=SC2086
        sudo podman kill -s KILL $left_after >/dev/null 2>&1 || true
        # shellcheck disable=SC2086
        sudo podman rm -f $left_after >/dev/null 2>&1 || true
      fi
    else
      echo "[bench-debug] rootful ae.app containers exist; set BENCH_AUTOCLEAN_PODMAN=1 to auto-remove" >&2
      return 4
    fi
  fi
  local left
  left=$( {
    sudo podman ps -aq --filter label=ae.app 2>/dev/null || true
    sudo podman ps -aq --filter name=ae- 2>/dev/null || true
  } | sed '/^$/d' | sort -u)
  if [[ -n "$left" ]]; then
    echo "[bench-debug] rootful ae.app containers remain: $left" >&2
    return 3
  fi
}

clean_k1nd() {
  local ids
  ids=$( {
    docker ps -aq --filter label=ae.app 2>/dev/null || true
    docker ps -aq --filter name=ae- 2>/dev/null || true
  } | sed '/^$/d' | sort -u)
  if [[ -n "$ids" ]]; then
    if [[ "${BENCH_AUTOCLEAN_DOCKER:-${BENCH_AUTOCLEAN_PODMAN:-0}}" == "1" ]]; then
      log "cleaning docker ae.app containers"
      # shellcheck disable=SC2086
      docker rm -f $ids >/dev/null 2>&1 || true
      local left_after
      left_after=$( {
        docker ps -aq --filter label=ae.app 2>/dev/null || true
        docker ps -aq --filter name=ae- 2>/dev/null || true
      } | sed '/^$/d' | sort -u)
      if [[ -n "$left_after" ]]; then
        # shellcheck disable=SC2086
        docker kill $left_after >/dev/null 2>&1 || true
        # shellcheck disable=SC2086
        docker rm -f $left_after >/dev/null 2>&1 || true
      fi
    else
      echo "[bench-debug] docker ae.app containers exist; set BENCH_AUTOCLEAN_DOCKER=1 to auto-remove" >&2
      return 4
    fi
  fi
  local left
  left=$( {
    docker ps -aq --filter label=ae.app 2>/dev/null || true
    docker ps -aq --filter name=ae- 2>/dev/null || true
  } | sed '/^$/d' | sort -u)
  if [[ -n "$left" ]]; then
    echo "[bench-debug] docker ae.app containers remain: $left" >&2
    return 3
  fi
}

clean_foreign_for_podman() {
  # For podman snapshots, docker is the "foreign" engine that can contaminate metrics.
  clean_k1nd
}

clean_foreign_for_docker() {
  # For docker snapshots (k1nd), podman is the "foreign" engine.
  clean_rootless || true
  clean_rootful || true
}

summarize_label() {
  local label="$1"
  local stage="$2"
  local out="$3"
  local snap
  snap=$(ls -1d "snapshots/${label}"/* 2>/dev/null | sort | tail -n1 || true)
  if [[ -z "$snap" ]]; then
    echo "missing snapshot for ${label}" | tee -a "$out"
    return 0
  fi
  python - "$snap" "$stage" <<'PY' | tee -a "$out"
import json, os, sys
snap=sys.argv[1]
stage=sys.argv[2]
summary_path=os.path.join(snap, "summary.json")
try:
    data=json.load(open(summary_path, "r", encoding="utf-8"))
except Exception as e:
    print(f"{stage}: failed to read summary.json ({e}); see {snap}/status.log")
    sys.exit(0)
def mib_bytes(v): return float(v or 0)/1024/1024
def mib_kb(v): return float(v or 0)/1024
containers = data.get("containers") or {}
overhead = data.get("overhead") or {}
app=mib_bytes(containers.get("app_mem_bytes"))
over=mib_kb(overhead.get("pss_kb_total_overhead"))
ctrl=mib_kb(overhead.get("pss_kb_controller"))
rt=mib_kb(overhead.get("pss_kb_runtime"))
ing=mib_kb(overhead.get("pss_kb_ingress"))
print(f"{stage}: app_cg={app:.1f}MiB overhead={over:.1f}MiB ctrl={ctrl:.1f}MiB runtime={rt:.1f}MiB ingress={ing:.1f}MiB")
if app <= 0.0:
    print(f"WARN: {stage} app_mem_bytes is 0")
if over <= 0.0:
    print(f"WARN: {stage} overhead_pss_kb_total is 0")
PY
}

report_app_count() {
  local name="$1"
  local count="$2"
  local out="$3"
  echo "${name}: ae.app containers=${count}" | tee -a "$out"
  if [[ "$count" -gt 1 ]]; then
    echo "WARN: ${name} has more than 1 ae.app container (possible leakage)" | tee -a "$out"
  fi
}

dump_system
dump_containers "$DEBUG_ROOT/pre"

if [[ "${BENCH_DEBUG_PRE_CLEAN:-1}" == "1" ]]; then
  log "pre-clean: sanitize engines and labs stack"
  stop_docker_dev_stack || true
  ./scripts/bench/k1nd_sanitize.sh pre >/dev/null 2>&1 || true
  clean_rootless || true
  clean_rootful || true
fi

if ! detect_controllers; then
  failures=$((failures + 1))
  log "controller guard failed; continuing"
fi

log "rootless quick run"
sanitize_env_podman
if ! clean_rootless; then
  failures=$((failures + 1))
  log "rootless pre-clean failed; continuing"
fi
if ! clean_foreign_for_podman; then
  failures=$((failures + 1))
  log "rootless foreign-engine cleanup failed; continuing"
fi
ROOTLESS_DIR="$DEBUG_ROOT/rootless"
ROOTLESS_ENV_FILE="$ROOTLESS_DIR/env.sh"
mkdir -p "$ROOTLESS_DIR"
rootless_ok=1
if ! ROOTLESS_ENV_FILE="$(BENCH_SPECS_MINIMAL="$bench_specs_minimal" BENCH_SPECS_EMPTY="$bench_specs_empty" BENCH_KEEP_ENV=1 ./scripts/bench/bench_env_prep.sh --manifest "$APP" --metrics-port 9210 --env-file "$ROOTLESS_ENV_FILE")"; then
  rootless_ok=0
  failures=$((failures + 1))
  log "rootless bench_env_prep failed; continuing"
fi
if (( rootless_ok )); then
  # shellcheck disable=SC1090
  source "$ROOTLESS_ENV_FILE"
fi
label_rootless="${RUN_ID}+podman+crun+rootless+cg2"
if (( rootless_ok )); then
  if ! AE_ENGINE_STRICT=1 WARM_ENABLED=0 WAIT_READY_TRIES="$WAIT_READY_TRIES" WAIT_READY_DELAY="$WAIT_READY_DELAY" \
    LABEL_SUITE="$label_rootless" \
    APP="$BENCH_PRIMARY_MANIFEST" APP_NAME="$BENCH_PRIMARY_APP" \
    REPLICAS="$REPLICAS" DURATION="$DURATION" \
    AE_SPECS_DIR="$AE_SPECS_DIR" AE_STATE_DB="$AE_STATE_DB" AE_CADDY_DIR="$AE_CADDY_DIR" \
    ./scripts/bench/run_matrix.sh --label-suite "$label_rootless" --app "$APP" --app-name "$APP_NAME" --replicas "$REPLICAS" --duration "$DURATION"; then
    rootless_ok=0
    failures=$((failures + 1))
    log "rootless run_matrix failed; continuing"
  fi
  BENCH_KEEP_ENV=1 ./scripts/bench/bench_env_teardown.sh --env "$ROOTLESS_ENV_FILE" || true
fi
rootless_count=$( {
  podman ps -aq --filter label=ae.app 2>/dev/null || true
  podman ps -aq --filter name=ae- 2>/dev/null || true
} | sed '/^$/d' | sort -u | wc -l | tr -d ' \t' || echo 0)
clean_rootless || true
dump_containers "$ROOTLESS_DIR/post"

log "rootful quick run"
sanitize_env_podman
if ! ensure_rootful_podman_socket; then
  failures=$((failures + 1))
  log "rootful podman socket guard failed; continuing"
fi
if ! clean_rootless; then
  failures=$((failures + 1))
  log "rootful pre-clean(rootless) failed; continuing"
fi
if ! clean_rootful; then
  failures=$((failures + 1))
  log "rootful pre-clean(rootful) failed; continuing"
fi
if ! clean_foreign_for_podman; then
  failures=$((failures + 1))
  log "rootful foreign-engine cleanup failed; continuing"
fi
ROOTFUL_DIR="$DEBUG_ROOT/rootful"
ROOTFUL_ENV_FILE="$ROOTFUL_DIR/env.sh"
mkdir -p "$ROOTFUL_DIR"
rootful_ok=1
if ! ROOTFUL_ENV_FILE="$(BENCH_SPECS_MINIMAL="$bench_specs_minimal" BENCH_SPECS_EMPTY="$bench_specs_empty" BENCH_KEEP_ENV=1 ./scripts/bench/bench_env_prep.sh --manifest "$APP" --metrics-port 9211 --env-file "$ROOTFUL_ENV_FILE" --sudo-controller)"; then
  rootful_ok=0
  failures=$((failures + 1))
  log "rootful bench_env_prep failed; continuing"
fi
if (( rootful_ok )); then
  # shellcheck disable=SC1090
  source "$ROOTFUL_ENV_FILE"
fi
label_rootful="${RUN_ID}+podman+crun+priv+cg2"
if (( rootful_ok )); then
  if ! AE_ENGINE_STRICT=1 AE_COLLECT_PODMAN_SUDO=1 BENCH_CONTROLLER_SUDO=1 \
    WARM_ENABLED=0 WAIT_READY_TRIES="$WAIT_READY_TRIES" WAIT_READY_DELAY="$WAIT_READY_DELAY" \
    LABEL_SUITE="$label_rootful" \
    APP="$BENCH_PRIMARY_MANIFEST" APP_NAME="$BENCH_PRIMARY_APP" \
    REPLICAS="$REPLICAS" DURATION="$DURATION" \
    ./scripts/bench/run_matrix.sh --label-suite "$label_rootful" --app "$APP" --app-name "$APP_NAME" --replicas "$REPLICAS" --duration "$DURATION" --sudo; then
    rootful_ok=0
    failures=$((failures + 1))
    log "rootful run_matrix failed; continuing"
  fi
  BENCH_KEEP_ENV=1 ./scripts/bench/bench_env_teardown.sh --env "$ROOTFUL_ENV_FILE" || true
fi
rootful_count=$( {
  sudo podman ps -aq --filter label=ae.app 2>/dev/null || true
  sudo podman ps -aq --filter name=ae- 2>/dev/null || true
} | sed '/^$/d' | sort -u | wc -l | tr -d ' \t' || echo 0)
clean_rootful
dump_containers "$ROOTFUL_DIR/post"

log "k1nd quick run"
sanitize_env_docker
K1ND_DIR="$DEBUG_ROOT/k1nd"
mkdir -p "$K1ND_DIR"
k1nd_ok=1
if ! clean_foreign_for_docker; then
  failures=$((failures + 1))
  log "k1nd foreign-engine cleanup failed; continuing"
fi
stop_docker_dev_stack || true
if ! ./scripts/bench/k1nd_sanitize.sh pre; then
  k1nd_ok=0
  failures=$((failures + 1))
  log "k1nd pre-sanitize failed; continuing"
fi
k1nd_specs_rel="${BENCH_K1ND_SPECS_DIR:-state/bench-debug/${RUN_ID}/k1nd-specs}"
k1nd_apply_rel="${BENCH_K1ND_APPLY_DIR:-state/bench-debug/${RUN_ID}/k1nd-apply}"
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
  cp -f "$app_src" "$k1nd_apply_abs/$(basename "$app_src")"
  if [[ "$k1nd_specs_empty" != "1" ]]; then
    cp -f "$app_src" "$k1nd_specs_abs/$(basename "$app_src")"
  fi
fi
k1nd_state_rel="${BENCH_K1ND_STATE_DIR:-state/bench-debug/${RUN_ID}/k1nd-state}"
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
    log "k1nd service port removed (ephemeral ports enabled)"
  else
    if port_is_free "$k1nd_port_start"; then
      patch_service_port "$k1nd_manifest" "$k1nd_port_start"
      log "k1nd service port set to ${k1nd_port_start}"
    else
      free_port="$(find_free_port "$k1nd_port_start" "$k1nd_port_end" || true)"
      if [[ -n "$free_port" ]]; then
        patch_service_port "$k1nd_manifest" "$free_port"
        log "k1nd service port set to ${free_port}"
      else
        k1nd_ok=0
        failures=$((failures + 1))
        log "no free port found in ${k1nd_port_start}-${k1nd_port_end} for k1nd service"
      fi
    fi
  fi
fi

if ! AE_SPECS_DIR="$k1nd_specs_rel" \
  AE_APISHIM_DSN="${k1nd_state_rel}/apishim.db" \
  AE_STATE_DB="${k1nd_state_db}" \
  make labs-aio-up; then
  k1nd_ok=0
  failures=$((failures + 1))
  log "k1nd labs-aio-up failed; continuing"
fi
label_k1nd="${RUN_ID}+docker+runc+k1nd"
if (( k1nd_ok )); then
  if ! wait_k1nd_controller_ready; then
    k1nd_ok=0
    failures=$((failures + 1))
    log "k1nd controller readiness check failed; continuing"
  fi
fi
if (( k1nd_ok )); then
  if ! AE_RUNTIME_BACKEND=docker AE_APISHIM_RUNTIME=docker AE_CLI_IN_CONTAINER=1 \
    AE_COLLECT_ENGINE=docker AE_ENGINE_STRICT=1 AE_SERIAL_SERVICE_ROLLOUT=1 \
    AE_STATE_DB="/tmp/k1s-bench-${USER}-debug.db" SKIP_GUARDS=1 \
    AE_APISHIM_DSN="${k1nd_state_rel}/apishim.db" AE_STATE_DB="${k1nd_state_db}" \
    WARM_ENABLED=0 WAIT_READY_TRIES="$WAIT_READY_TRIES" WAIT_READY_DELAY="$WAIT_READY_DELAY" \
    ./scripts/bench/run_matrix.sh --label-suite "$label_k1nd" --app "$k1nd_manifest_rel" --app-name "$APP_NAME" --replicas "$REPLICAS" --duration "$DURATION" --sudo; then
    k1nd_ok=0
    failures=$((failures + 1))
    log "k1nd run_matrix failed; continuing"
  fi
fi
docker_count=$( {
  docker ps -aq --filter label=ae.app 2>/dev/null || true
  docker ps -aq --filter name=ae- 2>/dev/null || true
} | sed '/^$/d' | sort -u | wc -l | tr -d ' \t' || echo 0)
if [[ "$K1ND_DEBUG_KEEP" != "1" ]]; then
  ./scripts/bench/k1nd_sanitize.sh post || true
else
  log "skipping k1nd teardown (K1ND_DEBUG_KEEP=1)"
fi
dump_containers "$K1ND_DIR/post"
docker inspect dev-controller-1 --format '{{range .Config.Env}}{{println .}}{{end}}' >"$K1ND_DIR/controller_env.txt" 2>&1 || true
docker logs dev-controller-1 --tail 200 >"$K1ND_DIR/controller.log" 2>&1 || true
docker logs dev-apishim-1 --tail 200 >"$K1ND_DIR/apishim.log" 2>&1 || true
docker logs dev-caddy-1 --tail 200 >"$K1ND_DIR/caddy.log" 2>&1 || true

REPORT="$DEBUG_ROOT/report.txt"
: > "$REPORT"
echo "bench-debug report: $RUN_ID" | tee -a "$REPORT"
echo "rootless: $label_rootless" | tee -a "$REPORT"
summarize_label "${label_rootless}-idle" "rootless-idle" "$REPORT"
summarize_label "${label_rootless}-pods-1" "rootless-pods-1" "$REPORT"
report_app_count "rootless" "${rootless_count:-0}" "$REPORT"
echo "rootful: $label_rootful" | tee -a "$REPORT"
summarize_label "${label_rootful}-idle" "rootful-idle" "$REPORT"
summarize_label "${label_rootful}-pods-1" "rootful-pods-1" "$REPORT"
report_app_count "rootful" "${rootful_count:-0}" "$REPORT"
echo "k1nd: $label_k1nd" | tee -a "$REPORT"
summarize_label "${label_k1nd}-idle" "k1nd-idle" "$REPORT"
summarize_label "${label_k1nd}-pods-1" "k1nd-pods-1" "$REPORT"
report_app_count "k1nd" "${docker_count:-0}" "$REPORT"

log "debug artifacts: $DEBUG_ROOT"

if (( failures > 0 )); then
  echo "[bench-debug] completed with ${failures} failure(s)" >&2
  exit 1
fi
