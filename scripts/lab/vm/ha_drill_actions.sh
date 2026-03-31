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
controller_agent_port="$(echo "$variant_json" | jq -r '.k1s.agent_api_port // 9110')"
apishim_port="$(echo "$variant_json" | jq -r '.k1s.apishim_port // 8445')"
agent_token="$(echo "$variant_json" | jq -r '.k1s.agent_token // "devtoken"')"
edge_hub_leaf_host="$(echo "$variant_json" | jq -r '.transport.hub_host // empty')"
edge_hub_leaf_port="$(echo "$variant_json" | jq -r '.transport.hub_leaf_port // 7422')"
edge_nats_url="nats://gateway:dev@127.0.0.1:4223"

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

wait_for_local_tcp_port() {
  local port="$1"
  local attempts="${2:-30}"
  local delay_s="${3:-1}"
  local attempt=""
  for attempt in $(seq 1 "$attempts"); do
    if bash -lc "exec 3<>/dev/tcp/127.0.0.1/${port}" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay_s"
  done
  return 1
}

wait_for_local_process() {
  local pattern="$1"
  local attempts="${2:-30}"
  local delay_s="${3:-1}"
  local attempt=""
  for attempt in $(seq 1 "$attempts"); do
    if pgrep -f -- "$pattern" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay_s"
  done
  return 1
}

wait_for_local_etcd_health() {
  local url="${1:-http://127.0.0.1:2379/health}"
  local attempts="${2:-30}"
  local delay_s="${3:-1}"
  local attempt=""
  for attempt in $(seq 1 "$attempts"); do
    if python3 - <<'PY' "$url"
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=3.0) as resp:
        payload = json.loads(resp.read().decode("utf-8") or "{}")
except Exception:
    raise SystemExit(1)

health = str(payload.get("health") or "").strip().lower()
raise SystemExit(0 if health in {"true", "1", "ok"} else 1)
PY
    then
      return 0
    fi
    sleep "$delay_s"
  done
  return 1
}
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
controller_pattern='python3 -m ae.controller --loop --metrics-port ${controller_port}'
old_pids="\$(sudo pgrep -f -- "\$controller_pattern" | tr '\n' ' ' || true)"
if [[ -n "\$old_pids" ]]; then
  sudo pkill -TERM -f -- "\$controller_pattern" >/dev/null 2>&1 || true
fi
drain_deadline=\$((SECONDS + 45))
while (( SECONDS < drain_deadline )); do
  port_busy=0
  if ss -ltn | awk '\$4 ~ /:${controller_port}\$/ {found=1} END {exit(found?0:1)}'; then
    port_busy=1
  fi
  stale_pid=0
  for pid in \$old_pids; do
    if sudo kill -0 "\$pid" >/dev/null 2>&1; then
      stale_pid=1
      break
    fi
  done
  if (( stale_pid == 0 && port_busy == 0 )); then
    break
  fi
  sleep 1
done
if [[ -n "\$old_pids" ]]; then
  for pid in \$old_pids; do
    if sudo kill -0 "\$pid" >/dev/null 2>&1; then
      sudo kill -KILL "\$pid" >/dev/null 2>&1 || true
    fi
  done
fi
if ss -ltn | awk '\$4 ~ /:${controller_port}\$/ {found=1} END {exit(found?0:1)}'; then
  echo "controller port ${controller_port} is still busy after stop attempt" >&2
  exit 1
