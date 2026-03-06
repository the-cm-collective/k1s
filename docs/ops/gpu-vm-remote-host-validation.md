# Remote GPU VM Validation (A+B, libvirt/QEMU)

Purpose
- Validate the two-host GPU ops pattern before full multi-site (`A+B+C`) runs.
- Host A (workstation): `k1s-core` controller.
- Host B (remote hypervisor): Ubuntu LTS VM with RTX-6000 passthrough, running `edge-b`.
- Run the sequence from Host A over SSH for repeatability.

Scope
- Includes VM bring-up, k1s edge connectivity, and inference cell readiness.
- Uses `libvirt+QEMU`, bridged static LAN networking, and repo golden GPU image.
- Includes an appendix to extend from `A+B` to `A+B+C`.

Success criteria
- Remote VM reports GPU and CRI runtime handler `nvidia` as ready.
- `edge-b` gateway and node register and become `Ready`.
- `specs/examples/inference/cell-b-single.yaml` reaches `READY`.

## 0) Variables and prerequisites

Set once on Host A:

```bash
cd ~/git/k1s-wt/dev-fabric-0
sudo -v

export RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_remote_gpu_vm_validate"

# Host A (this workstation/controller host)
export CORE_HOST="<core_host_lan_ip>"                # example: 192.168.1.50

# Host B (remote hypervisor host, not the VM)
export REMOTE_SSH="ae@<remote_hypervisor_lan_ip>"    # example: ae@192.168.1.60
export REMOTE_REPO="~/git/k1s-wt/dev-fabric-0"

# Remote VM identity (edge-b VM)
export VM_NAME="k1s-edge-b-gpu"
export VM_IP="192.168.1.70"
export VM_CIDR="24"
export VM_GW="192.168.1.1"
export VM_DNS1="1.1.1.1"
export VM_DNS2="8.8.8.8"
export VM_BRIDGE="br0"                               # bridge on remote hypervisor

# GPU passthrough BDFs on Host B (set from lspci in Step 2)
export GPU_BDF="<0000:xx:yy.z>"
export GPU_AUDIO_BDF="<0000:xx:yy.z>"

# k1s edge identity
export SITE_ID="edge-b"
export EDGE_GW_NODE_ID="edge-b-gw"
export EDGE_NODE_ID="edge-b--gpu-1"
export EDGE_GPU_SKU="rtx-6000"
export AE_AGENT_TOKEN="devtoken"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i "${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}")
```

Required tools
- Host A: `ssh`, `scp`, `jq`, repo checked out.
- Host B: `libvirt`, `qemu-kvm`, `virt-install`, `cloud-localds`, bridge configured.
- VM guest: `nvidia-smi`, `crictl`, `containerd` (provided by golden image path below).

## 1) Build/verify/transfer GPU golden image

On Host A:

```bash
scripts/lab/vm/labctl.sh image build --variant all
scripts/lab/vm/labctl.sh image verify --variant all
scripts/lab/vm/labctl.sh image transfer --host "$REMOTE_SSH" --dest /var/lib/k1s/images --variant all
```

Use the transferred GPU image on Host B:
- `/var/lib/k1s/images/ubuntu-22.04-k1s-gpu.qcow2`

## 2) Hypervisor preflight on Host B (libvirt + VFIO)

Run from Host A:

```bash
ssh "${SSH_OPTS[@]}" "$REMOTE_SSH" '
  set -euo pipefail
  echo "== host and kvm =="
  hostname
  egrep -wo "vmx|svm" /proc/cpuinfo | head -n1
  lsmod | egrep "kvm|vfio" || true
  echo "== iommu =="
  dmesg | egrep -i "DMAR|IOMMU" | tail -n 10 || true
  echo "== nvidia pci devices =="
  lspci -nn | egrep -i "NVIDIA|VGA|3D|Audio" || true
'
```

Set `GPU_BDF` and `GPU_AUDIO_BDF` from this output, then confirm VFIO binding:

```bash
ssh "${SSH_OPTS[@]}" "$REMOTE_SSH" "
  set -euo pipefail
  readlink /sys/bus/pci/devices/$GPU_BDF/driver || true
  readlink /sys/bus/pci/devices/$GPU_AUDIO_BDF/driver || true
"
```

Expected
- IOMMU is enabled.
- Both GPU functions are bindable to `vfio-pci` (or already bound).

## 3) Create and boot the Ubuntu LTS GPU VM on Host B

