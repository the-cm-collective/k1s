#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

BRIDGE="${BRIDGE:-k1s-br0}"
NET_CIDR="${NET_CIDR:-192.168.152.0/24}"
GATEWAY="${GATEWAY:-192.168.152.1}"
FORWARD_CHAIN="${FORWARD_CHAIN:-K1S_VM_FORWARD}"
VARIANT=""
APPLY=0
MANUAL_NETWORK_FLAGS=0
declare -a POD_CIDRS=()
declare -a TAP_INTERFACES=()

usage() {
  cat <<USAGE
Usage: $0 [--variant <path>] [--bridge name] [--cidr x.x.x.x/24] [--gateway x.x.x.x] [--apply]

Use --variant to derive bridge, CIDR, and gateway from a checked-in VM variant.
Do not combine --variant with --bridge, --cidr, or --gateway.

Without --apply this command only validates prerequisites.
USAGE
}

network_prefix() {
  local prefix="${NET_CIDR#*/}"
  if [[ "$prefix" == "$NET_CIDR" ]]; then
    prefix="24"
  fi
  printf '%s' "$prefix"
}

expected_bridge_cidr() {
  printf '%s/%s' "$GATEWAY" "$(network_prefix)"
}

bridge_exists() {
  sudo ip link show "$BRIDGE" >/dev/null 2>&1
}

bridge_ipv4_addrs() {
  sudo ip -o -4 addr show dev "$BRIDGE" 2>/dev/null | awk '{print $4}'
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --variant)
        if [[ "$MANUAL_NETWORK_FLAGS" -eq 1 ]]; then
          err "--variant cannot be combined with --bridge, --cidr, or --gateway"
          return 2
        fi
        VARIANT="$2"
        shift 2
        ;;
      --bridge)
        if [[ -n "$VARIANT" ]]; then
          err "--variant cannot be combined with --bridge, --cidr, or --gateway"
          return 2
        fi
        BRIDGE="$2"
        MANUAL_NETWORK_FLAGS=1
        shift 2
        ;;
      --cidr)
        if [[ -n "$VARIANT" ]]; then
          err "--variant cannot be combined with --bridge, --cidr, or --gateway"
          return 2
        fi
        NET_CIDR="$2"
        MANUAL_NETWORK_FLAGS=1
        shift 2
        ;;
      --gateway)
        if [[ -n "$VARIANT" ]]; then
          err "--variant cannot be combined with --bridge, --cidr, or --gateway"
          return 2
        fi
        GATEWAY="$2"
        MANUAL_NETWORK_FLAGS=1
        shift 2
        ;;
      --apply)
        APPLY=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        err "unknown arg: $1"
        usage
        return 2
        ;;
    esac
  done
}

load_variant_network() {
  [[ -n "$VARIANT" ]] || return 0

  local variant_json
  local host_count=0
  local i=0
  variant_json="$(variant_to_json "$VARIANT")"
  BRIDGE="$(echo "$variant_json" | jq -r '.network.bridge')"
  NET_CIDR="$(echo "$variant_json" | jq -r '.network.cidr')"
  GATEWAY="$(echo "$variant_json" | jq -r '.network.gateway')"
  mapfile -t POD_CIDRS < <(
    echo "$variant_json" | jq -r '.hosts[] | .pod_cidr // empty' | awk 'NF && !seen[$0]++'
  )
  host_count="$(echo "$variant_json" | jq -r '.hosts | length')"
  TAP_INTERFACES=()
  for ((i=0; i<host_count; i++)); do
    TAP_INTERFACES+=("$(lane_tap_name "$i")")
  done
}

require_prereqs() {
  for cmd in qemu-system-x86_64 cloud-localds qemu-img ip ssh jq crictl; do
    require_cmd "$cmd"
  done

  if [[ ! -e /dev/kvm ]]; then
    err "/dev/kvm missing"
    return 2
  fi
}