fi
nohup sudo env \
  PYTHONPATH=/mnt/host/src \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_RUNTIME_HANDLER=\${AE_CRI_RUNTIME_HANDLER:-runc} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  AE_CRI_REGISTRY_TRUST_SYSTEM=\${AE_CRI_REGISTRY_TRUST_SYSTEM:-1} \
  AE_CRI_REGISTRY_PRELOAD=\${AE_CRI_REGISTRY_PRELOAD:-1} \
  DEV_PROFILE_DIR=/mnt/host/state/profiles/k1s-ha-core \
  AE_PROJECTION_ROOT=/mnt/host/state/profiles/k1s-ha-core/projections \
  AE_APISHIM_MODE=\${AE_APISHIM_MODE:-cri} \
  AE_APISHIM_PRESEEDED=1 \
  AE_STATE_BACKEND=\${AE_STATE_BACKEND:-etcd} \
  AE_TRANSPORT_BACKEND=\${AE_TRANSPORT_BACKEND:-nats-js} \
  AE_JS_DOMAIN=\${AE_JS_DOMAIN:-K1S} \
  AE_NODE_PROFILE=\${AE_NODE_PROFILE:-k1s-ha-core} \
  AE_EDGE_INGRESS_MODE=\${AE_EDGE_INGRESS_MODE:-core-proxy} \
  AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS=\${AE_EDGE_INGRESS_TRANSLATE_APP_INGRESS:-1} \
  AE_EDGE_INGRESS_CONFIG_DIR=\${AE_EDGE_INGRESS_CONFIG_DIR:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress} \
  AE_EDGE_INGRESS_ENVOY_CONFIG=\${AE_EDGE_INGRESS_ENVOY_CONFIG:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/envoy.yaml} \
  AE_RATHOLE_SERVER_CONFIG=\${AE_RATHOLE_SERVER_CONFIG:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/rathole-server.toml} \
  AE_RATHOLE_CLIENT_DIR=\${AE_RATHOLE_CLIENT_DIR:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/clients} \
  AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX=\${AE_EDGE_INGRESS_SITE_DOMAIN_SUFFIX:-edge.local} \
  AE_EDGE_INGRESS_LOCAL_ADDR=\${AE_EDGE_INGRESS_LOCAL_ADDR:-127.0.0.1:18081} \
  AE_EDGE_INGRESS_HTTP_PORT=\${AE_EDGE_INGRESS_HTTP_PORT:-10080} \
  AE_EDGE_INGRESS_TLS_PORT=\${AE_EDGE_INGRESS_TLS_PORT:-10443} \
  AE_EDGE_INGRESS_CORE_PROXY=\${AE_EDGE_INGRESS_CORE_PROXY:-1} \
  AE_EDGE_INGRESS_RATHOLE_RELOAD=\${AE_EDGE_INGRESS_RATHOLE_RELOAD:-1} \
  AE_EDGE_INGRESS_RELOAD_CMD="python3 /mnt/host/scripts/dev/cri_stack.py up-envoy --profile k1s-ha-core --config \${AE_EDGE_INGRESS_ENVOY_CONFIG:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/envoy.yaml}" \
  AE_EDGE_INGRESS_RATHOLE_RELOAD_CMD="python3 /mnt/host/scripts/dev/cri_stack.py up-rathole-server --profile k1s-ha-core --config \${AE_RATHOLE_SERVER_CONFIG:-/mnt/host/state/profiles/k1s-ha-core/edge-ingress/rathole-server.toml}" \
  AE_RATHOLE_BIND_ADDR=\${AE_RATHOLE_BIND_ADDR:-0.0.0.0:2333} \
  AE_RATHOLE_DEFAULT_TOKEN=\${AE_RATHOLE_DEFAULT_TOKEN:-dev} \
  AE_RATHOLE_SERVER_ADDR=\${AE_RATHOLE_SERVER_ADDR:-127.0.0.1:2333} \
  AE_CONTROLPLANE_PUBLIC_ENABLE=\${AE_CONTROLPLANE_PUBLIC_ENABLE:-1} \
  AE_CONTROLPLANE_DASH_HOST=\${AE_CONTROLPLANE_DASH_HOST:-dash.home.arpa} \
  AE_CONTROLPLANE_DOCS_HOST=\${AE_CONTROLPLANE_DOCS_HOST:-docs.home.arpa} \
  AE_CONTROLPLANE_API_HOST=\${AE_CONTROLPLANE_API_HOST:-api.home.arpa} \
  AE_CONTROLPLANE_PROXY_ADDR=\${AE_CONTROLPLANE_PROXY_ADDR:-127.0.0.1} \
  AE_CONTROLPLANE_PROXY_PORT=\${AE_CONTROLPLANE_PROXY_PORT:-10081} \
  AE_CONTROLPLANE_CONTROLLER_UPSTREAM=\${AE_CONTROLPLANE_CONTROLLER_UPSTREAM:-127.0.0.1:${controller_port}} \
  AE_CONTROLPLANE_API_CONTROLLER_UPSTREAM=\${AE_CONTROLPLANE_API_CONTROLLER_UPSTREAM:-127.0.0.1:${controller_port}} \
  AE_CONTROLPLANE_APISHIM_UPSTREAM=\${AE_CONTROLPLANE_APISHIM_UPSTREAM:-127.0.0.1:${apishim_port}} \
  AE_CONTROLPLANE_API_APISHIM_UPSTREAM=\${AE_CONTROLPLANE_API_APISHIM_UPSTREAM:-127.0.0.1:${apishim_port}} \
  AE_CONTROLPLANE_API_APISHIM_TLS=\${AE_CONTROLPLANE_API_APISHIM_TLS:-1} \
  AE_ETCD_MAINTENANCE_ENABLE=\${AE_ETCD_MAINTENANCE_ENABLE:-0} \
  AE_ETCD_MAINTENANCE_THRESHOLD_PCT=\${AE_ETCD_MAINTENANCE_THRESHOLD_PCT:-80} \
  APISHIM_HOST=\${APISHIM_HOST:-0.0.0.0} \
  AE_HA_MODE=1 \
  AE_AGENT_API_PORT=${controller_agent_port} \
  AE_AGENT_API_TOKEN=${agent_token} \
  AE_CONTROLLER_ID=${node_id} \
  AE_CONTROLLER_ADVERTISE_ADDR=http://${ip}:${controller_port} \
  AE_ETCD_ENDPOINTS='${ha_etcd_endpoints}' \
  AE_APISHIM_ETCD_ENDPOINTS='${ha_etcd_endpoints}' \
  AE_ETCD_PREFIX='${ha_etcd_prefix}' \
  AE_NATS_URL='${ha_nats_url}' \
  APISHIM_PORT=${apishim_port} \
  python3 -m ae.controller --loop --metrics-port ${controller_port} > /home/ae/k1s-ha-core.log 2>&1 </dev/null &