Create cloud-init payload on Host A:

```bash
mkdir -p "runs/$RUN_ID/cloud-init"
PUBKEY="$(cat "${SSH_KEY_PATH:-$HOME/.ssh/id_rsa}.pub")"

cat > "runs/$RUN_ID/cloud-init/user-data" <<EOF
#cloud-config
hostname: $VM_NAME
users:
  - name: ae
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $PUBKEY
package_update: true
packages:
  - qemu-guest-agent
  - jq
runcmd:
  - systemctl enable qemu-guest-agent || true
  - systemctl start qemu-guest-agent || true
EOF

cat > "runs/$RUN_ID/cloud-init/meta-data" <<EOF
instance-id: iid-$VM_NAME
local-hostname: $VM_NAME
EOF

cat > "runs/$RUN_ID/cloud-init/network-config" <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    lan0:
      match:
        name: "en*"
      dhcp4: false
      addresses: [$VM_IP/$VM_CIDR]
      routes:
        - to: default
          via: $VM_GW
      nameservers:
        addresses: [$VM_DNS1, $VM_DNS2]
EOF
```

Copy payload and create VM on Host B:

```bash
scp "${SSH_OPTS[@]}" runs/$RUN_ID/cloud-init/user-data runs/$RUN_ID/cloud-init/meta-data runs/$RUN_ID/cloud-init/network-config "$REMOTE_SSH:/tmp/"

ssh "${SSH_OPTS[@]}" "$REMOTE_SSH" <<EOF
set -euo pipefail
sudo mkdir -p /var/lib/k1s/images
sudo mkdir -p /var/lib/libvirt/images/$VM_NAME

BASE_IMG="/var/lib/k1s/images/ubuntu-22.04-k1s-gpu.qcow2"
VM_DISK="/var/lib/libvirt/images/$VM_NAME/${VM_NAME}.qcow2"
VM_SEED="/var/lib/libvirt/images/$VM_NAME/${VM_NAME}-seed.iso"

sudo qemu-img create -f qcow2 -F qcow2 -b "\$BASE_IMG" "\$VM_DISK" 80G
sudo cloud-localds --network-config /tmp/network-config "\$VM_SEED" /tmp/user-data /tmp/meta-data

sudo virt-install \
  --name "$VM_NAME" \
  --import \
  --memory 24576 \
  --vcpus 8 \
  --cpu host-passthrough \
  --machine q35 \
  --boot uefi \
  --disk path="\$VM_DISK",format=qcow2,bus=virtio \
  --disk path="\$VM_SEED",device=cdrom \
  --network bridge="$VM_BRIDGE",model=virtio \
  --hostdev "$GPU_BDF" \
  --hostdev "$GPU_AUDIO_BDF" \
  --graphics none \
  --noautoconsole

sudo virsh start "$VM_NAME" || true
sudo virsh dominfo "$VM_NAME"
EOF
```

Wait for SSH from Host A:

```bash
for _ in $(seq 1 90); do
  if ssh "${SSH_OPTS[@]}" "ae@$VM_IP" "echo up" >/dev/null 2>&1; then
    echo "vm ssh ready"
    break
  fi
  sleep 2
done
```

## 4) Guest preflight (inside remote VM)

```bash
ssh "${SSH_OPTS[@]}" "ae@$VM_IP" "hostname; ip -4 -brief addr; nvidia-smi -L"
ssh "${SSH_OPTS[@]}" "ae@$VM_IP" "cd $REMOTE_REPO && AE_CRI_RUNTIME_HANDLER=nvidia scripts/cri_preflight.sh"
```

Expected
- RTX-6000 appears in `nvidia-smi`.
- CRI preflight passes with runtime handler `nvidia`.

## 5) Start core on Host A and register `edge-b` site credentials

Start core:

```bash
mkdir -p "runs/$RUN_ID/logs" "runs/$RUN_ID/pids"
nohup sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  make k1s-core-cri > "runs/$RUN_ID/logs/core.log" 2>&1 &
echo $! > "runs/$RUN_ID/pids/core.pid"
```

Quick readiness check:

```bash
ss -ltn | egrep ":9108|:9110|:7422" || true
```

Register edge site creds on hub without starting local edge NATS on Host A:

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  REGISTER_ONLY=1 \
  SITE_ID="$SITE_ID" \
  ./scripts/dev/add_edge_site.sh
