#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<USAGE
Usage: $0 <leader-failover|etcd-restart|transport-recovery> --variant <path> [--site <id>] [--dry-run]

Topology-aware VM drill trigger for the checked-in HA lab variants.
The script resolves the target host(s) from the variant and executes the
disruptive action over SSH against the guest VM(s).
USAGE
}

ACTION="${1:-}"
if [[ -z "$ACTION" || "$ACTION" == "-h" || "$ACTION" == "--help" ]]; then
  usage
  exit 0
fi
shift

VARIANT=""
SITE_ID=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --site) SITE_ID="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      err "unknown arg: $1"
      usage
      exit 2
      ;;
  esac
done

case "$ACTION" in
  leader-failover|etcd-restart|transport-recovery) ;;
  *)
    err "unsupported action: $ACTION"
    usage
    exit 2
    ;;
esac

[[ -n "$VARIANT" ]] || { err "--variant required"; exit 2; }
ensure_ssh_key

variant_json="$(variant_to_json "$VARIANT")"
python_bin="$(lab_python)"

ha_etcd_endpoints="$(
  echo "$variant_json" | jq -r '.ha.etcd_endpoints[]?' | paste -sd, -
)"
ha_etcd_prefix="$(echo "$variant_json" | jq -r '.ha.etcd_prefix // empty')"
ha_nats_url="$(echo "$variant_json" | jq -r '.ha.nats_url // empty')"
controller_port="$(echo "$variant_json" | jq -r '.k1s.controller_port // 9108')"
apishim_port="$(echo "$variant_json" | jq -r '.k1s.apishim_port // 8445')"
edge_hub_leaf_host="$(echo "$variant_json" | jq -r '.transport.hub_host // empty')"
edge_hub_leaf_port="$(echo "$variant_json" | jq -r '.transport.hub_leaf_port // 7422')"

wait_for_host() {
  local name="$1"
  local ip="$2"
  if ! wait_for_ssh "$ip" 80; then
    err "ssh not ready for ${name} (${ip})"
    exit 1
  fi
}

