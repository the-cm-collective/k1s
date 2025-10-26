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

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label-suite) label_suite="$2"; shift 2;;
    --manifest) manifest="$2"; shift 2;;
    --replicas) replicas_csv="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --namespace) ns="$2"; shift 2;;
    --app-name) app_name="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

require() { if ! command -v "$1" >/dev/null 2>&1; then echo "missing: $1" >&2; exit 2; fi; }
require kubectl

info(){ echo "[k3s-matrix] $*" >&2; }

ensure_kube() {
  if kubectl cluster-info >/dev/null 2>&1; then
    return 0
  fi
  echo "[k3s-matrix] kubectl cannot reach a cluster. Create one with 'make bench-k3s-up' (k3d required)." >&2
  exit 2
}

ensure_kube

wait_ready() {
  local dep="$1"; local want="$2"; local tries=60
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
scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-idle" --duration "$duration" || true

info "apply manifest: $manifest"
kubectl -n "$ns" apply -f "$manifest"

IFS=',' read -r -a reps <<< "$replicas_csv"
for n in "${reps[@]}"; do
  n=${n// /}
  [[ -z "$n" ]] && continue
  info "scale ${app_name} to $n"
  kubectl -n "$ns" scale deploy "$app_name" --replicas "$n"
  wait_ready "$app_name" "$n" || true
  info "snapshot label=${label_suite}-pods-${n}"
  scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-pods-${n}" --duration "$duration" || true
done

info "done"
