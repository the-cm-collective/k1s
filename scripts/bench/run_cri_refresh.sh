#!/usr/bin/env bash
set -Eeuo pipefail

# Refresh benchmarks using the CRI backend (containerd).
# Requires: sudo + containerd socket + crictl (for ingress reloads if enabled).

if [[ "$(id -u)" -eq 0 ]]; then
  echo "[cri-refresh] do not run as root; run as your user (sudo used internally)" >&2
  exit 2
fi

log() { echo "[cri-refresh] $*" >&2; }
trap 'log "error at line $LINENO: $BASH_COMMAND"' ERR

cleanup_done=0
cleanup_env_file=""

bench_cleanup() {
  if [[ "${cleanup_done:-0}" == "1" ]]; then
    return 0
  fi
  cleanup_done=1
  if [[ -n "${cleanup_env_file:-}" && -f "$cleanup_env_file" ]]; then
    ./scripts/bench/bench_env_teardown.sh --env "$cleanup_env_file" || true
  fi
}

trap 'rc=$?; set +e; bench_cleanup; exit "$rc"' EXIT

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

APP="${APP:-specs/examples/echo.yaml}"
APP_NAME="${APP_NAME:-echo}"
REPLICAS="${REPLICAS:-1,5,10}"
ROLL_REPLICAS="${ROLL_REPLICAS:-2,5}"
DURATION="${DURATION:-30}"
bench_runtime_handler="runc"

: "${BENCH_READY_STABLE_POLLS:=3}"
: "${BENCH_SETTLE_DELAY:=5}"
: "${CRI_POD_CLEANUP_TIMEOUT:=60}"
: "${CRI_POD_CLEANUP_SETTLE:=2}"
: "${CRI_RUNTIME_READY_TIMEOUT:=120}"
: "${CRI_RUNTIME_READY_DELAY:=2}"
: "${CRI_RUNTIME_READY_SETTLE:=3}"
: "${CRI_IDLE_QUIET_TIMEOUT:=60}"
: "${CRI_IDLE_QUIET_DELAY:=2}"
: "${CRI_IDLE_QUIET_POLLS:=3}"
: "${BENCH_IDLE_VALIDATE_ZERO_APP:=1}"
: "${CRI_DEBUG_STATE_DIR:=state/bench-cri-debug}"
: "${CRI_DEBUG_CAPTURE_ON_QUIET:=1}"

export BENCH_READY_STABLE_POLLS
export BENCH_SETTLE_DELAY
export CRI_POD_CLEANUP_TIMEOUT
export CRI_POD_CLEANUP_SETTLE
export CRI_RUNTIME_READY_TIMEOUT
export CRI_RUNTIME_READY_DELAY
export CRI_RUNTIME_READY_SETTLE
export CRI_IDLE_QUIET_TIMEOUT
export CRI_IDLE_QUIET_DELAY
export CRI_IDLE_QUIET_POLLS
export BENCH_IDLE_VALIDATE_ZERO_APP

LABEL_CRI="${LABEL_CRI:-r$(date +%Y%m%d)+cri+containerd}"
metrics_port="${BENCH_CRI_METRICS_PORT:-9212}"
env_file="${BENCH_CRI_ENV_FILE:-state/bench-cri/env.sh}"

bench_specs_minimal="${BENCH_SPECS_MINIMAL:-0}"
bench_specs_empty="${BENCH_SPECS_EMPTY:-1}"

# If the metrics port is already in use, pick a free port to avoid controller startup failure.
python_check="${PYTHON_BIN:-$(command -v python)}"
if [[ -n "$python_check" ]]; then
  if ! "$python_check" - "$metrics_port" <<'PY' >/dev/null 2>&1; then
import socket, sys
port = int(sys.argv[1])
s = socket.socket()
try:
    s.bind(("127.0.0.1", port))
    s.close()
    sys.exit(0)
except OSError:
    sys.exit(1)
PY
    new_port=$("$python_check" - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
print(port)
PY
    )
    if [[ -n "$new_port" ]]; then
      echo "[cri-refresh] metrics port ${metrics_port} in use; using ${new_port}" >&2
      metrics_port="$new_port"
    fi
  fi
fi

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

# Ensure sudo is usable before starting the controller.
if [[ -t 0 && "${SUDO_AUTO_PROMPT:-1}" == "1" ]]; then
  if ! sudo -v; then
    log "sudo auth failed; rerun with working sudo or set SUDO_AUTO_PROMPT=0 for non-interactive mode"
    exit 3
  fi
else
  if ! sudo -n true >/dev/null 2>&1; then
    log "sudo credentials not cached (or NOPASSWD required); run 'sudo -v' or set SUDO_AUTO_PROMPT=1"
    exit 3
  fi
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

first_requested_replica() {
  local raw="$1"
  local item=""
  raw="${raw//,/ }"
  for item in $raw; do
    item="${item// /}"
    [[ -z "$item" ]] && continue
    if [[ ! "$item" =~ ^[0-9]+$ ]]; then
      return 1
    fi
    printf '%s\n' "$item"
    return 0
  done
  return 1
}