emit_runtime_preamble() {
  cat <<'PRELUDE'
export AE_CRI_ENDPOINT=${AE_CRI_ENDPOINT:-unix:///run/containerd/containerd.sock}
sudo mkdir -p /mnt/host
sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
source /mnt/host/scripts/lab/vm/lib/guest_prereqs.sh
ensure_vm_bootstrap_prereqs
PRELUDE
}

run_remote_script() {
  local name="$1"
  local ip="$2"
  local script="$3"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "dry-run ${ACTION} target=${name} ip=${ip}"
    printf '%s\n' "$script"
    return 0
  fi
  wait_for_host "$name" "$ip"
  run_remote "$ip" "bash -s" <<<"$script"
}

current_leader_host_json() {
  local leader_id
  leader_id="$(
    PYTHONPATH="$ROOT_DIR/src" "$python_bin" - <<'PY' "$ha_etcd_endpoints" "$ha_etcd_prefix"
from __future__ import annotations

import sys

from ae.ha.ops import read_etcd_leader, split_csv

endpoints = split_csv(sys.argv[1])
prefix = sys.argv[2]
leader = read_etcd_leader(endpoints, prefix, timeout_s=3.0)
print("" if leader is None else leader.controller_id)
PY
  )"
  [[ -n "$leader_id" ]] || {
    err "could not resolve current controller leader from etcd"
    exit 1
  }
  echo "$variant_json" | jq -c --arg leader_id "$leader_id" '
    .hosts[]
    | select(.role == "k1s-ha-core" and (.node_id // .name) == $leader_id)
  ' | head -n1
}

first_core_host_json() {
  echo "$variant_json" | jq -c '.hosts[] | select(.role == "k1s-ha-core")' | head -n1
}

edge_host_json() {
  if [[ -n "$SITE_ID" ]]; then
    echo "$variant_json" | jq -c --arg site "$SITE_ID" '
      .hosts[]
      | select(.role == "k1s-edge-core" and (.site_id // "") == $site)
    ' | head -n1
  else
    echo "$variant_json" | jq -c '.hosts[] | select(.role == "k1s-edge-core")' | head -n1
  fi
}

build_ha_core_restart_script() {
  local ip="$1"
  local node_id="$2"
  cat <<EOF
$(emit_runtime_preamble)
cd /mnt/host
sudo pkill -f 'ae.controller --loop --metrics-port' >/dev/null 2>&1 || true
sleep 2
nohup sudo env \
  PYTHON_BIN=python3 \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_RUNTIME_HANDLER=runc \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  AE_APISHIM_MODE=\${AE_APISHIM_MODE:-cri} \
  AE_APISHIM_PRESEEDED=1 \
  APISHIM_HOST=\${APISHIM_HOST:-0.0.0.0} \
  AE_HA_MODE=1 \
  AE_CONTROLLER_ID=${node_id} \
  AE_CONTROLLER_ADVERTISE_ADDR=http://${ip}:${controller_port} \
  AE_ETCD_ENDPOINTS='${ha_etcd_endpoints}' \
  AE_APISHIM_ETCD_ENDPOINTS='${ha_etcd_endpoints}' \
  AE_ETCD_PREFIX='${ha_etcd_prefix}' \
  AE_NATS_URL='${ha_nats_url}' \
  APISHIM_PORT=${apishim_port} \
  make k1s-ha-core > /home/ae/k1s-ha-core.log 2>&1 </dev/null &
disown || true
echo leader-failover-triggered
EOF
}

build_etcd_restart_script() {
  local ip="$1"
  local node_id="$2"
  local initial_cluster
  initial_cluster="$(
    echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-ha-core") | "\(.node_id // .name)=http://\(.ip):2380"' | paste -sd, -
  )"
  cat <<EOF
$(emit_runtime_preamble)
sudo env \
  PYTHONPATH=/mnt/host/src \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_DATA_ROOT=/var/lib/ae/cri \
  AE_CRI_RUNTIME_HANDLER=\${AE_CRI_RUNTIME_HANDLER:-runc} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  python3 /mnt/host/scripts/dev/cri_stack.py up-etcd \
    --profile k1s-ha-core \
    --name ${node_id} \
    --component k1s-ha-core-etcd \
    --listen-client-urls http://0.0.0.0:2379 \
    --advertise-client-urls http://${ip}:2379 \
    --listen-peer-urls http://0.0.0.0:2380 \
    --initial-advertise-peer-urls http://${ip}:2380 \
    --initial-cluster '${initial_cluster}' \
    --initial-cluster-state new \
    --data-dir-name ha-etcd \
    --recreate
echo etcd-restart-triggered
EOF
}

build_edge_transport_restart_script() {
  local site_id="$1"
  local node_id="$2"
  cat <<EOF
$(emit_runtime_preamble)
cd /mnt/host
sudo pkill -f 'ae.gateway' >/dev/null 2>&1 || true
sleep 2
sudo mkdir -p /var/lib/ae/gateway
nohup sudo env \
  PYTHON_BIN=python3 \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_SITE_ID=${site_id} \
  AE_NODE_ID=${node_id} \
  AE_GATEWAY_SPOOL_PATH=/var/lib/ae/gateway/gateway-${site_id}-${node_id}.db \
  AE_GATEWAY_FENCE_DB=/var/lib/ae/gateway/fence-${site_id}-${node_id}.db \
  AE_NATS_HUB_LEAF_HOST=${edge_hub_leaf_host} \
  AE_NATS_HUB_LEAF_PORT=${edge_hub_leaf_port} \
  make k1s-edge-core-cri > /home/ae/k1s-edge-core.log 2>&1 </dev/null &
disown || true
echo transport-recovery-triggered
EOF
}

case "$ACTION" in
  leader-failover)
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "dry-run leader-failover target=current controller leader via etcd"
      exit 0
    fi
    host_json="$(current_leader_host_json)"
    [[ -n "$host_json" ]] || {
      err "could not map current controller leader to a k1s-ha-core host"
      exit 1
    }
    name="$(echo "$host_json" | jq -r '.name')"
    ip="$(echo "$host_json" | jq -r '.ip')"
    node_id="$(echo "$host_json" | jq -r '.node_id // .name')"
    run_remote_script "$name" "$ip" "$(build_ha_core_restart_script "$ip" "$node_id")"
    ;;
  etcd-restart)
    host_json="$(first_core_host_json)"
    [[ -n "$host_json" ]] || {
      err "variant does not include a k1s-ha-core host"
      exit 1
    }
    name="$(echo "$host_json" | jq -r '.name')"
    ip="$(echo "$host_json" | jq -r '.ip')"
    node_id="$(echo "$host_json" | jq -r '.node_id // .name')"
    run_remote_script "$name" "$ip" "$(build_etcd_restart_script "$ip" "$node_id")"
    ;;
  transport-recovery)
    host_json="$(edge_host_json)"
    [[ -n "$host_json" ]] || {
      err "variant does not include a matching k1s-edge-core host"
      exit 1
    }
    name="$(echo "$host_json" | jq -r '.name')"
    ip="$(echo "$host_json" | jq -r '.ip')"
    site_id="$(echo "$host_json" | jq -r '.site_id // empty')"
    node_id="$(echo "$host_json" | jq -r '.node_id // .name')"
    [[ -n "$site_id" ]] || {
      err "target edge host is missing site_id"
      exit 1
    }
    run_remote_script "$name" "$ip" "$(build_edge_transport_restart_script "$site_id" "$node_id")"
    ;;
esac
