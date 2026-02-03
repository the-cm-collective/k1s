#!/usr/bin/env bash
set -euo pipefail

# Trigger a rolling update for a k3s Deployment and capture memory snapshots
# during and after the rollout.

label_suite="baseline-roll"
deploy="echo"
namespace="default"
replicas_csv="2,5"
duration=30
use_sudo=0
old_tag="0.7.0"
new_tag="0.9.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label-suite) label_suite="$2"; shift 2;;
    --deploy) deploy="$2"; shift 2;;
    --namespace) namespace="$2"; shift 2;;
    --replicas) replicas_csv="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --sudo) use_sudo=1; shift;;
    --old-tag) old_tag="$2"; shift 2;;
    --new-tag) new_tag="$2"; shift 2;;
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

info(){ echo "[k3s-rollout] $*" >&2; }

ensure_kube() {
  if kubectl cluster-info >/dev/null 2>&1; then
    return 0
  fi
  echo "[k3s-rollout] kubectl cannot reach a cluster. Create one with 'make bench-k3s-up' (k3d required)." >&2
  exit 2
}

if [[ "${SKIP_GUARDS:-0}" != "1" ]]; then
  ensure_kube
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[k3s-rollout] docker not found; snapshots will skip container cgroup metrics." >&2
fi

wait_ready() {
  local dep="$1"; local want="$2"; local tries=120
  while (( tries-- > 0 )); do
    local rdy
    rdy=$(kubectl -n "$namespace" get deploy "$dep" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
    [[ -z "$rdy" ]] && rdy=0
    if [[ "$rdy" == "$want" ]]; then return 0; fi
    sleep 2
  done
  echo "timeout waiting for $dep ready=$want" >&2
  return 1
}

rollout_replicas=()
IFS=',' read -r -a rollout_replicas_raw <<< "$replicas_csv"
for rep in "${rollout_replicas_raw[@]}"; do
  rep="${rep// /}"
  [[ -z "$rep" ]] && continue
  if [[ ! "$rep" =~ ^[0-9]+$ ]]; then
    echo "[k3s-rollout] invalid replicas '${rep}' (expected integer); skipping" >&2
    continue
  fi
  rollout_replicas+=("$rep")
done
if (( ${#rollout_replicas[@]} == 0 )); then
  echo "[k3s-rollout] no valid replicas provided (got: '${replicas_csv}')" >&2
  exit 2
fi

run_rollout_once() {
  local replicas="$1"

  info "scale ${deploy} to ${replicas}"
  kubectl -n "$namespace" scale deploy "$deploy" --replicas "$replicas"
  wait_ready "$deploy" "$replicas" || true

  local cur
  local target
  cur=$(kubectl -n "$namespace" get deploy "$deploy" -o jsonpath='{.spec.template.spec.containers[0].image}')
  target="$cur"
  if [[ "$cur" == *":"$old_tag ]]; then target=${cur/%:$old_tag/:$new_tag}; else target=${cur/%:$new_tag/:$old_tag}; fi

  info "set image to ${target} and snapshot DURING"
  kubectl -n "$namespace" set image deploy/"$deploy" "$deploy"="$target"
  if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      sudo env "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration"
    else
      sudo env "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration" || true
    fi
  else
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration"
    else
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration" || true
    fi
  fi

  info "wait ready and snapshot POST"
  wait_ready "$deploy" "$replicas" || true
  if (( use_sudo )) && command -v sudo >/dev/null 2>&1; then
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      sudo env "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration"
    else
      sudo env "${sudo_env_snapshot[@]}" AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration" || true
    fi
  else
    if [[ "${AE_ENGINE_STRICT:-0}" == "1" ]]; then
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration"
    else
      AE_REQUIRE_CONTAINERS=1 scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration" || true
    fi
  fi
}

for replicas in "${rollout_replicas[@]}"; do
  run_rollout_once "$replicas"
done

info "done"