log "preparing bench environment"
bench_log="$(mktemp)"
if ! ENV_FILE="$(
  BENCH_SPECS_MINIMAL="$bench_specs_minimal" \
  BENCH_SPECS_EMPTY="$bench_specs_empty" \
  BENCH_AUTOCLEAN_PODMAN="${BENCH_AUTOCLEAN_PODMAN:-1}" \
  ./scripts/bench/bench_env_prep.sh \
    --manifest "$APP" \
    --metrics-port "$metrics_port" \
    --env-file "$env_file" \
    --sudo-controller
)" 2>"$bench_log"; then
  log "bench_env_prep failed"
  sed -n '1,200p' "$bench_log" >&2 || true
  rm -f "$bench_log"
  exit 4
fi
rm -f "$bench_log"
if [[ -z "${ENV_FILE:-}" ]]; then
  log "bench_env_prep returned empty env file path"
  exit 4
fi
if [[ ! -f "$ENV_FILE" ]]; then
  log "bench_env_prep returned missing env file: $ENV_FILE"
  exit 4
fi

# shellcheck disable=SC1090
if ! source "$ENV_FILE"; then
  log "failed to source env file: $ENV_FILE"
  exit 4
fi
cleanup_env_file="$ENV_FILE"
log "bench environment ready: $ENV_FILE"

bench_app_name="${BENCH_PRIMARY_APP:-$APP_NAME}"
python_bin="${PYTHON_BIN:-python}"
py_path="${PYTHONPATH:-$repo_root/src}"
sudo_cmd=(sudo -n)
if [[ "${SUDO_INTERACTIVE:-0}" == "1" ]]; then
  sudo_cmd=(sudo)
fi
sudo_env_base=(
  "HOME=/root"
  "XDG_CONFIG_HOME=/root/.config"
  "XDG_DATA_HOME=/root/.local/share"
  "XDG_CACHE_HOME=/root/.cache"
  "XDG_RUNTIME_DIR=/run/user/0"
  "DBUS_SESSION_BUS_ADDRESS="
  "CONTAINER_HOST="
  "PODMAN_HOST="
)
sudo_env_clean=(
  "-i"
  "PATH=${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
  "LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}"
  "NIX_LD_LIBRARY_PATH=${NIX_LD_LIBRARY_PATH:-}"
  "NIX_LD=${NIX_LD:-}"
)
sudo_env_cli=(
  "${sudo_env_base[@]}"
  "AE_SPECS_DIR=${AE_SPECS_DIR:-specs}"
  "AE_STATE_DB=${AE_STATE_DB:-state/controller.db}"
  "AE_CADDY_DIR=${AE_CADDY_DIR:-state/caddy}"
  "AE_ALLOW_PLAINTEXT_SECRETS=${AE_ALLOW_PLAINTEXT_SECRETS:-1}"
  "AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND:-podman}"
  "AE_OCI_RUNTIME=${AE_OCI_RUNTIME:-}"
  "AE_CRI_ENDPOINT=${AE_CRI_ENDPOINT:-}"
  "AE_CRI_SANDBOX_IMAGE=${AE_CRI_SANDBOX_IMAGE:-}"
  "AE_PODMAN_BIN=${AE_PODMAN_BIN:-podman}"
  "AE_DISABLE_INGRESS=${AE_DISABLE_INGRESS:-}"
  "PYTHONPATH=${py_path}"
)

ae_cli() {
  if [[ "${BENCH_CONTROLLER_SUDO:-0}" == "1" ]] && command -v sudo >/dev/null 2>&1; then
    sudo env "${sudo_env_clean[@]}" "${sudo_env_cli[@]}" "$python_bin" -m ae.cli "$@"
  else
    "$python_bin" -m ae.cli "$@"
  fi
}

delete_bench_app() {
  local app="$1"
  local reason="${2:-state reset}"
  log "delete desired state for app=${app} before ${reason}"
  ae_cli delete "$app"
}

cri_can_nosudo() {
  command -v crictl >/dev/null 2>&1 && crictl --runtime-endpoint "$AE_CRI_ENDPOINT" info >/dev/null 2>&1
}

cri_switch_to_sudo() {
  local reason="${1:-runtime access}"
  if [[ "$cri_use_sudo" == "1" ]]; then
    return 0
  fi
  if ! "${sudo_cmd[@]}" true >/dev/null 2>&1; then
    return 1
  fi
  echo "[cri-refresh] non-sudo CRI access unavailable after ${reason}; falling back to sudo for remainder of run" >&2
  cri_use_sudo=1
  return 0
}

cri_try_enable_nosudo() {
  local sock="$endpoint_path"
  if [[ -z "$sock" || ! -S "$sock" ]]; then
    return 1
  fi
  if command -v setfacl >/dev/null 2>&1; then
    "${sudo_cmd[@]}" setfacl -m u:"$USER":rw "$sock" >/dev/null 2>&1 && return 0
  fi
  if [[ "${CRI_UNSAFE_CHMOD:-0}" == "1" ]]; then
    "${sudo_cmd[@]}" chmod o+rw "$sock" >/dev/null 2>&1 && return 0
  fi
  return 1
}

cri_use_sudo=0
if ! cri_can_nosudo; then
  cri_use_sudo=1
fi

