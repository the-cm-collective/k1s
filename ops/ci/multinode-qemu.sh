#!/usr/bin/env bash
# Helper to spin up a three-VM multinode lab (controller + 2 workers) with QEMU/KVM.
# Designed for CI on KVM-capable runners. Requires sudo for tap/bridge setup.

set -euo pipefail

ACTION="${1:-}"

STATE_DIR="${STATE_DIR:-state/qemu}"
LOG_DIR="$STATE_DIR/logs"
PID_DIR="$STATE_DIR/pids"
SEED_DIR="$STATE_DIR/seeds"

BASE_IMG="${BASE_IMG:-.cache/images/ubuntu-24.04-server-cloudimg-amd64.img}"
VM_MEM="${VM_MEM:-2048}"
VM_CPUS="${VM_CPUS:-2}"
VM_DISK_GB="${VM_DISK_GB:-10}"
BRIDGE="${BRIDGE:-k1s-br0}"
NET_CIDR="${NET_CIDR:-192.168.152.0/24}"
GATEWAY="${GATEWAY:-192.168.152.1}"

CTRL_IP="${CTRL_IP:-192.168.152.10}"
WK1_IP="${WK1_IP:-192.168.152.11}"
WK2_IP="${WK2_IP:-192.168.152.12}"

AE_TOKEN="${AE_TOKEN:-ci-token}"
ENABLE_OVERLAY="${ENABLE_OVERLAY:-0}"

SSH_PUB_KEY="${SSH_PUB_KEY:-}"
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
HOST_KEY_PATH="${HOST_KEY_PATH:-ops/ci/keys/id_rsa}"

usage() {
  cat <<'EOF'
Usage: ops/ci/multinode-qemu.sh start|stop

Env knobs:
  BASE_IMG           Path to ubuntu-24.04 cloud image qcow2 (download ahead of time).
  STATE_DIR          Working dir for overlays/seeds/logs (default state/qemu).
  VM_MEM / VM_CPUS   Per-VM resources (defaults 2048 MB / 2 vCPU).
  BRIDGE             Host bridge name (default k1s-br0).
  NET_CIDR           Guest network (default 192.168.152.0/24), GATEWAY=192.168.152.1.
  CTRL_IP/WK1_IP/WK2_IP  Static guest IPs.
  AE_TOKEN           Shared controller/agent token.
  ENABLE_OVERLAY     Set 1 to install wireguard-tools in guests.
  SSH_PUB_KEY        Override authorized key (defaults to contents of SSH_KEY_PATH.pub).
  SSH_KEY_PATH       Private key path for ssh into guests (default ~/.ssh/id_rsa).

Prereqs on host: qemu-system-x86_64, cloud-localds (cloud-image-utils), iproute2, sudo,
and KVM access (/dev/kvm).
EOF
}

ensure_deps() {
  command -v qemu-system-x86_64 >/dev/null || { echo "qemu-system-x86_64 missing"; exit 1; }
  command -v cloud-localds >/dev/null || { echo "cloud-localds missing (cloud-image-utils)"; exit 1; }
  command -v ip >/dev/null || { echo "ip command missing"; exit 1; }
}

ensure_base_image() {
  if [[ ! -f "$BASE_IMG" ]]; then
    echo "Base image $BASE_IMG not found. Download ubuntu-24.04-server-cloudimg-amd64.img first."
    exit 1
  fi
}

ensure_bridge() {
  if ip link show "$BRIDGE" >/dev/null 2>&1; then
    return
  fi
  sudo ip link add name "$BRIDGE" type bridge
  sudo ip addr add "$GATEWAY"/24 dev "$BRIDGE"
  sudo ip link set "$BRIDGE" up
  sudo iptables -t nat -A POSTROUTING -s "$NET_CIDR" ! -d "$NET_CIDR" -j MASQUERADE || true
  sudo iptables -A FORWARD -i "$BRIDGE" -j ACCEPT || true
  sudo iptables -A FORWARD -o "$BRIDGE" -j ACCEPT || true
}

tap_up() {
  local tap=$1
  if ip link show "$tap" >/dev/null 2>&1; then
    return
  fi
  sudo ip tuntap add dev "$tap" mode tap user "$(whoami)"
  sudo ip link set "$tap" master "$BRIDGE"
  sudo ip link set "$tap" up
}

tap_down() {
  local tap=$1
  if ip link show "$tap" >/dev/null 2>&1; then
    sudo ip link set "$tap" down || true
    sudo ip link delete "$tap" || true
  fi
}

make_overlay() {
  local name=$1
  local img="$STATE_DIR/${name}.qcow2"
  if [[ -f "$img" ]]; then
    return
  fi
  qemu-img create -f qcow2 -b "$BASE_IMG" "$img" "${VM_DISK_GB}G" >/dev/null
}

