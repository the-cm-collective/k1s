#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT_DIR"

lane=""
label_base=""
experiment_id=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lane) lane="$2"; shift 2;;
    --label-base) label_base="$2"; shift 2;;
    --experiment-id) experiment_id="$2"; shift 2;;
    -h|--help)
      cat <<'USAGE'
Usage: scripts/bench/run_rollout_tuning_experiment.sh --lane <cri|rootless|rootful|k1nd> [--label-base LABEL] [--experiment-id ID]

Environment:
  APP                              source manifest (default: specs/examples/echo.yaml)
  APP_NAME                         app name (default: echo)
  REPLICAS                         steady-state replicas CSV (default: 1,5,10)
  ROLL_REPLICAS                    rollout replicas CSV (default: 2,5)
  DURATION                         snapshot duration seconds (default: 30)
  BENCH_EXPERIMENT_ROLLOUT_STRATEGY
                                   optional rollout strategy override (ordered|parallel)
  BENCH_EXPERIMENT_STEADY_QUIET    enable steady quiet hooks (default: 0)
  EXPERIMENT_STEADY_TIMEOUT        quiet helper timeout seconds (default: 20)
  EXPERIMENT_STEADY_DELAY          quiet helper delay seconds (default: 2)
  EXPERIMENT_STEADY_POLLS          quiet helper stable polls (default: 3)
  AE_CRI_RUNTIME_HANDLER           runtime class for CRI experiments (default: runc)
USAGE
      exit 0
      ;;
    *)
      echo "[bench-exp] unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

log() { echo "[bench-exp] $*" >&2; }
have() { command -v "$1" >/dev/null 2>&1; }

require_lane() {
  case "$lane" in
    cri|rootless|rootful|k1nd) ;;
    *)
      echo "[bench-exp] --lane must be one of: cri, rootless, rootful, k1nd" >&2
      exit 2
      ;;
  esac
}

APP=${APP:-specs/examples/echo.yaml}
APP_NAME=${APP_NAME:-echo}
REPLICAS=${REPLICAS:-1,5,10}
ROLL_REPLICAS=${ROLL_REPLICAS:-2,5}
DURATION=${DURATION:-30}
WAIT_READY_TRIES=${WAIT_READY_TRIES:-300}
BENCH_EXPERIMENT_STEADY_QUIET=${BENCH_EXPERIMENT_STEADY_QUIET:-0}
BENCH_EXPERIMENT_ROLLOUT_STRATEGY=${BENCH_EXPERIMENT_ROLLOUT_STRATEGY:-}
EXPERIMENT_STEADY_TIMEOUT=${EXPERIMENT_STEADY_TIMEOUT:-20}
EXPERIMENT_STEADY_DELAY=${EXPERIMENT_STEADY_DELAY:-2}
EXPERIMENT_STEADY_POLLS=${EXPERIMENT_STEADY_POLLS:-3}
BENCH_SPECS_MINIMAL=${BENCH_SPECS_MINIMAL:-1}
BENCH_SPECS_EMPTY=${BENCH_SPECS_EMPTY:-1}

python_bin="${PYTHON_BIN:-python}"
py_path="${PYTHONPATH:-$ROOT_DIR/src}"

require_lane
have "$python_bin" || { echo "[bench-exp] missing python" >&2; exit 2; }

ensure_exp_label() {
  local raw="$1"
  if [[ -z "$raw" ]]; then
    raw="r$(date +%Y%m%d-%H%M%S)+exp+${lane}"
  fi
  if [[ "$raw" != *"+exp+"* ]]; then
    raw="${raw}+exp+${lane}"
  fi
  printf '%s\n' "$raw"
}

label_base="$(ensure_exp_label "$label_base")"
if [[ -z "$experiment_id" ]]; then
  experiment_id="${label_base//[^A-Za-z0-9._+-]/-}"
fi
experiment_root="${BENCH_EXPERIMENT_OUTPUT_ROOT:-state/bench-experiments/${experiment_id}}"
experiment_snapshots_dir="${experiment_root}/snapshots"
experiment_combined_dir="${experiment_root}/combined"
experiment_charts_dir="${experiment_root}/charts"
experiment_apply_dir="${experiment_root}/apply"
experiment_reports_dir="${experiment_root}/reports"

if [[ -e "$experiment_root" ]]; then
  echo "[bench-exp] experiment root already exists: $experiment_root" >&2
  exit 2
fi
mkdir -p "$experiment_snapshots_dir" "$experiment_combined_dir" "$experiment_charts_dir" "$experiment_apply_dir" "$experiment_reports_dir"

