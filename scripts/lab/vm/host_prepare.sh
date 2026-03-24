#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

BRIDGE="${BRIDGE:-k1s-br0}"
NET_CIDR="${NET_CIDR:-192.168.152.0/24}"
GATEWAY="${GATEWAY:-192.168.152.1}"
VARIANT=""
APPLY=0
MANUAL_NETWORK_FLAGS=0

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
  variant_json="$(variant_to_json "$VARIANT")"
  BRIDGE="$(echo "$variant_json" | jq -r '.network.bridge')"
  NET_CIDR="$(echo "$variant_json" | jq -r '.network.cidr')"
  GATEWAY="$(echo "$variant_json" | jq -r '.network.gateway')"
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

ensure_nat_rules() {
  sudo iptables -t nat -C POSTROUTING -s "$NET_CIDR" ! -d "$NET_CIDR" -j MASQUERADE >/dev/null 2>&1 || \
    sudo iptables -t nat -A POSTROUTING -s "$NET_CIDR" ! -d "$NET_CIDR" -j MASQUERADE

  sudo iptables -C FORWARD -i "$BRIDGE" -j ACCEPT >/dev/null 2>&1 || sudo iptables -A FORWARD -i "$BRIDGE" -j ACCEPT
  sudo iptables -C FORWARD -o "$BRIDGE" -j ACCEPT >/dev/null 2>&1 || sudo iptables -A FORWARD -o "$BRIDGE" -j ACCEPT
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