if [[ "${CRI_NO_SUDO:-0}" == "1" && "$cri_use_sudo" == "1" ]]; then
  if [[ "${CRI_AUTO_ACL:-1}" == "1" ]] && "${sudo_cmd[@]}" true >/dev/null 2>&1; then
    if cri_try_enable_nosudo && cri_can_nosudo; then
      cri_use_sudo=0
    fi
  fi
  if [[ "$cri_use_sudo" == "1" ]]; then
    echo "[cri-refresh] CRI_NO_SUDO=1 but non-sudo crictl access failed; configure socket ACLs or unset CRI_NO_SUDO" >&2
    exit 3
  fi
fi

if [[ "$cri_use_sudo" == "1" ]]; then
  if ! "${sudo_cmd[@]}" true >/dev/null 2>&1; then
    echo "[cri-refresh] sudo credentials not cached (or NOPASSWD required); run 'sudo -v' or enable non-sudo CRI access" >&2
    exit 3
  fi
  if [[ "${CRI_AUTO_ACL:-1}" == "1" ]]; then
    if cri_try_enable_nosudo && cri_can_nosudo; then
      echo "[cri-refresh] enabled non-sudo CRI access for this session" >&2
      cri_use_sudo=0
    fi
  fi
fi

cri_cmd() {
  if [[ "$cri_use_sudo" == "1" ]]; then
    "${sudo_cmd[@]}" crictl --runtime-endpoint "$AE_CRI_ENDPOINT" "$@"
    return $?
  fi
  crictl --runtime-endpoint "$AE_CRI_ENDPOINT" "$@"
  local rc=$?
  if (( rc == 0 )); then
    return 0
  fi
  if ! cri_can_nosudo && cri_switch_to_sudo "crictl $1"; then
    "${sudo_cmd[@]}" crictl --runtime-endpoint "$AE_CRI_ENDPOINT" "$@"
    return $?
  fi
  return $rc
}

ctr_cmd() {
  if [[ "$cri_use_sudo" == "1" ]]; then
    "${sudo_cmd[@]}" ctr -n k8s.io "$@"
    return $?
  fi
  ctr -n k8s.io "$@"
  local rc=$?
  if (( rc == 0 )); then
    return 0
  fi
  if ! cri_can_nosudo && cri_switch_to_sudo "ctr $1"; then
    "${sudo_cmd[@]}" ctr -n k8s.io "$@"
    return $?
  fi
  return $rc
}

cri_default_runtime_name() {
  cri_cmd info 2>/dev/null | "$python_bin" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)
containerd = (data.get("config") or {}).get("containerd") or {}
print(containerd.get("defaultRuntimeName") or "")
' || true
}

run_cri_preflight_inner() {
  if [[ "$cri_use_sudo" == "1" ]]; then
    "${sudo_cmd[@]}" env PATH="$PATH" \
      AE_CRI_REQUIRE_RUNTIME_READY=1 \
      AE_CRI_RUNTIME_HANDLER="${bench_runtime_handler}" \
      ./scripts/cri_preflight.sh
  else
    env PATH="$PATH" \
      AE_CRI_REQUIRE_RUNTIME_READY=1 \
      AE_CRI_RUNTIME_HANDLER="${bench_runtime_handler}" \
      ./scripts/cri_preflight.sh
  fi
}

run_cri_preflight() {
  if run_cri_preflight_inner; then
    return 0
  fi
  local rc=$?
  if [[ "$cri_use_sudo" == "0" ]] && ! cri_can_nosudo && cri_switch_to_sudo "CRI preflight"; then
    run_cri_preflight_inner
    return $?
  fi
  return $rc
}

cri_wait_runtime_ready() {
  local reason="${1:-runtime transition}"
  local timeout="${CRI_RUNTIME_READY_TIMEOUT:-90}"
  local delay="${CRI_RUNTIME_READY_DELAY:-2}"
  local settle="${CRI_RUNTIME_READY_SETTLE:-2}"
  local deadline=$((SECONDS + timeout))

  log "waiting for CRI runtime ready after ${reason} (timeout=${timeout}s delay=${delay}s)"
  while :; do
    if run_cri_preflight >/dev/null 2>&1; then
      if (( settle > 0 )); then
        sleep "$settle"
      fi
      log "CRI runtime ready after ${reason}"
      return 0
    fi
    if (( SECONDS >= deadline )); then
      log "CRI runtime did not become ready after ${reason} within ${timeout}s"
      run_cri_preflight || true
      return 1
    fi
    sleep "$delay"
  done
}

