#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

RUN_ID="$(resolve_run_id)"
VARIANT=""
PURGE=0
DESTROY_NETWORK=0

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>] [--purge] [--destroy-network]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    --destroy-network) DESTROY_NETWORK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { err "--variant is required"; usage; exit 2; }
variant_json="$(variant_to_json "$VARIANT")"
bridge="$(echo "$variant_json" | jq -r '.network.bridge')"
cidr="$(echo "$variant_json" | jq -r '.network.cidr')"

state_dir="$ROOT_DIR/state/lab-vm/$RUN_ID"
inventory="$state_dir/inventory.json"
run_inventory="$(run_dir "$RUN_ID")/qemu_inventory.json"

if [[ ! -f "$inventory" ]]; then
  if [[ -f "$run_inventory" ]]; then
    inventory="$run_inventory"
    log "using run inventory fallback for run_id=${RUN_ID}: $inventory"
  else
    err "inventory not found for run_id=${RUN_ID}: $inventory"
    err "run inventory fallback also missing: $run_inventory"
    exit 1
  fi
fi

mapfile -t rows < <(jq -r '.[] | @base64' "$inventory")

for row in "${rows[@]}"; do
  decoded="$(printf '%s' "$row" | base64 -d)"
  name="$(echo "$decoded" | jq -r '.name')"
  tap="$(echo "$decoded" | jq -r '.tap')"
  pid_file="$(echo "$decoded" | jq -r '.pid_file')"
  overlay="$state_dir/${name}.qcow2"

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
done

if [[ "$DESTROY_NETWORK" -eq 1 ]]; then
  sudo iptables -t nat -D POSTROUTING -s "$cidr" ! -d "$cidr" -j MASQUERADE 2>/dev/null || true
  sudo iptables -D FORWARD -i "$bridge" -j ACCEPT 2>/dev/null || true
  sudo iptables -D FORWARD -o "$bridge" -j ACCEPT 2>/dev/null || true
  if ip link show "$bridge" >/dev/null 2>&1; then
    sudo ip link set "$bridge" down || true
    sudo ip link delete "$bridge" type bridge || true
  fi
  log "destroyed bridge=${bridge}"
fi

if [[ "$PURGE" -eq 1 ]]; then
  rm -rf "$state_dir"
  log "purged state dir $state_dir"
fi

log "variant down complete run_id=${RUN_ID}"
