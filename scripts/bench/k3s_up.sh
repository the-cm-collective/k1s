#!/usr/bin/env bash
set -euo pipefail

# Bring up a k3s cluster via k3d for local benchmarking.
# Exposes ports 80/443 via the load balancer so Ingress works.
#
# Usage:
#   scripts/bench/k3s_up.sh            # create cluster 'bench'
#   scripts/bench/k3s_up.sh --down     # delete cluster 'bench'

name="bench"
action="up"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) name="$2"; shift 2;;
    --down) action="down"; shift;;
    *) echo "unknown arg: $1"; exit 2;;
  esac
done

if ! command -v k3d >/dev/null 2>&1; then
  echo "k3d not found. Install from https://k3d.io/ (e.g., curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash)" >&2
  exit 2
fi

if [[ "$action" == "down" ]]; then
  k3d cluster delete "$name" || true
  exit 0
fi

if k3d cluster list | grep -q "^${name}\b"; then
  echo "[k3d] cluster '${name}' already exists" >&2
else
  echo "[k3d] creating cluster '${name}'" >&2
  k3d cluster create "$name" \
    --agents 1 \
    --port "80:80@loadbalancer" \
    --port "443:443@loadbalancer" \
    --wait
fi

echo "[k3d] kubeconfig: $(k3d kubeconfig get "$name" | wc -l) lines" >&2
echo "[k3d] done" >&2