validate_existing_bridge() {
  bridge_exists || return 0

  local expected
  expected="$(expected_bridge_cidr)"

  mapfile -t current_addrs < <(bridge_ipv4_addrs)
  if [[ "${#current_addrs[@]}" -eq 0 ]]; then
    err "bridge=${BRIDGE} already exists without an IPv4 address; expected ${expected}. Destroy and recreate the bridge before retrying."
    err "remediation: scripts/lab/vm/labctl.sh variant down --variant <path> --run-id <id> --purge --destroy-network"
    return 1
  fi

  local addr
  for addr in "${current_addrs[@]}"; do
    if [[ "$addr" == "$expected" ]]; then
      return 0
    fi
  done

  local actual
  actual="$(printf '%s ' "${current_addrs[@]}")"
  actual="${actual% }"
  err "bridge=${BRIDGE} already exists with IPv4 ${actual}; expected ${expected} for ${NET_CIDR}"
  err "remediation: tear down the previous lane with --destroy-network or recreate ${BRIDGE} before retrying"
  return 1
}

ensure_bridge() {
  if bridge_exists; then
    return 0
  fi

  sudo ip link add name "$BRIDGE" type bridge
  sudo ip addr add "$(expected_bridge_cidr)" dev "$BRIDGE"
  sudo ip link set "$BRIDGE" up
}

delete_nat_rule() {
  local table="$1"
  shift
  while sudo iptables -t "$table" -C "$@" >/dev/null 2>&1; do
    sudo iptables -t "$table" -D "$@" >/dev/null 2>&1 || break
  done
}

ensure_nat_return_rule() {
  delete_nat_rule nat POSTROUTING "$@"
  sudo iptables -t nat -I POSTROUTING 1 "$@"
}

delete_filter_rule() {
  while sudo iptables -C "$@" >/dev/null 2>&1; do
    sudo iptables -D "$@" >/dev/null 2>&1 || break
  done
}

ensure_forward_chain_rule() {
  sudo iptables -A "$FORWARD_CHAIN" "$@"
}

ensure_forward_rules() {
  local tap=""

  sudo iptables -N "$FORWARD_CHAIN" 2>/dev/null || true
  sudo iptables -F "$FORWARD_CHAIN"

  # Bridge netfilter can surface these packets as either the bridge device or
  # the individual tap ports. Accept both views before Docker/libvirt jumps.
  ensure_forward_chain_rule -i "$BRIDGE" -j ACCEPT
  ensure_forward_chain_rule -o "$BRIDGE" -j ACCEPT
  for tap in "${TAP_INTERFACES[@]}"; do
    ensure_forward_chain_rule -i "$tap" -j ACCEPT
    ensure_forward_chain_rule -o "$tap" -j ACCEPT
  done

  delete_filter_rule FORWARD -j "$FORWARD_CHAIN"
  sudo iptables -I FORWARD 1 -j "$FORWARD_CHAIN"
}

ensure_nat_rules() {
  ensure_nat_return_rule -s "$NET_CIDR" -o "$BRIDGE" -j RETURN
  local pod_cidr=""
  for pod_cidr in "${POD_CIDRS[@]}"; do
    [[ -n "$pod_cidr" ]] || continue
    ensure_nat_return_rule -s "$NET_CIDR" -d "$pod_cidr" -j RETURN
  done
  sudo iptables -t nat -C POSTROUTING -s "$NET_CIDR" ! -d "$NET_CIDR" -j MASQUERADE >/dev/null 2>&1 || \
    sudo iptables -t nat -A POSTROUTING -s "$NET_CIDR" ! -d "$NET_CIDR" -j MASQUERADE

  ensure_forward_rules
}

main() {
  parse_args "$@"
  require_prereqs
  load_variant_network

  if [[ "$APPLY" -eq 0 ]]; then
    echo "[host-prepare] prerequisites look good"
    echo "[host-prepare] dry-run complete (use --apply to configure bridge and NAT)"
    return 0
  fi

  validate_existing_bridge
  ensure_bridge
  ensure_nat_rules

  echo "[host-prepare] configured bridge=${BRIDGE} cidr=${NET_CIDR} gateway=${GATEWAY}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
