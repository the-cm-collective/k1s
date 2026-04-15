#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "${ROOT_DIR}/scripts/lib/nixos_bridge.sh"

pod_cidr="${AE_POD_CIDR:-}"
if [[ -z "$pod_cidr" ]]; then
  echo "error: AE_POD_CIDR is required" >&2
  exit 2
fi

export AE_CNI_SUBNET="${AE_CNI_SUBNET:-$pod_cidr}"
export AE_CNI_FORCE="${AE_CNI_FORCE:-1}"
export AE_CNI_BRIDGE_NAME="${AE_CNI_BRIDGE_NAME:-cni0}"

if k1s_is_nixos /etc/os-release; then
  resolved_cni_env="$(k1s_containerd_cni_env || true)"
  if [[ -n "$resolved_cni_env" ]]; then
    eval "$resolved_cni_env"
  fi
else
  bash "${ROOT_DIR}/scripts/cni_bin_bootstrap.sh"
fi

bash "${ROOT_DIR}/scripts/cni_init.sh"
