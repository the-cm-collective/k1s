#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

RUN_ID="$(resolve_run_id)"
VARIANT=""
PURGE=0
DESTROY_NETWORK=0
BEST_EFFORT=0
FORWARD_CHAIN="${FORWARD_CHAIN:-K1S_VM_FORWARD}"

state_dir=""
inventory=""
run_inventory=""
variant_json=""
bridge=""
cidr=""
INVENTORY_MODE=""

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>] [--purge] [--destroy-network] [--best-effort]
USAGE
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --variant) VARIANT="$2"; shift 2 ;;
      --run-id) RUN_ID="$2"; shift 2 ;;
      --purge) PURGE=1; shift ;;
      --destroy-network) DESTROY_NETWORK=1; shift ;;
      --best-effort) BEST_EFFORT=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) err "unknown arg: $1"; usage; exit 2 ;;
    esac
  done
}

load_variant_context() {
  [[ -n "$VARIANT" ]] || { err "--variant is required"; usage; exit 2; }
  variant_json="$(variant_to_json "$VARIANT")"
  bridge="$(echo "$variant_json" | jq -r '.network.bridge')"
  cidr="$(echo "$variant_json" | jq -r '.network.cidr')"

  state_dir="$ROOT_DIR/state/lab-vm/$RUN_ID"
  inventory="$state_dir/inventory.json"
  run_inventory="$(run_dir "$RUN_ID")/qemu_inventory.json"
}

resolve_inventory_mode() {
  if [[ -f "$inventory" ]]; then
    INVENTORY_MODE="inventory"
    return 0
  fi

  if [[ -f "$run_inventory" ]]; then
    inventory="$run_inventory"
    log "using run inventory fallback for run_id=${RUN_ID}: $inventory"
    INVENTORY_MODE="inventory"
    return 0
  fi

  if [[ "$BEST_EFFORT" -eq 1 ]]; then
    log "inventory not found for run_id=${RUN_ID}: $inventory"
    log "run inventory fallback also missing: $run_inventory"
    log "continuing with best-effort cleanup derived from variant topology"
    INVENTORY_MODE="best_effort"
    return 0
  fi

  err "inventory not found for run_id=${RUN_ID}: $inventory"
  err "run inventory fallback also missing: $run_inventory"
  return 1
}

stop_host() {
  local name="$1"
  local tap="$2"
  local pid_file="$3"
  local overlay="$4"
  local pids=""

  if [[ -f "$pid_file" ]]; then
    kill "$(cat "$pid_file")" >/dev/null 2>&1 || true
    rm -f "$pid_file"
  elif pids="$(pgrep -f -- "$overlay" 2>/dev/null || true)" && [[ -n "$pids" ]]; then
    while IFS= read -r pid; do
      [[ -n "$pid" ]] || continue
      kill "$pid" >/dev/null 2>&1 || true
    done <<<"$pids"
  fi
  if ip link show "$tap" >/dev/null 2>&1; then
    sudo ip link set "$tap" down || true
    sudo ip link delete "$tap" || true
  fi
  log "stopped ${name}"
}

stop_inventory_hosts() {
  local decoded="" name="" tap="" pid_file="" overlay=""
  local -a rows=()

  mapfile -t rows < <(jq -r '.[] | @base64' "$inventory")

  for row in "${rows[@]}"; do
    decoded="$(printf '%s' "$row" | base64 -d)"
    name="$(echo "$decoded" | jq -r '.name')"
    tap="$(echo "$decoded" | jq -r '.tap')"
    pid_file="$(echo "$decoded" | jq -r '.pid_file')"
    overlay="$state_dir/${name}.qcow2"
    stop_host "$name" "$tap" "$pid_file" "$overlay"
  done
}

stop_variant_hosts_best_effort() {
  local decoded="" name="" tap="" pid_file="" overlay=""
  local -a hosts=()

  mapfile -t hosts < <(echo "$variant_json" | jq -r '.hosts[] | @base64')

  for i in "${!hosts[@]}"; do
    decoded="$(printf '%s' "${hosts[$i]}" | base64 -d)"
    name="$(echo "$decoded" | jq -r '.name')"
    tap="$(lane_tap_name "$i")"
    pid_file="$state_dir/pids/${name}.pid"
    overlay="$state_dir/${name}.qcow2"
    stop_host "$name" "$tap" "$pid_file" "$overlay"
  done
}

destroy_network_if_requested() {
  if [[ "$DESTROY_NETWORK" -eq 1 ]]; then
    local pod_cidr=""
    while IFS= read -r pod_cidr; do
      [[ -n "$pod_cidr" ]] || continue
      while sudo iptables -t nat -D POSTROUTING -s "$cidr" -d "$pod_cidr" -j RETURN 2>/dev/null; do
        :
      done
    done < <(echo "$variant_json" | jq -r '.hosts[] | .pod_cidr // empty' | awk 'NF && !seen[$0]++')
    while sudo iptables -t nat -D POSTROUTING -s "$cidr" -o "$bridge" -j RETURN 2>/dev/null; do
      :
    done
    sudo iptables -t nat -D POSTROUTING -s "$cidr" ! -d "$cidr" -j MASQUERADE 2>/dev/null || true
    while sudo iptables -D FORWARD -j "$FORWARD_CHAIN" 2>/dev/null; do
      :
    done
    sudo iptables -F "$FORWARD_CHAIN" 2>/dev/null || true
    sudo iptables -X "$FORWARD_CHAIN" 2>/dev/null || true
    sudo iptables -D FORWARD -i "$bridge" -j ACCEPT 2>/dev/null || true
    sudo iptables -D FORWARD -o "$bridge" -j ACCEPT 2>/dev/null || true
    if ip link show "$bridge" >/dev/null 2>&1; then
      sudo ip link set "$bridge" down || true
      sudo ip link delete "$bridge" type bridge || true
    fi
    log "destroyed bridge=${bridge}"
  fi
}

purge_state_dir_if_requested() {
  if [[ "$PURGE" -eq 1 ]]; then
    rm -rf "$state_dir"
    log "purged state dir $state_dir"
  fi
}

main() {
  parse_args "$@"
  load_variant_context
  resolve_inventory_mode

  case "$INVENTORY_MODE" in
    inventory)
      stop_inventory_hosts
      ;;
    best_effort)
      stop_variant_hosts_best_effort
      ;;
    *)
      err "unsupported inventory mode: ${inventory_mode}"
      exit 1
      ;;
  esac

  destroy_network_if_requested
  purge_state_dir_if_requested
  log "variant down complete run_id=${RUN_ID}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
