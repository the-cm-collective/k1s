#!/usr/bin/env bash
set -euo pipefail

# Trigger a rolling update for a k3s Deployment and capture memory snapshots
# during and after the rollout.

label_suite="baseline-roll"
deploy="echo"
namespace="default"
replicas=5
duration=30
old_tag="0.7.0"
new_tag="0.9.0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --label-suite) label_suite="$2"; shift 2;;
    --deploy) deploy="$2"; shift 2;;
    --namespace) namespace="$2"; shift 2;;
    --replicas) replicas="$2"; shift 2;;
    --duration) duration="$2"; shift 2;;
    --old-tag) old_tag="$2"; shift 2;;
    --new-tag) new_tag="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

require() { if ! command -v "$1" >/dev/null 2>&1; then echo "missing: $1" >&2; exit 2; fi; }
require kubectl

info(){ echo "[k3s-rollout] $*" >&2; }

ensure_kube() {
  if kubectl cluster-info >/dev/null 2>&1; then
    return 0
  fi
  echo "[k3s-rollout] kubectl cannot reach a cluster. Create one with 'make bench-k3s-up' (k3d required)." >&2
  exit 2
}

ensure_kube

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

info "scale ${deploy} to ${replicas}"
kubectl -n "$namespace" scale deploy "$deploy" --replicas "$replicas"
wait_ready "$deploy" "$replicas" || true

cur=$(kubectl -n "$namespace" get deploy "$deploy" -o jsonpath='{.spec.template.spec.containers[0].image}')
target="$cur"
if [[ "$cur" == *":"$old_tag ]]; then target=${cur/%:$old_tag/:$new_tag}; else target=${cur/%:$new_tag/:$old_tag}; fi

info "set image to ${target} and snapshot DURING"
kubectl -n "$namespace" set image deploy/"$deploy" "$deploy"="$target"
scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-during" --duration "$duration" || true

info "wait ready and snapshot POST"
wait_ready "$deploy" "$replicas" || true
scripts/bench/mem_snapshot.sh --mode k3s --label "${label_suite}-rollout-${replicas}-post" --duration "$duration" || true

info "done"