stop_controller() {
  pkill -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1 || true
  if have sudo; then
    sudo pkill -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1 || true
  fi
}

clear_rootless_podman() {
  have podman || return 0
  local ids
  ids=$(podman ps -aq 2>/dev/null || true)
  if [[ -n "$ids" ]]; then
    log "rootless podman: removing containers"
    podman rm -f $ids >/dev/null 2>&1 || true
  fi
}

clear_rootful_podman() {
  if ! have sudo; then
    return 0
  fi
  if sudo -n true >/dev/null 2>&1 && sudo bash -lc 'command -v podman >/dev/null 2>&1'; then
    local ids
    ids=$(sudo podman ps -aq 2>/dev/null || true)
    if [[ -n "$ids" ]]; then
      log "rootful podman: removing containers"
      sudo podman rm -f $ids >/dev/null 2>&1 || true
    fi
  fi
}

clear_docker_all() {
  have docker || return 0
  local ids
  ids=$(docker ps -aq 2>/dev/null || true)
  if [[ -n "$ids" ]]; then
    log "docker: removing containers"
    docker rm -f $ids >/dev/null 2>&1 || true
  fi
}

engines_clear_all() {
  stop_controller || true
  clear_rootless_podman || true
  clear_docker_all || true
  clear_rootful_podman || true
  if have sudo && sudo -n true >/dev/null 2>&1 && have make; then
    sudo make bench-engines-clear CONFIRM=1 >/dev/null 2>&1 || true
  fi
}

fix_perms() {
  if have sudo && sudo -n true >/dev/null 2>&1 && have make; then
    sudo make bench-fix-perms >/dev/null 2>&1 || true
  fi
}

start_caddy_only() {
  have docker || return 0
  local dock="docker"
  if ! docker ps >/dev/null 2>&1 && have sudo && sudo -n true >/dev/null 2>&1; then
    dock="sudo docker"
  fi
  $dock rm -f caddy-bench >/dev/null 2>&1 || true
  log "starting caddy"
  $dock run -d --name caddy-bench \
    -p "${CADDY_HTTP_PORT:-8888}:80" \
    -p "${CADDY_HTTPS_PORT:-8443}:443" \
    -v "$ROOT_DIR/ops/dev/caddy:/etc/caddy:ro" \
    -v "$ROOT_DIR/state/caddy-data:/data" \
    -v "$ROOT_DIR/state/caddy:/etc/caddy/dynsites:ro" \
    -v "$ROOT_DIR/docs/site:/srv/docs:ro" \
    --restart unless-stopped \
    caddy:2.8 >/dev/null 2>&1 || true
}

stop_caddy_only() {
  have docker || return 0
  local dock="docker"
  if ! docker ps >/dev/null 2>&1 && have sudo && sudo -n true >/dev/null 2>&1; then
    dock="sudo docker"
  fi
  $dock rm -f caddy-bench >/dev/null 2>&1 || true
}

build_demo_images_podman() {
  local sudo_flag="$1"
  have podman || return 0
  local runner=(podman)
  if [[ "$sudo_flag" == "1" ]]; then
    runner=(sudo podman)
  fi
  if ! "${runner[@]}" images --format '{{.Repository}}:{{.Tag}}' | grep -q '^demo-blue:latest$'; then
    log "building demo-blue:latest"
    "${runner[@]}" build -t demo-blue:latest samples/servers/blue >/dev/null 2>&1 || true
  fi
  if ! "${runner[@]}" images --format '{{.Repository}}:{{.Tag}}' | grep -q '^demo-green:latest$'; then
    log "building demo-green:latest"
    "${runner[@]}" build -t demo-green:latest samples/servers/green >/dev/null 2>&1 || true
  fi
}

build_bench_manifest() {
  local source="$1"
  local lane_name="$2"
  local current="$source"
  local next=""

  if [[ "$lane_name" == "cri" ]]; then
    next="${experiment_apply_dir}/$(basename "${source%.yaml}")-runc.yaml"
    "$python_bin" scripts/bench/pin_runtime_class.py \
      "$current" \
      "$next" \
      --runtime-class "${AE_CRI_RUNTIME_HANDLER:-runc}" >/dev/null
    current="$next"
  fi

  if [[ -n "$BENCH_EXPERIMENT_ROLLOUT_STRATEGY" ]]; then
    next="${experiment_apply_dir}/$(basename "${source%.yaml}")-${BENCH_EXPERIMENT_ROLLOUT_STRATEGY}.yaml"
    "$python_bin" scripts/bench/pin_rollout_policy.py \
      "$current" \
      "$next" \
      --strategy "$BENCH_EXPERIMENT_ROLLOUT_STRATEGY" >/dev/null
    current="$next"
  fi

  printf '%s\n' "$current"
}

