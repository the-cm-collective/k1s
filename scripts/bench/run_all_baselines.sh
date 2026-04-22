#!/usr/bin/env bash
set -euo pipefail

# Run a clean, leak-free baseline across:
# - k1s (Podman rootless)
# - k1s (Podman rootful, snapshots via sudo)
# - dev-min (host controller + podman, no compose)
# - k3d/k3s (cluster up/down, snapshots via sudo)
#
# Requirements:
# - podman, docker, python, make, curl, sudo
# - k3d + kubectl for the k3s suite
# - repo virtualenv optional; charts/docs generation needs matplotlib per docs
#
# Notes:
# - Performs aggressive engine cleanup (rootless+rootful) between suites.
# - Preserves snapshots/ and combined/ outputs.
# - Rebuilds combined.csv/json, charts/, and docs at the end.

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT_DIR"

# -------- Config (override via env) --------
LBL_K1S_ROOTLESS=${LBL_K1S_ROOTLESS:-r20251110+podman+rootless+cg2}
LBL_K1S_ROOTFUL=${LBL_K1S_ROOTFUL:-r20251110+podman+priv+cg2}
LBL_K1ND=${LBL_K1ND:-r20251110+docker+k1nd}
LBL_K3D=${LBL_K3D:-r20251110+k3d}

APP=${APP:-specs/examples/blue.yaml}
APP_NAME=${APP_NAME:-blue}
K3S_MANIFEST=${K3S_MANIFEST:-specs/examples/k3s-echo.yaml}
REPLICAS=${REPLICAS:-1,5,10}
DURATION=${DURATION:-30}
WAIT_READY_TRIES=${WAIT_READY_TRIES:-300}
ROLL_REPLICAS=${ROLL_REPLICAS:-2,5}
BENCH_SPECS_MINIMAL=${BENCH_SPECS_MINIMAL:-1}
BENCH_SPECS_EMPTY=${BENCH_SPECS_EMPTY:-1}
BENCH_BASELINE_STEADY_QUIET=${BENCH_BASELINE_STEADY_QUIET:-1}
BASELINE_STEADY_TIMEOUT=${BASELINE_STEADY_TIMEOUT:-60}
BASELINE_STEADY_DELAY=${BASELINE_STEADY_DELAY:-2}
BASELINE_STEADY_POLLS=${BASELINE_STEADY_POLLS:-3}

# Optional: disable specific suites
# - Set DISABLE_K1ND=1 (or SKIP_K1ND=1) to skip the k1nd baseline stage.
DISABLE_K1ND=${DISABLE_K1ND:-${SKIP_K1ND:-0}}
DISABLE_DEV_MIN=${DISABLE_DEV_MIN:-0}

# -------- Helpers --------
log() { printf "[%s] %s\n" "$(date +%H:%M:%S)" "$*" >&2; }

have() { command -v "$1" >/dev/null 2>&1; }

stop_controller() {
  log "stopping any running controllers (user/root)"
  pkill -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1 || true
  if have sudo; then
    sudo pkill -f "python\s*-m\s*ae\.controller" >/dev/null 2>&1 || true
  fi
}

clear_rootless_podman() {
  if have podman; then
    local ids
    ids=$(podman ps -aq 2>/dev/null || true)
    if [[ -n "$ids" ]]; then
      log "rootless podman: stopping/removing containers"
      podman rm -f $ids >/dev/null 2>&1 || true
    fi
  fi
}

clear_rootful_podman() {
  if [[ "${USE_SUDO:-0}" != "1" ]]; then return 0; fi
  if have sudo && sudo -n true >/dev/null 2>&1; then
    if sudo bash -lc 'command -v podman >/dev/null 2>&1'; then
      local ids
      ids=$(sudo podman ps -aq 2>/dev/null || true)
      if [[ -n "$ids" ]]; then
        log "rootful podman: stopping/removing containers (sudo)"
        sudo podman rm -f $ids >/dev/null 2>&1 || true
      fi
    fi
  fi
}

clear_docker_all() {
  if have docker; then
    local ids
    ids=$(docker ps -aq 2>/dev/null || true)
    if [[ -n "$ids" ]]; then
      log "docker: stopping/removing containers"
      docker rm -f $ids >/dev/null 2>&1 || true
    fi
  fi
}

engines_clear_all() {
  log "clearing container engines (rootless/rootful)"
  stop_controller || true
  clear_rootless_podman || true
  clear_docker_all || true
  clear_rootful_podman || true
  # Also run repo-provided deep clear (rootful) when allowed
  if [[ "${USE_SUDO:-0}" == "1" ]] && have make && have sudo; then
    log "make bench-engines-clear (sudo)"
    sudo make bench-engines-clear CONFIRM=1 >/dev/null 2>&1 || true
  fi
}

