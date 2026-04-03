#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lab/vm/lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

VARIANT=""
RUN_ID="$(resolve_run_id)"
CLOUD_INIT_WAIT_TIMEOUT="${CLOUD_INIT_WAIT_TIMEOUT:-300}"

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
if ! [[ "$CLOUD_INIT_WAIT_TIMEOUT" =~ ^[0-9]+$ ]]; then
  err "CLOUD_INIT_WAIT_TIMEOUT must be an integer number of seconds"
  exit 2
fi

require_cmd qemu-system-x86_64
require_cmd qemu-img
require_cmd cloud-localds
require_cmd ip
require_cmd jq
require_cmd ssh
ensure_ssh_key

variant_json="$(variant_to_json "$VARIANT" --validate-images)"
bridge="$(echo "$variant_json" | jq -r '.network.bridge')"
cidr="$(echo "$variant_json" | jq -r '.network.cidr')"
gateway="$(echo "$variant_json" | jq -r '.network.gateway')"
base_img="$(echo "$variant_json" | jq -r '.images.base')"
gpu_img="$(echo "$variant_json" | jq -r '.images.gpu')"
pod_route_rows="$(
  echo "$variant_json" | jq -r '.hosts[] | select(.role=="k1s-core-node" and (.pod_cidr // "") != "") | [.pod_cidr, .ip] | @tsv'
)"

[[ -f "$base_img" ]] || { err "base image missing: $base_img"; exit 2; }
[[ -f "$gpu_img" ]] || { err "gpu image missing: $gpu_img"; exit 2; }

"$ROOT_DIR/scripts/lab/vm/host_prepare.sh" --variant "$VARIANT" --apply

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
  local route_yaml="${5:-}"
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
    ssh_authorized_keys:
      - ${pubkey}
package_update: true
packages:
  - qemu-guest-agent
  - jq
  - python3-pip
runcmd:
  - mkdir -p /mnt/host
  - mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
  - systemctl enable qemu-guest-agent || true
  - systemctl start qemu-guest-agent || true
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
${route_yaml}
CFG

  cat >"$tmp/meta-data" <<CFG
instance-id: iid-${name}
local-hostname: ${name}
CFG

  cloud-localds --network-config="$tmp/network-config" "$seed_path" "$tmp/user-data" "$tmp/meta-data"
  rm -rf "$tmp"
}

render_guest_route_yaml() {
  local role="$1"
  if [[ "$role" != "k1s-core" && "$role" != "k1s-ha-core" ]]; then
    return 0
  fi
  if [[ -z "$pod_route_rows" ]]; then
    return 0
  fi

  printf '      routes:\n'
  while IFS=$'\t' read -r route_cidr route_ip; do
    [[ -n "$route_cidr" && -n "$route_ip" ]] || continue
    printf '        - to: %s\n' "$route_cidr"
    printf '          via: %s\n' "$route_ip"
  done <<<"$pod_route_rows"
}

start_one() {
  local index="$1"
  local row_b64="$2"
  local row
  row="$(printf '%s' "$row_b64" | base64 -d)"

  local name ip role gpu tap seed img overlay pid log mac dns_csv route_yaml
  local host_disk_gb host_mem host_cpus
  name="$(echo "$row" | jq -r '.name')"
  ip="$(echo "$row" | jq -r '.ip')"
  role="$(echo "$row" | jq -r '.role')"
  gpu="$(echo "$row" | jq -r '.gpu')"
  host_disk_gb="$(echo "$row" | jq -r '.vm.disk_gb')"
  host_mem="$(echo "$row" | jq -r '.vm.memory_mb')"
  host_cpus="$(echo "$row" | jq -r '.vm.vcpus')"
  tap="$(lane_tap_name "$index")"
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
    qemu-img create -f qcow2 -F qcow2 -b "$img" "$overlay" "${host_disk_gb}G" >/dev/null
  fi

  route_yaml="$(render_guest_route_yaml "$role")"
  make_seed "$name" "$ip" "$seed" "$dns_csv" "$route_yaml"
  tap_up "$tap"

  mac="$(printf '52:54:00:%02x:%02x:%02x' $((index & 0xff)) $(((index + 16) & 0xff)) $(((index + 32) & 0xff)))"

  qemu-system-x86_64 \
    -enable-kvm \
    -m "$host_mem" -smp "$host_cpus" \
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

  if [[ ! -s "$pid" ]]; then
    err "qemu failed to start ${name}; missing pidfile ${pid} (see ${log})"
    return 1
  fi
  local qemu_pid
  qemu_pid="$(cat "$pid" 2>/dev/null || true)"
  if [[ -z "$qemu_pid" ]] || ! kill -0 "$qemu_pid" >/dev/null 2>&1; then
    err "qemu failed to stay up for ${name}; invalid pid '${qemu_pid}' (see ${log})"
    return 1
  fi

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
  log "waiting for cloud-init: ${name} (${ip})"
  if ! wait_for_cloud_init "$ip" "$CLOUD_INIT_WAIT_TIMEOUT"; then
    cloud_init_detail="$(run_remote "$ip" "cloud-init status --long 2>/dev/null | sed -n '1,12p'" || true)"
    if [[ -n "$cloud_init_detail" ]]; then
      err "cloud-init detail for ${name} (${ip}):"$'\n'"${cloud_init_detail}"
    fi
    err "cloud-init did not complete for ${name} (${ip})"
    exit 1
  fi
  with_repo_host_mount "$ip" >/dev/null 2>&1 || true
done

log "variant up complete run_id=${RUN_ID}"
log "next: scripts/lab/vm/k1s_bootstrap.sh --variant ${VARIANT} --run-id ${RUN_ID} --execute"