make_seed() {
  local name=$1 ip=$2
  mkdir -p "$SEED_DIR"
  local seed="$SEED_DIR/${name}-seed.iso"
  local tmp
  tmp="$(mktemp -d)"

  local pubkey_content
  if [[ -n "$SSH_PUB_KEY" ]]; then
    pubkey_content="$SSH_PUB_KEY"
  else
    if [[ -f "${SSH_KEY_PATH}.pub" ]]; then
      pubkey_content="$(cat "${SSH_KEY_PATH}.pub")"
    else
      echo "No SSH key found; set SSH_PUB_KEY or SSH_KEY_PATH" >&2
      exit 1
    fi
  fi

  cat >"$tmp/user-data" <<EOF
#cloud-config
users:
  - name: ae
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh-authorized-keys:
      - $pubkey_content
package_update: true
packages:
  - docker.io
  - python3-pip
  - jq
$( [[ "$ENABLE_OVERLAY" == "1" ]] && echo "  - wireguard-tools" )
runcmd:
  - cloud-init status --wait
  - ip link set ens3 up
  - ip addr add $ip/24 dev ens3 || true
  - ip route replace default via $GATEWAY || true
  - mkdir -p /mnt/host
  - mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host || true
  - systemctl enable docker
  - systemctl start docker
  - sysctl -w net.ipv4.ip_forward=1
EOF

  cat >"$tmp/network-config" <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    ens3:
      dhcp4: false
      addresses: [$ip/24]
      gateway4: $GATEWAY
      nameservers:
        addresses: [1.1.1.1,8.8.8.8]
EOF

  cloud-localds --network-config="$tmp/network-config" "$seed" "$tmp/user-data"
  rm -rf "$tmp"
}

start_vm() {
  local name=$1 ip=$2 mac=$3 tap=$4
  local seed="$SEED_DIR/${name}-seed.iso"
  local img="$STATE_DIR/${name}.qcow2"
  local log="$LOG_DIR/${name}.qemu.log"
  local pid="$PID_DIR/${name}.pid"
  mkdir -p "$LOG_DIR" "$PID_DIR"

  tap_up "$tap"

  qemu-system-x86_64 \
    -enable-kvm \
    -m "$VM_MEM" -smp "$VM_CPUS" \
    -drive file="$img",if=virtio \
    -drive file="$seed",if=virtio,format=raw \
    -device virtio-net-pci,netdev=net0,mac="$mac" \
    -netdev tap,id=net0,ifname="$tap",script=no,downscript=no \
    -fsdev local,id=fsdev0,path="$PWD",security_model=none,multidevs=remap \
    -device virtio-9p-pci,fsdev=fsdev0,mount_tag=hostshare \
    -display none -serial "file:${LOG_DIR}/${name}.console.log" \
    -daemonize \
    -pidfile "$pid" \
    -D "$log"
}

stop_vm() {
  local name=$1 tap=$2
  local pid="$PID_DIR/${name}.pid"
  if [[ -f "$pid" ]]; then
    kill "$(cat "$pid")" 2>/dev/null || true
    rm -f "$pid"
  fi
  tap_down "$tap"
}

wait_ssh() {
  local ip=$1
  for _ in {1..90}; do
    if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_PATH" "ae@$ip" "echo up" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "SSH not reachable on $ip" >&2
  return 1
}

mount_host() {
  local ip=$1
  for _ in {1..6}; do
    if ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_PATH" "ae@$ip" \
      "sudo mkdir -p /mnt/host && sudo mount -t 9p -o trans=virtio,version=9p2000.L hostshare /mnt/host" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "failed to mount hostshare on $ip" >&2
  return 1
}

