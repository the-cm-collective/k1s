#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
RUN_ID="$(resolve_run_id)"

usage() {
  cat <<USAGE
Usage: $0 --variant <path> [--run-id <id>]
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant) VARIANT="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) err "unknown arg: $1"; usage; exit 2 ;;
  esac
done

[[ -n "$VARIANT" ]] || { err "--variant is required"; usage; exit 2; }
[[ -f "$VARIANT" ]] || { err "variant not found: $VARIANT"; exit 2; }

require_cmd qemu-system-x86_64
require_cmd qemu-img
require_cmd cloud-localds
require_cmd ip
require_cmd jq
require_cmd ssh
ensure_ssh_key

variant_json="$(variant_to_json "$VARIANT")"
bridge="$(echo "$variant_json" | jq -r '.network.bridge')"
cidr="$(echo "$variant_json" | jq -r '.network.cidr')"
gateway="$(echo "$variant_json" | jq -r '.network.gateway')"
disk_gb="$(echo "$variant_json" | jq -r '.vm.disk_gb')"
vm_mem="$(echo "$variant_json" | jq -r '.vm.memory_mb')"
vm_cpus="$(echo "$variant_json" | jq -r '.vm.vcpus')"
base_img="$(echo "$variant_json" | jq -r '.images.base')"
gpu_img="$(echo "$variant_json" | jq -r '.images.gpu')"

[[ -f "$base_img" ]] || { err "base image missing: $base_img"; exit 2; }
[[ -f "$gpu_img" ]] || { err "gpu image missing: $gpu_img"; exit 2; }

"$ROOT_DIR/scripts/lab/vm/host_prepare.sh" --bridge "$bridge" --cidr "$cidr" --gateway "$gateway" --apply

state_dir="$ROOT_DIR/state/lab-vm/$RUN_ID"
log_dir="$state_dir/logs"
pid_dir="$state_dir/pids"
seed_dir="$state_dir/seeds"
mkdir -p "$state_dir" "$log_dir" "$pid_dir" "$seed_dir"
ensure_run_dir "$RUN_ID"

echo "$variant_json" >"$(run_dir "$RUN_ID")/topology.json"
cp "$VARIANT" "$(run_dir "$RUN_ID")/variant.yaml"

tap_up() {
  local tap="$1"
  if ip link show "$tap" >/dev/null 2>&1; then
    return 0
  fi
  sudo ip tuntap add dev "$tap" mode tap user "$(whoami)"
  sudo ip link set "$tap" master "$bridge"
  sudo ip link set "$tap" up
}

make_seed() {
  local name="$1"
  local ip="$2"
  local seed_path="$3"
  local dns_csv="$4"
  local tmp
  tmp="$(mktemp -d)"

  local key_path="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
  local pubkey
  pubkey="$(cat "${key_path}.pub")"

  cat >"$tmp/user-data" <<CFG
#cloud-config
hostname: ${name}
users:
  - name: ae
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh-authorized-keys:
      - ${pubkey}
package_update: true
packages:
  - qemu-guest-agent
  - jq
  - python3-pip
runcmd:
  - cloud-init status --wait
  - mkdir -p /mnt/host
  - mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
  - systemctl enable qemu-guest-agent
  - systemctl start qemu-guest-agent
CFG

  cat >"$tmp/network-config" <<CFG
network:
  version: 2
  renderer: networkd
  ethernets:
    ens3:
      dhcp4: false
      addresses: [${ip}/24]
      gateway4: ${gateway}
      nameservers:
        addresses: [${dns_csv}]
CFG

  cat >"$tmp/meta-data" <<CFG
instance-id: iid-${name}
local-hostname: ${name}
CFG

  cloud-localds --network-config="$tmp/network-config" "$seed_path" "$tmp/user-data" "$tmp/meta-data"
  rm -rf "$tmp"
}

start_one() {
  local index="$1"
  local row_b64="$2"
  local row
  row="$(printf '%s' "$row_b64" | base64 -d)"

  local name ip gpu tap seed img overlay pid log mac dns_csv
  name="$(echo "$row" | jq -r '.name')"
  ip="$(echo "$row" | jq -r '.ip')"
  gpu="$(echo "$row" | jq -r '.gpu')"
  tap="k1s${index}"
  seed="$seed_dir/${name}.seed.iso"
  overlay="$state_dir/${name}.qcow2"
  pid="$pid_dir/${name}.pid"
  log="$log_dir/${name}.qemu.log"
  dns_csv="$(echo "$variant_json" | jq -r '.network.dns | join(",")')"
  img="$base_img"
  if [[ "$gpu" == "true" ]]; then
    img="$gpu_img"
  fi

  if [[ ! -f "$overlay" ]]; then
    qemu-img create -f qcow2 -b "$img" "$overlay" "${disk_gb}G" >/dev/null
  fi

  make_seed "$name" "$ip" "$seed" "$dns_csv"
  tap_up "$tap"

  mac="$(printf '52:54:00:%02x:%02x:%02x' $((index & 0xff)) $(((index + 16) & 0xff)) $(((index + 32) & 0xff)))"

  qemu-system-x86_64 \
    -enable-kvm \
    -m "$vm_mem" -smp "$vm_cpus" \
    -drive file="$overlay",if=virtio \
    -drive file="$seed",if=virtio,format=raw \
    -device virtio-net-pci,netdev=net0,mac="$mac" \
    -netdev tap,id=net0,ifname="$tap",script=no,downscript=no \
    -fsdev local,id=fsdev0,path="$ROOT_DIR",security_model=none,multidevs=remap \
    -device virtio-9p-pci,fsdev=fsdev0,mount_tag=hostshare \
    -display none \
    -serial "file:${log_dir}/${name}.console.log" \
    -daemonize \
    -pidfile "$pid" \
    -D "$log"

  jq -n \
    --arg name "$name" \
    --arg ip "$ip" \
    --arg tap "$tap" \
    --arg pid_file "$pid" \
    --arg log "$log" \
    '{name:$name,ip:$ip,tap:$tap,pid_file:$pid_file,log:$log}'
}

mapfile -t hosts < <(echo "$variant_json" | jq -r '.hosts[] | @base64')
if [[ "${#hosts[@]}" -eq 0 ]]; then
  err "variant has no hosts"
  exit 2
fi

inventory_tmp="$(mktemp)"
printf '[]' >"$inventory_tmp"

for i in "${!hosts[@]}"; do
  host_json="$(start_one "$i" "${hosts[$i]}")"
  jq --argjson h "$host_json" '. + [$h]' "$inventory_tmp" >"${inventory_tmp}.next"
  mv "${inventory_tmp}.next" "$inventory_tmp"
done

mv "$inventory_tmp" "$state_dir/inventory.json"
cp "$state_dir/inventory.json" "$(run_dir "$RUN_ID")/qemu_inventory.json"

for row in "${hosts[@]}"; do
  host="$(printf '%s' "$row" | base64 -d)"
  name="$(echo "$host" | jq -r '.name')"
  ip="$(echo "$host" | jq -r '.ip')"
  log "waiting for ssh: ${name} (${ip})"
  if ! wait_for_ssh "$ip" 150; then
    err "ssh did not become ready for ${name} (${ip})"
    exit 1
  fi
  with_repo_host_mount "$ip" >/dev/null 2>&1 || true
done

log "variant up complete run_id=${RUN_ID}"
log "next: scripts/lab/vm/k1s_bootstrap.sh --variant ${VARIANT} --run-id ${RUN_ID} --execute"
