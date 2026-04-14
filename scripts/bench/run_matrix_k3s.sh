#!/usr/bin/env bash
set -euo pipefail

# Run idle + scale-out snapshots against a k3s cluster (via k3d).
# Requires: k3d cluster created (scripts/bench/k3s_up.sh) and kubectl in PATH.

label_suite="baseline"
manifest="specs/examples/k3s-echo.yaml"
replicas_csv="1,5,10"
duration=30
ns="default"
app_name="echo"
use_sudo=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label-suite) label_suite="$2"; shift 2;;
    --manifest) manifest="$2"; shift 2;;
    --replicas) replicas_csv="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --namespace) ns="$2"; shift 2;;
    --app-name) app_name="$2"; shift 2;;
    --sudo) use_sudo=1; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

require() { if ! command -v "$1" >/dev/null 2>&1; then echo "missing: $1" >&2; exit 2; fi; }
require kubectl
sudo_env_base=(
  "HOME=/root"
  "XDG_RUNTIME_DIR=/run/user/0"
  "DBUS_SESSION_BUS_ADDRESS="
  "CONTAINER_HOST="
  "PODMAN_HOST="
)
sudo_env_snapshot=(
  "${sudo_env_base[@]}"
  "AE_RUNTIME_BACKEND=${AE_RUNTIME_BACKEND:-podman}"
  "AE_OCI_RUNTIME=${AE_OCI_RUNTIME:-}"
  "AE_PODMAN_BIN=${AE_PODMAN_BIN:-podman}"
  "AE_COLLECT_ENGINE=${AE_COLLECT_ENGINE:-}"
  "AE_COLLECT_PODMAN_SUDO=${AE_COLLECT_PODMAN_SUDO:-}"
  "AE_PODMAN_SUDO=${AE_PODMAN_SUDO:-}"
  "AE_ENGINE_STRICT=${AE_ENGINE_STRICT:-0}"
  "AE_SNAPSHOT_TRACE=${AE_SNAPSHOT_TRACE:-0}"
)

info(){ echo "[k3s-matrix] $*" >&2; }

current_pod_uids() {
  local selector="${AE_K3S_POD_SELECTOR:-app=${app_name}}"
  kubectl -n "$ns" get pods -l "$selector" -o jsonpath='{range .items[*]}{.metadata.uid}{","}{end}' 2>/dev/null | sed 's/,$//'
}

ensure_kube() {
  if kubectl cluster-info >/dev/null 2>&1; then
    return 0
  fi
  echo "[k3s-matrix] kubectl cannot reach a cluster. Create one with 'make bench-k3s-up' (k3d required)." >&2
  exit 2
}

if [[ "${SKIP_GUARDS:-0}" != "1" ]]; then
  ensure_kube
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[k3s-matrix] docker not found; snapshots will skip container cgroup metrics." >&2
fi

wait_ready() {
  local dep="$1"; local want="$2"
  local default_tries=60
  if [[ "$label_suite" =~ ^r[0-9]{8} ]]; then
    default_tries=180
  fi
  local tries=${WAIT_READY_TRIES:-$default_tries}
  while (( tries-- > 0 )); do
    local rdy
    rdy=$(kubectl -n "$ns" get deploy "$dep" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
    [[ -z "$rdy" ]] && rdy=0
    if [[ "$rdy" == "$want" ]]; then return 0; fi
    sleep 2
  done
  echo "timeout waiting for $dep ready=$want" >&2
  return 1
}

# Idle snapshot (no app deployed)
info "idle snapshot"
if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
  if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
    sudo env "${sudo_env_snapshot[@]}" AE_K3S_POD_UIDS="$(current_pod_uids)" scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-idle" --duration "$duration"
  else
    sudo env "${sudo_env_snapshot[@]}" AE_K3S_POD_UIDS="$(current_pod_uids)" scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-idle" --duration "$duration" || true
  fi
else
  if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
    AE_K3S_POD_UIDS="$(current_pod_uids)" scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-idle" --duration "$duration"
  else
    AE_K3S_POD_UIDS="$(current_pod_uids)" scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-idle" --duration "$duration" || true
  fi
fi

info "apply manifest: $manifest"
kubectl -n "$ns" apply -f "$manifest"

IFS=',' read -r -a reps <<< "$replicas_csv"
for n in "${reps[@]}"; do
  n=${n// /}
  [[ -z "$n" ]] && continue
  info "scale ${app_name} to $n"
  kubectl -n "$ns" scale deploy "$app_name" --replicas "$n"
  wait_ready "$app_name" "$n"
  info "snapshot label=${label_suite}-pods-${n}"
  if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      sudo env "${sudo_env_snapshot[@]}" AE_K3S_POD_UIDS="$(current_pod_uids)" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-pods-${n}" --duration "$duration"
    else
      sudo env "${sudo_env_snapshot[@]}" AE_K3S_POD_UIDS="$(current_pod_uids)" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-pods-${n}" --duration "$duration" || true
    fi
  else
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      AE_K3S_POD_UIDS="$(current_pod_uids)" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-pods-${n}" --duration "$duration"
    else
      AE_K3S_POD_UIDS="$(current_pod_uids)" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-pods-${n}" --duration "$duration" || true
    fi
  fi
done

info "done"