fix_perms() {
  if have make && have sudo; then
    log "normalizing artifact permissions"
    sudo make bench-fix-perms >/dev/null 2>&1 || true
  fi
}

rebuild_docs_and_charts() {
  log "backfilling OCI runtime tags and rebuilding charts/docs"
  make bench-mem-backfill-oci GLOB='snapshots/*/*' REBUILD_DOCS=1 >/dev/null 2>&1 || true
}

print_summary() {
  local csv="combined/combined.csv"
  [[ -f "$csv" ]] || { log "summary skipped (missing $csv)"; return 0; }
  python - "$csv" <<'PY' || true
import csv, sys
from pathlib import Path

csv_path = Path(sys.argv[1])
rows = list(csv.DictReader(csv_path.open()))
if not rows:
    sys.exit(0)

def scenario_name(r):
    mode = (r.get('mode','') or '').lower()
    backend = (r.get('backend','') or '').lower()
    label = r.get('label','')
    root = 'rootless' if '+rootless+' in label else ('priv' if '+priv+' in label else '?')
    if mode == 'k1s' and backend == 'podman' and root == 'rootless':
        return 'k1s rootless'
    if mode == 'k1s' and backend == 'podman' and root == 'priv':
        return 'k1s rootful'
    if mode == 'k1s' and backend == 'docker':
        return 'k1nd'
    if mode == 'k3s':
        return 'k3d'
    return f"{mode} {backend}".strip()

def stage_name(label):
    if label.endswith('-idle'): return 'idle'
    import re
    m = re.search(r'-pods-(\d+)$', label)
    if m: return f"pods-{m.group(1)}"
    return 'other'

latest = {}
for r in rows:
    st = stage_name(r.get('label',''))
    if st == 'other':
        continue
    sc = scenario_name(r)
    key = (sc, st)
    if key not in latest or r.get('timestamp','') > latest[key].get('timestamp',''):
        latest[key] = r

scenarios = [
    'k1s rootless','k1s rootful','k1nd','k3d'
]
stages = ['idle','pods-1','pods-5','pods-10']

def to_mib(val, kib=False):
    try: v=float(val or 0)
    except: v=0.0
    return v/1024.0 if kib else v/1048576.0

print('\nSummary (latest per scenario/stage)')
print('Scenario  Stage    Ctrl/CP  Runtime  Ingress  AppCG  HostCG  MemAvailΔ')
print('(MiB)     (MiB)    (MiB)    (MiB)    (MiB)    (MiB)')
for sc in scenarios:
    for st in stages:
        r = latest.get((sc,st))
        if not r: continue
        ctrl_key = 'k3s_control_plane_pss_kb' if sc == 'k3d' else 'controller_pss_kb'
        ctrl = to_mib(r.get(ctrl_key,'0'), kib=True)
        run  = to_mib(r.get('runtime_pss_kb','0'), kib=True)
        ingr = to_mib(r.get('ingress_pss_kb','0'), kib=True)
        app  = to_mib(r.get('app_mem_bytes','0'))
        host = to_mib(r.get('host_system_cgroups_bytes','0'))
        dmem = to_mib(r.get('mem_available_delta_bytes','0'))
        print(f"{sc:<8} {st:<7} {ctrl:7.1f} {run:8.1f} {ingr:8.1f} {app:7.1f} {host:7.1f} {dmem:9.1f}")
print('Ctrl/CP = AE controller PSS for k1s/k1nd, k3s control-plane PSS for k3d')
print()
PY
}

build_steady_cmd() {
  local backend="$1"
  local app="$2"
  local use_sudo="$3"
  local python_bin="${PYTHON_BIN:-python}"
  local py_path="${PYTHONPATH:-$ROOT_DIR/src}"
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
    --timeout "$BASELINE_STEADY_TIMEOUT"
    --delay "$BASELINE_STEADY_DELAY"
    --stable-polls "$BASELINE_STEADY_POLLS"
    --require-app-present
  )
  if [[ "$use_sudo" == "1" ]]; then
    cmd=(
      sudo
      --preserve-env=BENCH_SNAPSHOT_LABEL,BENCH_SNAPSHOT_STAGE,BENCH_SNAPSHOT_REPLICAS,BENCH_SNAPSHOT_DURATION,BENCH_SNAPSHOT_CAPTURE_TIMING,BENCH_BACKEND,BENCH_APP_NAME
      "${cmd[@]}"
    )
  fi
  printf '%q ' "${cmd[@]}"
}

# -------- Preflights --------
have python || { echo "python is required" >&2; exit 2; }
have make   || { echo "make is required" >&2; exit 2; }
have podman || { echo "podman is required for k1s suites" >&2; exit 2; }
have docker || { echo "docker is required for k1nd/k3d suites" >&2; exit 2; }