cri_collect_app_state_json() {
  local app="$1"
  local pods_json="" containers_json="" query_failed=0
  if ! pods_json=$(cri_cmd pods -o json 2>/dev/null); then
    query_failed=1
  fi
  if ! containers_json=$(cri_cmd ps -a -o json 2>/dev/null); then
    query_failed=1
  fi
  "$python_bin" - "$app" "$query_failed" <(printf '%s' "$pods_json") <(printf '%s' "$containers_json") <<'PY' || true
import json
import sys

app = sys.argv[1]
query_failed = bool(int(sys.argv[2]))
pods_path = sys.argv[3]
containers_path = sys.argv[4]


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def labels_for(item):
    labels = item.get("labels") or {}
    if labels:
        return {str(k): str(v) for k, v in labels.items()}
    meta = item.get("metadata") or {}
    meta_labels = meta.get("labels") or {}
    return {str(k): str(v) for k, v in meta_labels.items()}


def item_name(item):
    meta = item.get("metadata") or {}
    return str(meta.get("name") or item.get("name") or "")


pods = load(pods_path)
containers = load(containers_path)
pod_ids = set()
live_pod_ids = set()

for pod in (pods.get("items") or pods.get("pods") or []):
    labels = labels_for(pod)
    name = item_name(pod)
    if labels.get("ae.app") == app or (name.startswith(f"{app}-rev") and not labels.get("ae.app")):
        pid = pod.get("id") or pod.get("podSandboxId") or pod.get("pod_sandbox_id")
        if pid:
            pod_ids.add(str(pid))
    pid = pod.get("id") or pod.get("podSandboxId") or pod.get("pod_sandbox_id")
    if pid:
        live_pod_ids.add(str(pid))

live_container_ids = set()
container_pod_ids = set()
orphan_container_ids = []
for container in (containers.get("containers") or containers.get("items") or []):
    labels = labels_for(container)
    replica_id = labels.get("ae.pod_name") or labels.get("ae.replica_id") or ""
    app_label = labels.get("ae.app") or ""
    if app_label != app and not replica_id.startswith(f"{app}-rev"):
        continue
    cid = container.get("id") or container.get("containerId") or container.get("container_id")
    if cid:
        live_container_ids.add(str(cid))
    pod_id = (
        container.get("podSandboxId")
        or container.get("pod_sandbox_id")
        or container.get("pod_id")
        or ""
    )
    if pod_id:
        pod_id = str(pod_id)
    if pod_id and pod_id in live_pod_ids:
        container_pod_ids.add(pod_id)
        continue
    if cid:
        orphan_container_ids.append(str(cid))

pod_ids.update(container_pod_ids)

print(
    json.dumps(
        {
            "query_failed": query_failed,
            "pod_ids": sorted(pod_ids),
            "live_container_ids": sorted(live_container_ids),
            "orphan_container_ids": sorted(set(orphan_container_ids)),
        }
    )
)
PY
}

cri_state_field_lines() {
  local field="$1"
  local state_json="$2"
  printf '%s' "$state_json" | "$python_bin" -c '
import json, sys
field = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for item in data.get(field) or []:
    if item:
        print(str(item))
' "$field" || true
}

cri_state_query_failed() {
  local state_json="$1"
  printf '%s' "$state_json" | "$python_bin" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if data.get("query_failed") else 1)
' >/dev/null 2>&1
}

cri_debug_reason_slug() {
  local reason="${1:-state}"
  reason="${reason//[^A-Za-z0-9._-]/-}"
  while [[ "$reason" == *--* ]]; do
    reason="${reason//--/-}"
  done
  reason="${reason#-}"
  reason="${reason%-}"
  printf '%s\n' "${reason:-state}"
}

cri_collect_matching_containers_via_inspect() {
  local app="$1"
  local ps_json=""
  local -a ids=()
  local cid inspect_json

  if ! ps_json="$(cri_cmd ps -a -o json 2>/dev/null)"; then
    return 1
  fi

  while IFS= read -r cid; do
    [[ -n "$cid" ]] && ids+=("$cid")
  done < <(printf '%s' "$ps_json" | "$python_bin" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
for item in data.get("containers") or data.get("items") or []:
    cid = item.get("id") or item.get("containerId") or item.get("container_id") or ""
    if cid:
        print(str(cid))
' || true)

  for cid in "${ids[@]}"; do
    inspect_json="$(cri_cmd inspect -o json "$cid" 2>/dev/null || true)"
    [[ -z "$inspect_json" ]] && continue
    printf '%s' "$inspect_json" | "$python_bin" - "$cid" "$app" <<'PY' || true
import json
import sys

cid = sys.argv[1]
app = sys.argv[2]

try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)

status = data.get("status") or {}
labels = status.get("labels") or {}
replica_id = labels.get("ae.pod_name") or labels.get("ae.replica_id") or ""
if labels.get("ae.app") != app and not replica_id.startswith(f"{app}-rev"):
    raise SystemExit(1)

meta = status.get("metadata") or {}
name = meta.get("name") or data.get("info", {}).get("name") or ""
pod_id = status.get("podSandboxId") or ""
print("\t".join([cid, str(name), str(replica_id), str(pod_id)]))
PY
  done
}