```

## 6) Start edge services in the remote VM (`edge-b`)

From Host A:

```bash
ssh "${SSH_OPTS[@]}" "ae@$VM_IP" <<EOF
set -euo pipefail
cd "$REMOTE_REPO"
sudo -v
mkdir -p "runs/$RUN_ID/logs" "runs/$RUN_ID/pids"

nohup sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_SITE_ID="$SITE_ID" \
  AE_NODE_ID="$EDGE_GW_NODE_ID" \
  AE_CONTROLLER_URL="http://$CORE_HOST:9110" \
  AE_NATS_HUB_LEAF_HOST="$CORE_HOST" \
  AE_NATS_HUB_LEAF_PORT=7422 \
  make k1s-edge-core-cri > "runs/$RUN_ID/logs/edge-core.log" 2>&1 &
echo \$! > "runs/$RUN_ID/pids/edge-core.pid"

nohup sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID="$EDGE_NODE_ID" \
  AE_NODE_LABELS="role=worker,site=$SITE_ID,gpu.sku=$EDGE_GPU_SKU" \
  AE_CONTROLLER_URL="http://$CORE_HOST:9110" \
  AE_AGENT_ENDPOINT="http://$VM_IP:9112" \
  AE_AGENT_TOKEN="$AE_AGENT_TOKEN" \
  make k1s-edge-node > "runs/$RUN_ID/logs/edge-node.log" 2>&1 &
echo \$! > "runs/$RUN_ID/pids/edge-node.pid"
EOF
```

## 7) Validate connectivity and inference cell

On Host A:

```bash
ae nodes
ae nodes "$EDGE_NODE_ID"
```

Expected
- `edge-b` nodes show `Ready`.
- GPU labels include `gpu.present=true` and `gpu.count>=1`.

Inference validation:

```bash
export AE_INFERENCE_EXPERIMENTAL=1
ae cell apply -f specs/examples/inference/cell-b-single.yaml
ae cell status cell-b-single --json | tee "runs/$RUN_ID/cell-b-single.status.json"
```

Pass condition
- `phase=READY`
- No failing conditions for fabric/worker/api readiness.

## 8) Quick diagnostics (if not ready)

From Host A:

```bash
tail -n 200 "runs/$RUN_ID/logs/core.log" || true
ssh "${SSH_OPTS[@]}" "ae@$VM_IP" "tail -n 200 $REMOTE_REPO/runs/$RUN_ID/logs/edge-core.log || true"
ssh "${SSH_OPTS[@]}" "ae@$VM_IP" "tail -n 200 $REMOTE_REPO/runs/$RUN_ID/logs/edge-node.log || true"

ssh "${SSH_OPTS[@]}" "ae@$VM_IP" "sudo crictl --runtime-endpoint unix:///run/containerd/containerd.sock ps -a"
ssh "${SSH_OPTS[@]}" "ae@$VM_IP" "sudo ss -ltn | egrep ':4223|:8223|:9112|:5001' || true"
```

Common failure signatures
- `nvidia` runtime missing in CRI preflight: guest toolkit/runtime handler issue.
- node not registering: wrong `AE_CONTROLLER_URL` or `AE_AGENT_TOKEN`.
- gateway unstable: leaf uplink cannot reach `CORE_HOST:7422`.

## 9) Teardown

Stop k1s processes:

```bash
ssh "${SSH_OPTS[@]}" "ae@$VM_IP" "cd $REMOTE_REPO && sudo -E make down || true"
sudo -E make down || true
```

Optional VM stop/remove:

```bash
ssh "${SSH_OPTS[@]}" "$REMOTE_SSH" "sudo virsh destroy $VM_NAME || true"
ssh "${SSH_OPTS[@]}" "$REMOTE_SSH" "sudo virsh undefine $VM_NAME --nvram || true"
```

## Appendix: extend to Host C (`edge-c`)

Use the same sequence with:
- `SITE_ID=edge-c`
- `EDGE_GW_NODE_ID=edge-c-gw`
- `EDGE_NODE_ID=edge-c--gpu-1`
- VM IP and GPU SKU updated for Host C

Then validate cross-site cells:

```bash
ae cell apply -f specs/examples/inference/cell-bc-pp2-ray.yaml
ae cell status cell-bc-pp2-ray --json
ae cell apply -f specs/examples/inference/cell-bc-pp2-mp.yaml
ae cell status cell-bc-pp2-mp --json
```