start_stack() {
  ensure_deps
  ensure_base_image
  mkdir -p "$STATE_DIR"
  ensure_bridge

  # Prepare overlays and seeds
  make_overlay controller
  make_overlay worker1
  make_overlay worker2

  make_seed controller "$CTRL_IP"
  make_seed worker1 "$WK1_IP"
  make_seed worker2 "$WK2_IP"

  start_vm controller "$CTRL_IP" "52:54:00:00:00:10" vnet10
  start_vm worker1 "$WK1_IP" "52:54:00:00:00:11" vnet11
  start_vm worker2 "$WK2_IP" "52:54:00:00:00:12" vnet12

  wait_ssh "$CTRL_IP"
  wait_ssh "$WK1_IP"
  wait_ssh "$WK2_IP"

  echo "VMs up. Mounting repo and installing ae..."
  mount_host "$CTRL_IP"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_PATH" "ae@$CTRL_IP" \
    "cloud-init status --wait; cd /mnt/host && python3 -m pip install -e .[dev] --break-system-packages"
  for ip in "$WK1_IP" "$WK2_IP"; do
    mount_host "$ip"
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_PATH" "ae@$ip" \
      "cloud-init status --wait; cd /mnt/host && python3 -m pip install -e . --break-system-packages"
  done

  echo "Starting controller and agents..."
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_PATH" "ae@$CTRL_IP" \
    "AE_ENABLE_SERVICE_PROXY=1 AE_SERVICE_PROVIDER=bridge AE_SERVICE_IP_POOL=10.241.0.0/16 AE_POD_CIDR_POOL=10.42.0.0/16 AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=REDACTED"

  for ip in "$WK1_IP" "$WK2_IP"; do
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_PATH" "ae@$ip" \
      "AE_CONTROLLER_URL=https://${CTRL_IP}:9110 AE_CONTROLLER_TLS_CA=/mnt/host/state/tls/agent-ca.crt AE_CONTROLLER_TLS_CERT=/mnt/host/state/tls/${ip//./-}.crt AE_CONTROLLER_TLS_KEY=/mnt/host/state/tls/${ip//./-}.key AE_AGENT_TOKEN=${AE_TOKEN} AE_NODE_ID=${ip//./-} AE_NODE_NAME=${ip//./-} AE_NODE_ADVERTISE_IP=${ip} AE_AGENT_ENDPOINT=https://${ip}:9109 AE_AGENT_HEARTBEAT_SECONDS=10 AE_AGENT_CONFIGURE_OVERLAY=${ENABLE_OVERLAY} AE_NETWORK_NAME=ae-net AE_DOCKER_NETWORK=ae-net AE_POD_CIDR=10.42.$((RANDOM%250)).0/24 AE_AGENT_TLS_CERT=/mnt/host/state/tls/${ip//./-}.crt AE_AGENT_TLS_KEY=/mnt/host/state/tls/${ip//./-}.key AE_AGENT_CLIENT_CA=/mnt/host/state/tls/agent-ca.crt AE_AGENT_REQUIRE_CLIENT_CERT=1 nohup python3 -m ae.node.server --runtime-backend docker --port 9109 --advertise-endpoint https://${ip}:9109 > /home/ae/agent.log 2>&1 & echo \$! > /home/ae/agent.pid"
  done

  echo "Stack started. Controller at ${CTRL_IP}:9110"

  if [[ "${RUN_SMOKE:-0}" == "1" ]]; then
    echo "Running built-in smoke..."
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_PATH" "ae@$CTRL_IP" "WK1_IP=${WK1_IP} HOST_KEY_PATH=${HOST_KEY_PATH} bash -s" <<'REMOTE'
set -euo pipefail
WK1="${WK1_IP}"
HOST_KEY="${HOST_KEY_PATH}"
export AE_STATE_DB=/home/ae/state/controller.db
cd /mnt/host
python3 -m ae.cli apply -f specs/examples/echo-multinode.yaml
python3 -m ae.cli status echo-mn --watch 5 --timeout 150
vip=$(python3 - <<'PY'
import json, subprocess, os
raw = subprocess.check_output(["python3", "-m", "ae.cli", "services", "--json"], env=dict(os.environ))
for item in json.loads(raw):
    if item.get("app_name") == "echo-mn":
        print(item.get("cluster_ip", ""))
        break
PY
)
curl -s --max-time 5 http://$vip:8080/healthz >/tmp/vip_before.txt
echo "VIP before failover: $(cat /tmp/vip_before.txt)"
if [[ -f "$HOST_KEY" ]]; then
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$HOST_KEY" "$WK1" "\
    sudo pkill -f 'ae.node.server' || true; \
    sudo docker rm -f \$(sudo docker ps -aq --filter label=ae.app=echo-mn) 2>/dev/null || true"
else
  echo "Skip failover kill: $HOST_KEY not present"
fi
# wait for node staleness (AE_NODE_NOTREADY_AFTER defaults 40s)
sleep 50
python3 -m ae.cli status echo-mn --watch 5 --timeout 150
vip2=$(python3 - <<'PY'
import json, subprocess, os
raw = subprocess.check_output(["python3", "-m", "ae.cli", "services", "--json"], env=dict(os.environ))
for item in json.loads(raw):
    if item.get("app_name") == "echo-mn":
        print(item.get("cluster_ip", ""))
        break
PY
)
curl -s --max-time 5 http://$vip2:8080/healthz >/tmp/vip_after.txt
echo "VIP after failover: $(cat /tmp/vip_after.txt)"
python3 -m ae.cli delete echo-mn || true
REMOTE
    echo "Smoke complete."
  fi
}

stop_stack() {
  echo "Stopping controller/agents (best effort)..."
  for ip in "$CTRL_IP" "$WK1_IP" "$WK2_IP"; do
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "$SSH_KEY_PATH" "ae@$ip" \
      "kill \$(cat /home/ae/controller.pid 2>/dev/null) 2>/dev/null || true; kill \$(cat /home/ae/agent.pid 2>/dev/null) 2>/dev/null || true" || true
  done
  stop_vm controller vnet10
  stop_vm worker1 vnet11
  stop_vm worker2 vnet12
  # cleanup NAT rules (best effort)
  sudo iptables -t nat -D POSTROUTING -s "$NET_CIDR" ! -d "$NET_CIDR" -j MASQUERADE 2>/dev/null || true
  sudo iptables -D FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null || true
  sudo iptables -D FORWARD -o "$BRIDGE" -j ACCEPT 2>/dev/null || true
  if ip link show "$BRIDGE" >/dev/null 2>&1; then
    sudo ip link set "$BRIDGE" down || true
    sudo ip link delete "$BRIDGE" type bridge || true
  fi
  echo "Stopped."
}

case "$ACTION" in
  start) start_stack ;;
  stop) stop_stack ;;
  *) usage ;;
esac