cri_dump_app_state_debug() {
  local app="$1"
  local reason="${2:-state}"
  local slug outdir ps_json pods_json state_json
  local -a matches=()
  local line cid pod_id

  slug="$(cri_debug_reason_slug "$reason")"
  outdir="${CRI_DEBUG_STATE_DIR}/$(date +%Y%m%d-%H%M%S)-${slug}"
  mkdir -p "$outdir"

  printf '%s\n' "$app" > "$outdir/app.txt"
  printf '%s\n' "$reason" > "$outdir/reason.txt"

  state_json="$(cri_collect_app_state_json "$app")"
  printf '%s\n' "$state_json" > "$outdir/state.json"

  pods_json="$(cri_cmd pods -o json 2>/dev/null || true)"
  printf '%s\n' "$pods_json" > "$outdir/pods.json"

  ps_json="$(cri_cmd ps -a -o json 2>/dev/null || true)"
  printf '%s\n' "$ps_json" > "$outdir/containers.json"

  while IFS= read -r line; do
    [[ -n "$line" ]] && matches+=("$line")
  done < <(cri_collect_matching_containers_via_inspect "$app" || true)

  {
    echo -e "container_id\tname\treplica_id\tpod_id"
    for line in "${matches[@]}"; do
      echo "$line"
    done
  } > "$outdir/matching-containers.tsv"

  for line in "${matches[@]}"; do
    IFS=$'\t' read -r cid _ _ pod_id <<< "$line"
    [[ -n "$cid" ]] && cri_cmd inspect -o json "$cid" > "$outdir/inspect-${cid}.json" 2>/dev/null || true
    [[ -n "$pod_id" ]] && cri_cmd inspectp -o json "$pod_id" > "$outdir/inspectp-${pod_id}.json" 2>/dev/null || true
  done

  log "captured CRI debug state: ${outdir}"
}