build_steady_cmd() {
  local backend="$1"
  local app="$2"
  local use_sudo="$3"
  local -a cmd=(
    env
    "PATH=${PATH}"
    "PYTHONPATH=${py_path}"
    "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}"
    "NIX_LD_LIBRARY_PATH=${NIX_LD_LIBRARY_PATH:-}"
    "NIX_LD=${NIX_LD:-}"
    "AE_PODMAN_BIN=${AE_PODMAN_BIN:-podman}"
    "AE_CRI_ENDPOINT=${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"
    "$python_bin"
    scripts/bench/wait_rollout_steady.py
    --backend "$backend"
    --app "$app"
    --timeout "$EXPERIMENT_STEADY_TIMEOUT"
    --delay "$EXPERIMENT_STEADY_DELAY"
    --stable-polls "$EXPERIMENT_STEADY_POLLS"
    --require-app-present
  )
  if [[ "$use_sudo" == "1" ]]; then
    cmd=(sudo "${cmd[@]}")
  fi
  printf '%q ' "${cmd[@]}"
}

move_label_dirs() {
  shopt -s nullglob
  local dirs=(snapshots/"${label_base}"-*)
  shopt -u nullglob
  if (( ${#dirs[@]} == 0 )); then
    echo "[bench-exp] no snapshot dirs found for ${label_base}" >&2
    return 1
  fi
  mkdir -p "$experiment_snapshots_dir"
  local dir
  for dir in "${dirs[@]}"; do
    mv "$dir" "$experiment_snapshots_dir/"
  done
}

combine_and_plot_experiment() {
  shopt -s nullglob
  local snapshot_dirs=("$experiment_snapshots_dir"/*)
  shopt -u nullglob
  if (( ${#snapshot_dirs[@]} == 0 )); then
    echo "[bench-exp] experiment snapshot dir is empty: $experiment_snapshots_dir" >&2
    return 1
  fi
  "$python_bin" scripts/bench/mem_combine.py --outdir "$experiment_combined_dir" "${snapshot_dirs[@]}"
  "$python_bin" scripts/bench/plot_overhead.py "$experiment_combined_dir/combined.csv" "$experiment_charts_dir"
}

write_experiment_audit() {
  local report_file="${experiment_reports_dir}/audit.txt"
  local -a args=(
    scripts/bench/audit_cp_metrics.py
    "$experiment_combined_dir/combined.csv"
    --family "$label_base"
    --stage idle
  )
  local rep
  IFS=',' read -r -a reps_raw <<< "$REPLICAS"
  for rep in "${reps_raw[@]}"; do
    rep="${rep// /}"
    [[ -n "$rep" ]] && args+=(--stage "pods-${rep}")
  done
  IFS=',' read -r -a reps_raw <<< "$ROLL_REPLICAS"
  for rep in "${reps_raw[@]}"; do
    rep="${rep// /}"
    [[ -n "$rep" ]] || continue
    args+=(--stage "rollout-${rep}-during" --stage "rollout-${rep}-during-warm" --stage "rollout-${rep}-post")
  done
  "$python_bin" "${args[@]}" | tee "$report_file"
}

run_cri_lane() {
  local bench_manifest="$1"
  local hook_cmd=""
  if [[ "$BENCH_EXPERIMENT_STEADY_QUIET" == "1" ]]; then
    hook_cmd="$(build_steady_cmd cri "$APP_NAME" 1)"
  fi
  env \
    PATH="$PATH" \
    PYTHONPATH="$py_path" \
    BENCH_SPECS_SRC="$ROOT_DIR" \
    BENCH_EXPERIMENT_OUTPUT_ROOT="$experiment_root" \
    LABEL_CRI="$label_base" \
    APP="$bench_manifest" \
    APP_NAME="$APP_NAME" \
    REPLICAS="$REPLICAS" \
    ROLL_REPLICAS="$ROLL_REPLICAS" \
    DURATION="$DURATION" \
    BENCH_PRE_STEADY_SNAPSHOT_CMD="$hook_cmd" \
    BENCH_PRE_POST_SNAPSHOT_CMD="$hook_cmd" \
    ./scripts/bench/run_cri_refresh.sh
}

run_podman_lane() {
  local lane_name="$1"
  local bench_manifest="$2"
  local metrics_port="$3"
  local use_sudo="$4"
  local env_file="state/bench-experiments/${experiment_id}/${lane_name}.env.sh"
  local -a prep_args=(
    --manifest "$bench_manifest"
    --metrics-port "$metrics_port"
    --env-file "$env_file"
  )
  if [[ "$use_sudo" == "1" ]]; then
    prep_args+=(--sudo-controller)
  fi

  local prepared_env
  prepared_env="$(
    BENCH_SPECS_SRC="$ROOT_DIR" \
    BENCH_SPECS_MINIMAL="$BENCH_SPECS_MINIMAL" \
    BENCH_SPECS_EMPTY="$BENCH_SPECS_EMPTY" \
    BENCH_AUTOCLEAN_PODMAN="${BENCH_AUTOCLEAN_PODMAN:-1}" \
    ./scripts/bench/bench_env_prep.sh "${prep_args[@]}"
  )"
  # shellcheck disable=SC1090
  source "$prepared_env"

  local hook_cmd=""
  if [[ "$BENCH_EXPERIMENT_STEADY_QUIET" == "1" ]]; then
    hook_cmd="$(build_steady_cmd podman "$BENCH_PRIMARY_APP" "$use_sudo")"
  fi

  start_caddy_only || true
  build_demo_images_podman "$use_sudo" || true

  local -a common_env=(
    PYTHONPATH="$py_path"
    AE_RUNTIME_BACKEND=podman
    AE_COLLECT_ENGINE=podman
    AE_ALLOW_PLAINTEXT_SECRETS=1
    AE_ENGINE_STRICT=1
    AE_COLLECT_PODMAN_SUDO="$use_sudo"
    BENCH_CONTROLLER_SUDO="${BENCH_CONTROLLER_SUDO:-0}"
    WAIT_READY_TRIES="$WAIT_READY_TRIES"
    LABEL_SUITE="$label_base"
    APP="$BENCH_PRIMARY_MANIFEST"
    APP_NAME="$BENCH_PRIMARY_APP"
    REPLICAS="$REPLICAS"
    ROLL_REPLICAS="$ROLL_REPLICAS"
    DURATION="$DURATION"
    AE_SPECS_DIR="${AE_SPECS_DIR:-specs}"
    AE_STATE_DB="${AE_STATE_DB:-state/controller.db}"
    AE_CADDY_DIR="${AE_CADDY_DIR:-state/caddy}"
    BENCH_WAIT_RUNTIME="${BENCH_WAIT_RUNTIME:-0}"
    BENCH_PRE_STEADY_SNAPSHOT_CMD="$hook_cmd"
    BENCH_PRE_POST_SNAPSHOT_CMD="$hook_cmd"
  )

  if [[ "$use_sudo" == "1" ]]; then
    env "${common_env[@]}" ./scripts/bench/run_matrix.sh \
      --label-suite "$label_base" \
      --app "$BENCH_PRIMARY_MANIFEST" \
      --app-name "$BENCH_PRIMARY_APP" \
      --replicas "$REPLICAS" \
      --duration "$DURATION" \
      --sudo
    env "${common_env[@]}" ./scripts/bench/run_rollout_k1s.sh \
      --label-suite "$label_base" \
      --app "$BENCH_PRIMARY_MANIFEST" \
      --app-name "$BENCH_PRIMARY_APP" \
      --replicas "$ROLL_REPLICAS" \
      --duration "$DURATION" \
      --sudo
  else
    env "${common_env[@]}" ./scripts/bench/run_matrix.sh \
      --label-suite "$label_base" \
      --app "$BENCH_PRIMARY_MANIFEST" \
      --app-name "$BENCH_PRIMARY_APP" \
      --replicas "$REPLICAS" \
      --duration "$DURATION"
    env "${common_env[@]}" ./scripts/bench/run_rollout_k1s.sh \
      --label-suite "$label_base" \
      --app "$BENCH_PRIMARY_MANIFEST" \
      --app-name "$BENCH_PRIMARY_APP" \
      --replicas "$ROLL_REPLICAS" \
      --duration "$DURATION"
  fi

  ./scripts/bench/bench_env_teardown.sh --env "$prepared_env" || true
  stop_caddy_only || true
}

run_k1nd_lane() {
  local bench_manifest="$1"
  local app_base
  app_base=$(basename "$bench_manifest")
  local container_manifest="/apply/${app_base}"
  local hook_cmd=""
  if [[ "$BENCH_EXPERIMENT_STEADY_QUIET" == "1" ]]; then
    hook_cmd="$(build_steady_cmd docker "$APP_NAME" 0)"
  fi

  scripts/bench/k1nd_sanitize.sh pre
  export K1ND_MANIFEST="$bench_manifest"
  export K1ND_APP_IN_CONTAINER="$container_manifest"
  scripts/bench/k1nd_single.sh up
  scripts/bench/k1nd_single.sh wait

  env \
    PYTHONPATH="$py_path" \
    AE_CLI_IN_CONTAINER=1 \
    AE_CLI_CONTAINER="${AE_CLI_CONTAINER:-k1nd-server}" \
    AE_K1ND_CONTROLLER_CONTAINER="${AE_K1ND_CONTROLLER_CONTAINER:-k1nd-server}" \
    AE_K1ND_APISHIM_CONTAINER="${AE_K1ND_APISHIM_CONTAINER:-k1nd-server}" \
    AE_K1ND_INGRESS_CONTAINER="${AE_K1ND_INGRESS_CONTAINER:-k1nd-server}" \
    BENCH_WAIT_RUNTIME=1 \
    AE_COLLECT_ENGINE=docker \
    AE_ENGINE_STRICT=1 \
    AE_SERIAL_SERVICE_ROLLOUT="${AE_SERIAL_SERVICE_ROLLOUT:-1}" \
    AE_RUNTIME_BACKEND=docker \
    SKIP_GUARDS=1 \
    WAIT_READY_TRIES="$WAIT_READY_TRIES" \
    BENCH_PRE_STEADY_SNAPSHOT_CMD="$hook_cmd" \
    BENCH_PRE_POST_SNAPSHOT_CMD="$hook_cmd" \
    ./scripts/bench/run_matrix.sh \
      --label-suite "$label_base" \
      --app "$container_manifest" \
      --app-name "$APP_NAME" \
      --replicas "$REPLICAS" \
      --duration "$DURATION"

  env \
    PYTHONPATH="$py_path" \
    AE_CLI_IN_CONTAINER=1 \
    AE_CLI_CONTAINER="${AE_CLI_CONTAINER:-k1nd-server}" \
    AE_K1ND_CONTROLLER_CONTAINER="${AE_K1ND_CONTROLLER_CONTAINER:-k1nd-server}" \
    AE_K1ND_APISHIM_CONTAINER="${AE_K1ND_APISHIM_CONTAINER:-k1nd-server}" \
    AE_K1ND_INGRESS_CONTAINER="${AE_K1ND_INGRESS_CONTAINER:-k1nd-server}" \
    BENCH_WAIT_RUNTIME=1 \
    AE_COLLECT_ENGINE=docker \
    AE_ENGINE_STRICT=1 \
    AE_SERIAL_SERVICE_ROLLOUT="${AE_SERIAL_SERVICE_ROLLOUT:-1}" \
    AE_RUNTIME_BACKEND=docker \
    SKIP_GUARDS=1 \
    WAIT_READY_TRIES="$WAIT_READY_TRIES" \
    BENCH_PRE_STEADY_SNAPSHOT_CMD="$hook_cmd" \
    BENCH_PRE_POST_SNAPSHOT_CMD="$hook_cmd" \
    ./scripts/bench/run_rollout_k1s.sh \
      --label-suite "$label_base" \
      --app "$container_manifest" \
      --app-name "$APP_NAME" \
      --replicas "$ROLL_REPLICAS" \
      --duration "$DURATION"

  scripts/bench/k1nd_sanitize.sh post
}

log "lane=${lane} label=${label_base} experiment_root=${experiment_root}"
bench_manifest="$(build_bench_manifest "$APP" "$lane")"

case "$lane" in
  cri)
    run_cri_lane "$bench_manifest"
    ;;
  rootless)
    engines_clear_all
    run_podman_lane rootless "$bench_manifest" 9310 0
    ;;
  rootful)
    engines_clear_all
    run_podman_lane rootful "$bench_manifest" 9311 1
    ;;
  k1nd)
    engines_clear_all
    run_k1nd_lane "$bench_manifest"
    ;;
esac

fix_perms
move_label_dirs
combine_and_plot_experiment
write_experiment_audit

cat > "${experiment_reports_dir}/summary.txt" <<EOF
lane=${lane}
label=${label_base}
app=${APP_NAME}
manifest=${bench_manifest}
steady_quiet=${BENCH_EXPERIMENT_STEADY_QUIET}
rollout_strategy=${BENCH_EXPERIMENT_ROLLOUT_STRATEGY:-baseline}
combined_csv=${experiment_combined_dir}/combined.csv
charts_dir=${experiment_charts_dir}
EOF

log "experiment complete"
log "combined: ${experiment_combined_dir}/combined.csv"
log "charts: ${experiment_charts_dir}"
log "audit: ${experiment_reports_dir}/audit.txt"