# Sudo policy: ALLOW_SUDO=1 to enable non-interactively (CI). If unset and TTY,
# ask once up front and cache credentials. Otherwise default to 0.
USE_SUDO=0
if [[ "${ALLOW_SUDO:-}" == "1" ]]; then
  if command -v sudo >/dev/null 2>&1; then
    if sudo -v; then USE_SUDO=1; fi
  fi
elif [[ -t 0 ]] && command -v sudo >/dev/null 2>&1; then
  echo -n "Use sudo for rootful runs and deep engine cleanup? [y/N] "
  read -r ans || ans=""
  case "${ans,,}" in
    y|yes)
      if sudo -v; then USE_SUDO=1; else log "sudo unavailable; continuing without"; fi
      ;;
    *) ;; 
  esac
fi
if [[ "$USE_SUDO" == "1" ]]; then
  log "sudo enabled for privileged steps"
else
  log "sudo disabled; rootful suite will be skipped; k3s will run without sudo"
fi

start_ts=$(date +%Y-%m-%dT%H:%M:%S)
log "starting baseline at $start_ts"

# Build demo images if missing (rootless/rootful)
build_demo_images_podman() {
  local sudo_flag="$1"    # 0 or 1
  local runner=podman
  if [[ "$sudo_flag" == "1" ]]; then
    have sudo || return 0
    runner="sudo podman"
  fi
  if ! $runner images --format '{{.Repository}}:{{.Tag}}' | grep -q '^demo-blue:latest$'; then
    log "building demo-blue:latest ($([[ "$sudo_flag" == "1" ]] && echo rootful || echo rootless))"
    $runner build -t demo-blue:latest samples/servers/blue >/dev/null 2>&1 || true
  fi
  if ! $runner images --format '{{.Repository}}:{{.Tag}}' | grep -q '^demo-green:latest$'; then
    log "building demo-green:latest ($([[ "$sudo_flag" == "1" ]] && echo rootful || echo rootless))"
    $runner build -t demo-green:latest samples/servers/green >/dev/null 2>&1 || true
  fi
}

# Ingress: run Caddy only (no controller) to avoid foreign ae.app containers in Docker
start_caddy_only() {
  have docker || { log "docker required to run caddy ingress"; return 0; }
  local DOCK='docker'
  if ! docker ps >/dev/null 2>&1 && [[ "${USE_SUDO:-0}" == "1" ]] && command -v sudo >/dev/null 2>&1; then
    DOCK='sudo docker'
  fi
  # Important: name must NOT start with 'ae-' to avoid strict foreign-engine checks
  local name=caddy-bench
  $DOCK rm -f "$name" >/dev/null 2>&1 || true
  log "starting caddy (docker)"
  $DOCK run -d --name "$name" \
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
  local DOCK='docker'
  if ! docker ps >/dev/null 2>&1 && [[ "${USE_SUDO:-0}" == "1" ]] && command -v sudo >/dev/null 2>&1; then
    DOCK='sudo docker'
  fi
  $DOCK rm -f caddy-bench >/dev/null 2>&1 || true
}

run_isolated_k1s_suite() {
  local suite_name="$1"
  local metrics_port="$2"
  local make_target="$3"
  local label="$4"
  local podman_sudo="$5"
  local env_file="state/bench-baselines/${suite_name}/env.sh"
  local prep_args=(
    --manifest "$APP"
    --metrics-port "$metrics_port"
    --env-file "$env_file"
  )
  local rc=0

  if [[ "$make_target" == "bench-mem-e2e-k1s-sudo" ]]; then
    prep_args+=(--sudo-controller)
  fi

  env_file="$(
    BENCH_SPECS_MINIMAL="$BENCH_SPECS_MINIMAL" \
    BENCH_SPECS_EMPTY="$BENCH_SPECS_EMPTY" \
    BENCH_AUTOCLEAN_PODMAN="${BENCH_AUTOCLEAN_PODMAN:-1}" \
    ./scripts/bench/bench_env_prep.sh "${prep_args[@]}"
  )"

  # shellcheck disable=SC1090
  source "$env_file"

  local hook_cmd=""
  if [[ "$BENCH_BASELINE_STEADY_QUIET" == "1" ]]; then
    hook_cmd="$(build_steady_cmd podman "$BENCH_PRIMARY_APP" "$podman_sudo")"
  fi

  if AE_ENGINE_STRICT=1 \
    AE_COLLECT_PODMAN_SUDO="$podman_sudo" \
    BENCH_CONTROLLER_SUDO="$BENCH_CONTROLLER_SUDO" \
    WAIT_READY_TRIES="$WAIT_READY_TRIES" \
    LABEL_SUITE="$label" \
    APP="$BENCH_PRIMARY_MANIFEST" \
    APP_NAME="$BENCH_PRIMARY_APP" \
    REPLICAS="$REPLICAS" \
    ROLL_REPLICAS="$ROLL_REPLICAS" \
    DURATION="$DURATION" \
    AE_SPECS_DIR="$AE_SPECS_DIR" \
    AE_STATE_DB="$AE_STATE_DB" \
    AE_CADDY_DIR="$AE_CADDY_DIR" \
    BENCH_WAIT_RUNTIME="$BENCH_WAIT_RUNTIME" \
    BENCH_PRE_STEADY_SNAPSHOT_CMD="$hook_cmd" \
    BENCH_PRE_POST_SNAPSHOT_CMD="$hook_cmd" \
    make "$make_target"; then
    :
  else
    rc=$?
  fi

  ./scripts/bench/bench_env_teardown.sh --env "$env_file" || true
  return "$rc"
}