cri_wait_app_quiet() {
  local app="$1"
  local reason="${2:-app quiet state}"
  local timeout="${CRI_IDLE_QUIET_TIMEOUT:-60}"
  local delay="${CRI_IDLE_QUIET_DELAY:-2}"
  local quiet_polls="${CRI_IDLE_QUIET_POLLS:-3}"
  local deadline=$((SECONDS + timeout))
  local stable_hits=0
  local state_json=""
  local -a pod_ids_arr=()
  local -a container_ids_arr=()
  local id

  if (( quiet_polls < 1 )); then
    quiet_polls=1
  fi

  log "waiting for CRI app quiet after ${reason} (timeout=${timeout}s delay=${delay}s stable=${quiet_polls})"
  while :; do
    state_json="$(cri_collect_app_state_json "$app")"
    if ! cri_state_query_failed "$state_json"; then
      pod_ids_arr=()
      container_ids_arr=()
      while IFS= read -r id; do
        [[ -n "$id" ]] && pod_ids_arr+=("$id")
      done < <(cri_state_field_lines "pod_ids" "$state_json")
      while IFS= read -r id; do
        [[ -n "$id" ]] && container_ids_arr+=("$id")
      done < <(cri_state_field_lines "live_container_ids" "$state_json")

      if (( ${#pod_ids_arr[@]} == 0 && ${#container_ids_arr[@]} == 0 )); then
        stable_hits=$((stable_hits + 1))
        if (( stable_hits >= quiet_polls )); then
          if [[ "${CRI_DEBUG_CAPTURE_ON_QUIET:-0}" == "1" ]]; then
            cri_dump_app_state_debug "$app" "quiet-${reason}"
          fi
          log "CRI app quiet after ${reason}"
          return 0
        fi
      else
        stable_hits=0
      fi
    else
      stable_hits=0
    fi

    if (( SECONDS >= deadline )); then
      cri_dump_app_state_debug "$app" "quiet-timeout-${reason}"
      if cri_state_query_failed "$state_json"; then
        log "unable to confirm CRI app quiet after ${reason} within ${timeout}s"
      else
        log "CRI app still active after ${reason}: pods=${pod_ids_arr[*]:-} containers=${container_ids_arr[*]:-}"
      fi
      return 1
    fi
    sleep "$delay"
  done
}

cri_assert_app_absent_via_inspect() {
  local app="$1"
  local reason="${2:-app absence check}"
  local -a matches=()
  local line

  while IFS= read -r line; do
    [[ -n "$line" ]] && matches+=("$line")
  done < <(cri_collect_matching_containers_via_inspect "$app" || true)

  if (( ${#matches[@]} == 0 )); then
    return 0
  fi

  cri_dump_app_state_debug "$app" "inspect-failure-${reason}"
  log "CRI inspect-based app absence check failed after ${reason}:"
  for line in "${matches[@]}"; do
    log "  ${line}"
  done
  return 1
}

default_runtime_name="$(cri_default_runtime_name)"
if [[ -n "$default_runtime_name" ]]; then
  log "containerd default runtime handler: ${default_runtime_name}"
fi
log "verifying CRI preflight with runtime handler=${bench_runtime_handler}"
run_cri_preflight

bench_runtime_manifest="${BENCH_APPLY_DIR}/runtime-class/${bench_app_name}-${bench_runtime_handler}.yaml"
"$python_bin" scripts/bench/pin_runtime_class.py \
  "$BENCH_PRIMARY_MANIFEST" \
  "$bench_runtime_manifest" \
  --runtime-class "$bench_runtime_handler" >/dev/null
log "using bench-local manifest override: ${bench_runtime_manifest}"

cri_wait_pod_ids_gone() {
  local -a ids=("$@")
  (( ${#ids[@]} == 0 )) && return 0
  local timeout="${CRI_POD_CLEANUP_TIMEOUT:-30}"
  local settle="${CRI_POD_CLEANUP_SETTLE:-1}"
  local deadline=$((SECONDS + timeout))
  local pending=""
  local json_out=""
  while :; do
    if ! json_out=$(cri_cmd pods -o json 2>/dev/null); then
      if (( SECONDS >= deadline )); then
        echo "[cri-refresh] warning: unable to confirm CRI pod cleanup state" >&2
        sleep "$settle"
        return 1
      fi
      sleep 1
      continue
    fi
    pending=$(printf '%s' "$json_out" | \
      "$python_bin" -c '
import json, sys
targets = set(sys.argv[1:])
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = data.get("items") or data.get("pods") or []
remaining = []
for pod in items:
    pid = pod.get("id") or pod.get("podSandboxId") or pod.get("pod_sandbox_id")
    if pid and pid in targets:
        remaining.append(pid)
print("\n".join(remaining))
' "${ids[@]}" || true)
    if [[ -z "$pending" ]]; then
      sleep "$settle"
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "[cri-refresh] warning: CRI pod cleanup still pending for: ${pending//$'\n'/ }" >&2
      sleep "$settle"
      return 1
    fi
    sleep 1
  done
}

cri_wait_container_ids_gone() {
  local -a ids=("$@")
  (( ${#ids[@]} == 0 )) && return 0
  local timeout="${CRI_POD_CLEANUP_TIMEOUT:-30}"
  local settle="${CRI_POD_CLEANUP_SETTLE:-1}"
  local deadline=$((SECONDS + timeout))
  local pending=""
  local json_out=""
  while :; do
    if ! json_out=$(cri_cmd ps -a -o json 2>/dev/null); then
      if (( SECONDS >= deadline )); then
        echo "[cri-refresh] warning: unable to confirm CRI container cleanup state" >&2
        sleep "$settle"
        return 1
      fi
      sleep 1
      continue
    fi
    pending=$(printf '%s' "$json_out" | \
      "$python_bin" -c '
import json, sys
targets = set(sys.argv[1:])
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = data.get("containers") or data.get("items") or []
remaining = []
for container in items:
    cid = container.get("id") or container.get("containerId") or container.get("container_id")
    if cid and cid in targets:
        remaining.append(cid)
print("\n".join(remaining))
' "${ids[@]}" || true)
    if [[ -z "$pending" ]]; then
      sleep "$settle"
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "[cri-refresh] warning: CRI container cleanup still pending for: ${pending//$'\n'/ }" >&2
      sleep "$settle"
      return 1
    fi
    sleep 1
  done
}

cri_has_image() {
  local ref="$1"
  if ! command -v crictl >/dev/null 2>&1; then
    return 1
  fi
  local json_out
  json_out=$(cri_cmd images -o json 2>/dev/null || true)
  if ! printf '%s' "$json_out" | "$python_bin" -c '
import json, sys
ref = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
candidates = []
for img in data.get("images", []):
    candidates.extend(img.get("repoTags") or [])
    candidates.extend(img.get("repoDigests") or [])
for tag in candidates:
    if tag == ref:
        sys.exit(0)
    if tag.startswith(ref + "@"):
        sys.exit(0)
sys.exit(1)
' "$ref"
  then
    return 1
  fi
  return 0
}

cri_pull_image() {
  local ref="$1"
  if ! command -v crictl >/dev/null 2>&1; then
    return 1
  fi
  if cri_cmd pull "$ref" >/dev/null 2>&1; then
    return 0
  fi
  if command -v ctr >/dev/null 2>&1; then
    ctr_cmd images pull "$ref" >/dev/null 2>&1 && return 0
  fi
  return 1
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
    ctr_cmd images import "$tmp" >/dev/null 2>&1 || true
    rm -f "$tmp"
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && docker image inspect "$ref" >/dev/null 2>&1; then
    tmp=$(mktemp)
    docker save "$ref" -o "$tmp" >/dev/null 2>&1 || true
    ctr_cmd images import "$tmp" >/dev/null 2>&1 || true
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
  if cri_import_image "$ref"; then
    return 0
  fi
  if cri_has_image "$ref"; then
    return 0
  fi
  if [[ "$ref" != localhost/* ]]; then
    if cri_pull_image "$ref"; then
      return 0
    fi
  fi
  return 1
}

require_cri_image() {
  local ref="$1"
  if ensure_cri_image "$ref"; then
    return 0
  fi
  echo "[cri-refresh] missing image in containerd (pull/import failed): $ref" >&2
  echo "[cri-refresh] hint: check network access or try 'sudo crictl --runtime-endpoint $AE_CRI_ENDPOINT pull $ref'" >&2
  return 1
}
cri_find_image() {
  local needle="$1"
  if ! command -v crictl >/dev/null 2>&1; then
    return 1
  fi
  cri_cmd images -o json 2>/dev/null | \
    "$python_bin" -c '
import json, sys
needle = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for img in data.get("images", []):
    for tag in img.get("repoTags") or []:
        if needle in tag:
            print(tag)
            sys.exit(0)
sys.exit(0)
' "$needle" || true
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

# Ensure sandbox + manifest + rollout images exist in containerd before running.
manifest_image=""
if [[ -f "${BENCH_PRIMARY_MANIFEST:-$APP}" ]]; then
  manifest_image=$(awk '/^[[:space:]]*image:/ {print $2; exit}' "${BENCH_PRIMARY_MANIFEST:-$APP}" | tr -d '"')
fi
manifest_image="${manifest_image:-mendhak/http-https-echo:37}"
if ! require_cri_image "$AE_CRI_SANDBOX_IMAGE"; then
  alt=$(cri_find_image 'pause:')
  if [[ -n "$alt" && "$alt" != "$AE_CRI_SANDBOX_IMAGE" ]]; then
    echo "[cri-refresh] falling back to available sandbox image: $alt" >&2
    export AE_CRI_SANDBOX_IMAGE="$alt"
    if ! require_cri_image "$AE_CRI_SANDBOX_IMAGE"; then
      exit 4
    fi
  else
    exit 4
  fi
fi
if ! require_cri_image "$manifest_image"; then
  exit 4
fi
if ! require_cri_image "$AE_ROLLOUT_IMAGE_BLUE"; then
  exit 4
fi
if ! require_cri_image "$AE_ROLLOUT_IMAGE_GREEN"; then
  exit 4
fi


cri_cleanup_app_pods() {
  local app="$1"
  if ! command -v crictl >/dev/null 2>&1; then
    return 0
  fi
  local timeout="${CRI_POD_CLEANUP_TIMEOUT:-30}"
  local settle="${CRI_POD_CLEANUP_SETTLE:-1}"
  local deadline=$((SECONDS + timeout))
  local state_json=""
  local -a pod_ids_arr=()
  local -a orphan_ids_arr=()
  local pid cid cids

  while :; do
    state_json="$(cri_collect_app_state_json "$app")"
    if cri_state_query_failed "$state_json"; then
      if (( SECONDS >= deadline )); then
        echo "[cri-refresh] unable to inspect CRI state for app=${app} within ${timeout}s" >&2
        return 1
      fi
      cri_wait_runtime_ready "CRI cleanup state refresh for app=${app}"
      sleep 1
      continue
    fi
    pod_ids_arr=()
    orphan_ids_arr=()
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && pod_ids_arr+=("$pid")
    done < <(cri_state_field_lines "pod_ids" "$state_json")
    while IFS= read -r cid; do
      [[ -n "$cid" ]] && orphan_ids_arr+=("$cid")
    done < <(cri_state_field_lines "orphan_container_ids" "$state_json")

    if (( ${#pod_ids_arr[@]} == 0 && ${#orphan_ids_arr[@]} == 0 )); then
      if (( settle > 0 )); then
        sleep "$settle"
      fi
      return 0
    fi

    local op_failed=0
    if (( ${#pod_ids_arr[@]} > 0 )); then
      echo "[cri-refresh] removing stale CRI pods for app=${app}" >&2
      for pid in "${pod_ids_arr[@]}"; do
        cids=$(cri_cmd ps -a --pod "$pid" -o json 2>/dev/null | \
          "$python_bin" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = data.get("containers") or data.get("items") or []
print("\n".join([c.get("id","") for c in items if c.get("id")]))
' || true)
        for cid in $cids; do
          if ! cri_cmd stop "$cid" >/dev/null 2>&1; then
            op_failed=1
          fi
          if ! cri_cmd rm "$cid" >/dev/null 2>&1; then
            op_failed=1
          fi
        done
        if ! cri_cmd stopp "$pid" >/dev/null 2>&1; then
          op_failed=1
        fi
        if ! cri_cmd rmp "$pid" >/dev/null 2>&1; then
          op_failed=1
        fi
      done
      if ! cri_wait_pod_ids_gone "${pod_ids_arr[@]}"; then
        op_failed=1
      fi
      state_json="$(cri_collect_app_state_json "$app")"
      if cri_state_query_failed "$state_json"; then
        op_failed=1
      fi
      orphan_ids_arr=()
      while IFS= read -r cid; do
        [[ -n "$cid" ]] && orphan_ids_arr+=("$cid")
      done < <(cri_state_field_lines "orphan_container_ids" "$state_json")
    fi

    if (( ${#orphan_ids_arr[@]} > 0 )); then
      echo "[cri-refresh] removing orphan CRI containers for app=${app}: ${#orphan_ids_arr[@]}" >&2
      for cid in "${orphan_ids_arr[@]}"; do
        if ! cri_cmd stop "$cid" >/dev/null 2>&1; then
          op_failed=1
        fi
        if ! cri_cmd rm "$cid" >/dev/null 2>&1; then
          op_failed=1
        fi
      done
      if ! cri_wait_container_ids_gone "${orphan_ids_arr[@]}"; then
        op_failed=1
      fi
    fi

    state_json="$(cri_collect_app_state_json "$app")"
    if cri_state_query_failed "$state_json"; then
      if (( SECONDS >= deadline )); then
        echo "[cri-refresh] unable to inspect CRI state for app=${app} within ${timeout}s" >&2
        return 1
      fi
      cri_wait_runtime_ready "CRI cleanup state refresh for app=${app}"
      sleep 1
      continue
    fi
    pod_ids_arr=()
    orphan_ids_arr=()
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && pod_ids_arr+=("$pid")
    done < <(cri_state_field_lines "pod_ids" "$state_json")
    while IFS= read -r cid; do
      [[ -n "$cid" ]] && orphan_ids_arr+=("$cid")
    done < <(cri_state_field_lines "orphan_container_ids" "$state_json")

    if (( ${#pod_ids_arr[@]} == 0 && ${#orphan_ids_arr[@]} == 0 )); then
      if (( settle > 0 )); then
        sleep "$settle"
      fi
      return 0
    fi

    if (( SECONDS >= deadline )); then
      if (( ${#pod_ids_arr[@]} > 0 )); then
        echo "[cri-refresh] remaining stale CRI pods for app=${app}: ${#pod_ids_arr[@]} (${pod_ids_arr[*]})" >&2
      fi
      if (( ${#orphan_ids_arr[@]} > 0 )); then
        echo "[cri-refresh] remaining orphan CRI containers for app=${app}: ${#orphan_ids_arr[@]} (${orphan_ids_arr[*]})" >&2
      fi
      return 1
    fi

    if (( op_failed > 0 )); then
      cri_wait_runtime_ready "CRI cleanup for app=${app}"
    fi
    sleep 1
  done
}

verify_snapshot_runtime_handler() {
  local label="$1"
  local csv
  csv=$(find "snapshots/${label}" -type f -path '*/raw/containers_mem.csv' | sort | tail -n1)
  if [[ -z "$csv" ]]; then
    echo "[cri-refresh] unable to locate CRI snapshot cgroup data for ${label}" >&2
    exit 4
  fi
  if ! "$python_bin" - "$csv" <<'PY'
import csv
import sys

path = sys.argv[1]
with open(path, newline="", encoding="utf-8") as fh:
    rows = list(csv.DictReader(fh))

if not rows:
    print(f"[cri-refresh] no container rows found in {path}", file=sys.stderr)
    raise SystemExit(1)

bad = [row.get("cg_path", "") for row in rows if "/k8s.io/kata" in row.get("cg_path", "")]
if bad:
    print("[cri-refresh] benchmark workload did not pin to runc; found kata cgroup paths:", file=sys.stderr)
    for item in bad:
        print(f"  {item}", file=sys.stderr)
    raise SystemExit(1)
PY
  then
    exit 4
  fi
}

cri_cleanup_app_pods "$bench_app_name"
cri_wait_runtime_ready "cleanup for app=${bench_app_name}"
cri_wait_app_quiet "$bench_app_name" "cleanup for app=${bench_app_name}"
cri_assert_app_absent_via_inspect "$bench_app_name" "cleanup for app=${bench_app_name}"

AE_ENGINE_STRICT=1 \
BENCH_IDLE_VALIDATE_ZERO_APP="$BENCH_IDLE_VALIDATE_ZERO_APP" \
LABEL_SUITE="$LABEL_CRI" \
APP="$bench_runtime_manifest" APP_NAME="$bench_app_name" \
REPLICAS="$REPLICAS" DURATION="$DURATION" AE_COLLECT_ENGINE=cri \
./scripts/bench/run_matrix.sh \
  --label-suite "$LABEL_CRI" \
  --app "$bench_runtime_manifest" \
  --app-name "$bench_app_name" \
  --replicas "$REPLICAS" \
  --duration "$DURATION" \
  --sudo
first_steady_replica="$(first_requested_replica "$REPLICAS")" || {
  log "invalid or empty REPLICAS='${REPLICAS}'"
  exit 2
}
verify_snapshot_runtime_handler "${LABEL_CRI}-pods-${first_steady_replica}"

rollout_replicas_list=""
rollout_replicas_count=0
old_ifs="$IFS"
IFS=','
for rep in $ROLL_REPLICAS; do
  rep="${rep// /}"
  [[ -z "$rep" ]] && continue
  if [[ ! "$rep" =~ ^[0-9]+$ ]]; then
    log "invalid rollout replicas '${rep}' (expected integer); aborting"
    exit 2
  fi
  rollout_replicas_list="${rollout_replicas_list}${rollout_replicas_list:+ }${rep}"
  rollout_replicas_count=$((rollout_replicas_count + 1))
done
IFS="$old_ifs"
if (( rollout_replicas_count == 0 )); then
  log "no valid rollout replicas provided (ROLL_REPLICAS='${ROLL_REPLICAS}')"
  exit 2
fi

for rep in $rollout_replicas_list; do
  delete_bench_app "$bench_app_name" "rollout replicas=${rep}"
  cri_wait_runtime_ready "delete before rollout replicas=${rep}"
  log "cleanup CRI pods before rollout replicas=${rep}"
  cri_cleanup_app_pods "$bench_app_name"
  cri_wait_runtime_ready "cleanup before rollout replicas=${rep}"
  cri_wait_app_quiet "$bench_app_name" "cleanup before rollout replicas=${rep}"
  cri_assert_app_absent_via_inspect "$bench_app_name" "cleanup before rollout replicas=${rep}"
  AE_ENGINE_STRICT=1 \
  LABEL_SUITE_ROLL="$LABEL_CRI" \
  APP="$bench_runtime_manifest" APP_NAME="$bench_app_name" \
  ROLL_REPLICAS="$rep" DURATION="$DURATION" AE_COLLECT_ENGINE=cri \
  ./scripts/bench/run_rollout_k1s.sh \
    --label-suite "$LABEL_CRI" \
    --app "$bench_runtime_manifest" \
    --app-name "$bench_app_name" \
    --replicas "$rep" \
    --duration "$DURATION" \
    --sudo
done

bench_cleanup

make bench-mem-docs
