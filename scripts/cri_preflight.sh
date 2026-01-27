#!/usr/bin/env bash
set -euo pipefail

endpoint="${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}"

if [[ "$endpoint" == unix://* ]]; then
  sock="${endpoint#unix://}"
  if [[ ! -S "$sock" ]]; then
    echo "CRI socket not found: $sock" >&2
    exit 1
  fi
else
  echo "Non-unix CRI endpoint configured: $endpoint"
fi

if ! command -v crictl >/dev/null 2>&1; then
  echo "crictl not found; install for debugging" >&2
else
  echo "crictl: $(command -v crictl)"
fi

if [[ "${AE_ENABLE_SERVICE_PROXY:-0}" == "1" ]]; then
  provider="${AE_SERVICE_PROVIDER:-iptables}"
  if [[ "$provider" == "iptables" || "$provider" == "kubeproxy" || "$provider" == "cri" ]]; then
    ipt="${AE_IPTABLES_BIN:-iptables}"
    if ! command -v "$ipt" >/dev/null 2>&1; then
      echo "iptables not found; Service VIP proxy requires $ipt on PATH" >&2
    fi
    if [[ "${EUID}" -ne 0 ]]; then
      echo "Service VIP proxy requires root (iptables NAT rules)" >&2
    fi
  fi
fi

cni_bin="${CNI_BIN_DIR:-/opt/cni/bin}"
cni_conf="${CNI_CONF_DIR:-/etc/cni/net.d}"
if [[ ! -d "$cni_bin" ]]; then
  echo "CNI bin dir missing: $cni_bin" >&2
  exit 1
fi
if [[ ! -d "$cni_conf" ]]; then
  echo "CNI config dir missing: $cni_conf" >&2
  exit 1
fi

echo "CRI preflight OK"