new_pid=\$!
disown || true
wait_for_local_process "\$new_pid" 45 1 || {
  echo "controller process exited early; tailing /home/ae/k1s-ha-core.log" >&2
  tail -n 80 /home/ae/k1s-ha-core.log >&2 || true
  exit 1
}
wait_for_local_tcp_port ${controller_port} 45 1 || {
  echo "controller restart was not observed on port ${controller_port}" >&2
  exit 1
}
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
wait_for_local_etcd_health http://127.0.0.1:2379/health 45 1 || {
  echo "etcd restart did not become healthy in time" >&2
  exit 1
}
echo etcd-restart-triggered
EOF
}

build_edge_transport_restart_script() {
  local site_id="$1"
  local node_id="$2"
  cat <<EOF
$(emit_runtime_preamble)
cd /mnt/host
sudo pkill -f -- 'ae\.gateway' >/dev/null 2>&1 || true
sleep 2
sudo mkdir -p /var/lib/ae/gateway
nohup sudo env \
  PYTHONPATH=/mnt/host/src \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_DATA_ROOT=\${AE_CRI_DATA_ROOT:-/var/lib/ae/cri} \
  AE_CRI_RUNTIME_HANDLER=\${AE_CRI_RUNTIME_HANDLER:-runc} \
  AE_CRI_IMAGE_POLICY=\${AE_CRI_IMAGE_POLICY:-pull} \
  EDGE_PROFILE=\${EDGE_PROFILE:-k1s-core} \
  AE_TRANSPORT_BACKEND=\${AE_TRANSPORT_BACKEND:-nats-js} \
  AE_JS_DOMAIN=\${AE_JS_DOMAIN:-K1S} \
  AE_SITE_ID=${site_id} \
  AE_NODE_ID=${node_id} \
  AE_NODE_LABELS=\${AE_NODE_LABELS:-role=gateway,profile=k1s-core} \
  AE_NATS_URL=\${AE_NATS_URL:-${edge_nats_url}} \
  AE_GATEWAY_SPOOL_PATH=/var/lib/ae/gateway/gateway-${site_id}-${node_id}.db \
  AE_GATEWAY_FENCE_DB=/var/lib/ae/gateway/fence-${site_id}-${node_id}.db \
  AE_NATS_HUB_LEAF_HOST=${edge_hub_leaf_host} \
  AE_NATS_HUB_LEAF_PORT=${edge_hub_leaf_port} \
  python3 -m ae.gateway > /home/ae/k1s-edge-core.log 2>&1 </dev/null &
disown || true
wait_for_local_process 'python(3)? -m ae\.gateway' 45 1 || {
  echo "gateway restart was not observed" >&2
  exit 1
}
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