# -------- Suite: k1s rootless --------
log "suite: k1s rootless"
engines_clear_all
start_caddy_only || true
build_demo_images_podman 0 || true
# Rootless snapshots must inspect the user's podman namespace, not rootful podman.
PYTHONPATH=src AE_RUNTIME_BACKEND=podman AE_COLLECT_ENGINE=podman AE_ALLOW_PLAINTEXT_SECRETS=1 \
  run_isolated_k1s_suite rootless 9210 bench-mem-e2e-k1s "$LBL_K1S_ROOTLESS" 0
fix_perms
stop_caddy_only || true

# -------- Suite: k1s rootful (sudo) --------
log "suite: k1s rootful (sudo)"
engines_clear_all
if [[ "$USE_SUDO" == "1" ]]; then
  start_caddy_only || true
  build_demo_images_podman 1 || true
  PYTHONPATH=src AE_RUNTIME_BACKEND=podman AE_COLLECT_ENGINE=podman AE_ALLOW_PLAINTEXT_SECRETS=1 \
    run_isolated_k1s_suite rootful 9211 bench-mem-e2e-k1s-sudo "$LBL_K1S_ROOTFUL" 1
  fix_perms
  stop_caddy_only || true
else
  log "sudo not enabled; skipping k1s rootful"
fi

# -------- Suite: k1nd (single-container) --------
if [[ "$DISABLE_K1ND" == "1" ]]; then
  log "skipping suite: k1nd (DISABLE_K1ND=1)"
else
  log "suite: k1nd (single-container)"
  engines_clear_all
  AE_ENGINE_STRICT=1 AE_COLLECT_ENGINE=docker WAIT_READY_TRIES="$WAIT_READY_TRIES" make bench-mem-e2e-k1nd \
    LABEL_SUITE="$LBL_K1ND" APP="$APP" APP_NAME="$APP_NAME" REPLICAS="$REPLICAS" DURATION="$DURATION"
  fix_perms
fi

# -------- Suite: dev-min (legacy k1nd compose deprecated) --------
if [[ "$DISABLE_DEV_MIN" == "1" ]]; then
  log "skipping suite: dev-min (DISABLE_DEV_MIN=1)"
else
  log "suite: dev-min (uses rootless podman bench env)"
  # dev-min bench runs are covered by the rootless/rootful suites above.
fi

# -------- Suite: k3d/k3s (sudo snapshots) --------
log "suite: k3d/k3s"
engines_clear_all
if [[ "$USE_SUDO" == "1" ]]; then
  AE_ENGINE_STRICT=1 AE_COLLECT_ENGINE=docker WAIT_READY_TRIES="$WAIT_READY_TRIES" make bench-mem-e2e-k3s-sudo \
    LABEL_SUITE="$LBL_K3D" MANIFEST="$K3S_MANIFEST" REPLICAS="$REPLICAS" DURATION="$DURATION"
else
  AE_ENGINE_STRICT=1 AE_COLLECT_ENGINE=docker WAIT_READY_TRIES="$WAIT_READY_TRIES" make bench-mem-e2e-k3s \
    LABEL_SUITE="$LBL_K3D" MANIFEST="$K3S_MANIFEST" REPLICAS="$REPLICAS" DURATION="$DURATION"
fi
# tear down cluster
make bench-k3s-down K3S_NAME=bench >/dev/null 2>&1 || true
fix_perms

# -------- Finalize --------
log "rebuilding combined, charts, and docs"
python scripts/bench/mem_combine.py snapshots/*/* >/dev/null 2>&1 || true
python scripts/bench/plot_overhead.py combined/combined.csv charts >/dev/null 2>&1 || true
python docs/build_docs.py >/dev/null 2>&1 || true
rebuild_docs_and_charts

end_ts=$(date +%Y-%m-%dT%H:%M:%S)
log "baseline complete at $end_ts"
print_summary

exit 0
