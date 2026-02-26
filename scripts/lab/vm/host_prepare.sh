#!/usr/bin/env bash
set -euo pipefail

BRIDGE="${BRIDGE:-k1s-br0}"
NET_CIDR="${NET_CIDR:-192.168.152.0/24}"
GATEWAY="${GATEWAY:-192.168.152.1}"
APPLY=0

usage() {
  cat <<USAGE
Usage: $0 [--bridge name] [--cidr x.x.x.x/24] [--gateway x.x.x.x] [--apply]

Without --apply this command only validates prerequisites.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bridge) BRIDGE="$2"; shift 2 ;;
    --cidr) NET_CIDR="$2"; shift 2 ;;
    --gateway) GATEWAY="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

for cmd in qemu-system-x86_64 cloud-localds qemu-img ip ssh jq crictl; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[host-prepare] missing command: $cmd" >&2
    exit 2
  fi
done

if [[ ! -e /dev/kvm ]]; then
  echo "[host-prepare] /dev/kvm missing" >&2
  exit 2
fi

if [[ "$APPLY" -eq 0 ]]; then
  echo "[host-prepare] prerequisites look good"
  echo "[host-prepare] dry-run complete (use --apply to configure bridge and NAT)"
  exit 0
fi

sudo ip link show "$BRIDGE" >/dev/null 2>&1 || {
  prefix="${NET_CIDR#*/}"
  if [[ "$prefix" == "$NET_CIDR" ]]; then
    prefix="24"
  fi
  sudo ip link add name "$BRIDGE" type bridge
  sudo ip addr add "$GATEWAY/$prefix" dev "$BRIDGE"
  sudo ip link set "$BRIDGE" up
}

sudo iptables -t nat -C POSTROUTING -s "$NET_CIDR" ! -d "$NET_CIDR" -j MASQUERADE >/dev/null 2>&1 || \
  sudo iptables -t nat -A POSTROUTING -s "$NET_CIDR" ! -d "$NET_CIDR" -j MASQUERADE

sudo iptables -C FORWARD -i "$BRIDGE" -j ACCEPT >/dev/null 2>&1 || sudo iptables -A FORWARD -i "$BRIDGE" -j ACCEPT
sudo iptables -C FORWARD -o "$BRIDGE" -j ACCEPT >/dev/null 2>&1 || sudo iptables -A FORWARD -o "$BRIDGE" -j ACCEPT

echo "[host-prepare] configured bridge=${BRIDGE} cidr=${NET_CIDR} gateway=${GATEWAY}"
