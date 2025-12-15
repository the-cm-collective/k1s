# Multi-node CI Test Plan (QEMU/libvirt)

Goal: reproducible two-worker multinode smoke that mirrors real deployment (systemd, Docker/Podman, overlay optional), runnable on a KVM-capable CI runner.

## Overview
- 3 VMs: 1 controller, 2 workers.
- Ubuntu 24.04 cloud images (qcow2) with cloud-init for user + Docker install.
- Controller runs `ae.controller` with Service proxy enabled; workers run `ae-node` agents against per-VM Docker.
- Default path uses bridge service provider (no WireGuard). Optional overlay/WireGuard is feature-gated.
- Test flow: apply `specs/examples/echo-multinode.yaml` → verify ready + VIP reachability → kill one worker agent → ensure reschedule → curl VIP again → delete app.

## Prereqs (dev workstation or KVM CI runner)
- KVM available: `/dev/kvm` present and usable (`kvm-ok` or `lsmod | grep kvm`).
- Packages: `qemu-system-x86_64`, `qemu-utils`, `cloud-image-utils` (for `cloud-localds`), `iproute2`, `bridge-utils` (optional), `wget`, `jq`, `ssh`.
- Ubuntu 24.04 cloud image downloaded to `.cache/images/ubuntu-24.04-server-cloudimg-amd64.img`:
  ```
  mkdir -p .cache/images
  wget -O .cache/images/ubuntu-24.04-server-cloudimg-amd64.img \
    https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img
  ```
- SSH key available for cloud-init: default uses `~/.ssh/id_rsa.pub`; override with `SSH_PUB_KEY` if needed.
- Sudo access to create tap/bridge devices.

## Fast start for developers
```
# 1) Download base image (once)
mkdir -p .cache/images
wget -O .cache/images/ubuntu-24.04-server-cloudimg-amd64.img \
  https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img

# 2) Start the lab (creates bridge, seeds, boots 3 VMs, starts controller/agents)
AE_TOKEN=dev-token \
BASE_IMG=.cache/images/ubuntu-24.04-server-cloudimg-amd64.img \
ops/ci/multinode-qemu.sh start

# 3) Run the smoke from the controller VM
ssh ae@192.168.152.10 "export AE_STATE_DB=/home/ae/state/controller.db; \
  python3 -m ae.cli apply -f /mnt/host/specs/examples/echo-multinode.yaml \
  && python3 -m ae.cli status echo-mn --watch --timeout 120 \
  && vip=\$(python3 -m ae.cli services --json | jq -r '.[0].cluster_ip') \
  && curl -s --max-time 5 http://\$vip:8080/healthz"

# 4) Simulate worker loss and verify reschedule
ssh ae@192.168.152.11 "sudo pkill -f 'ae.node.server' || sudo pkill -f 'ae.node' || true"
ssh ae@192.168.152.10 "export AE_STATE_DB=/home/ae/state/controller.db; \
  python3 -m ae.cli status echo-mn --watch --timeout 120"
ssh ae@192.168.152.10 "export AE_STATE_DB=/home/ae/state/controller.db; \
  vip=\$(python3 -m ae.cli services --json | jq -r '.[0].cluster_ip'); \
  curl -s --max-time 5 http://\$vip:8080/healthz"

# 5) Stop the lab and clean taps/bridge
ops/ci/multinode-qemu.sh stop
```

## VM topology
- Network: libvirt NAT network `k1s-net` (192.168.152.0/24, DHCP) plus host port forwards for SSH.
- Host forwards:
  - Controller SSH: 10022 → controller:22
  - Worker1 SSH: 10023 → worker1:22
  - Worker2 SSH: 10024 → worker2:22
- Resources: 2 vCPU, 2–3 GiB RAM per VM; 10 GiB qcow2 overlay.

## Artifacts
- On failure, collect: controller log, agent logs (journalctl), `ae nodes --json`, `ae services --json`, `ae events echo-mn --limit 50`, and the test script output.

## Implementation steps
1) **Images & seeds**
   - Cache `ubuntu-24.04-server-cloudimg-amd64.img` under `.cache/images/`.
   - Create base qcow2 overlay per VM: `qemu-img create -f qcow2 -b base.img controller.qcow2`.
   - Generate cloud-init seed ISOs with:
     - user `ae` + injected SSH key.
     - `runcmd` to install Docker (`apt-get install docker.io`), enable/start service.
     - Copy repo (or mount host via 9p/virtiofs) into `/home/ae/k1s`.
2) **Launch script** (`ops/ci/multinode-qemu.sh`)
   - Creates libvirt network if missing.
   - Boots controller + workers with `qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 -drive file=... -cdrom seed.iso -netdev user,hostfwd=tcp::10022-:22 ... -device virtio-net-pci,netdev=...`.
   - Waits for SSH to be reachable on all VMs.
3) **Configure and run test** (host-driven via SSH; set `AE_STATE_DB=/home/ae/state/controller.db` for CLI lookups)
   - Controller:
     ```
     AE_ENABLE_SERVICE_PROXY=1 AE_SERVICE_PROVIDER=bridge \
     AE_SERVICE_IP_POOL=10.241.0.0/16 AE_POD_CIDR_POOL=10.42.0.0/16 \
     AE_AGENT_API_PORT=9110 AE_AGENT_API_TOKEN=REDACTED"sudo pkill -f 'ae.node.server' || true"
     python3 -m ae.cli status echo-mn --watch --timeout 120
     vip=$(python3 -m ae.cli services --json | jq -r '.[0].cluster_ip')
     curl -s --max-time 5 http://$vip:8080/healthz
     python3 -m ae.cli delete echo-mn
     ```
4) **WireGuard optional path**
   - Add feature flag `AE_CI_OVERLAY=1`.
   - Install `wireguard-tools` in cloud-init, pass `/dev/net/tun` to qemu (default).
   - Pre-render wg configs in seed; set controller `AE_SERVICE_PROVIDER=overlay` and workers `AE_AGENT_CONFIGURE_OVERLAY=true`.
   - Keep MTU at 1400.
5) **CI wiring**
   - GitHub Actions job gated on `kvm` self-hosted runners:
     - Step 1: prepare images/seeds (cached).
     - Step 2: run `ops/ci/multinode-qemu.sh start`.
     - Step 3: run the SSH-driven test script.
     - Step 4: on success, `ops/ci/multinode-qemu.sh stop`; on failure, gather artifacts and then stop.
   - Add `AE_CI_MULTINODE_QEMU=1` env gate so PR jobs can skip when KVM not available; enable on nightly/merge queues with KVM runners.
   - Tests to run in the job:
     - `ae status echo-mn --watch 5 --timeout 120` to wait for readiness
     - `ae services list --json` to grab the VIP
     - `curl http://<vip>:8080/healthz` before/after killing one agent
     - `pytest tests/integration/test_multinode_agent_flow.py tests/integration/test_service_vip_routing.py` (docker-enabled env)

## Defaults and conventions
- Repo mount: 9p mount of host repo into `/mnt/host` (smaller seeds, faster iterations).
- Runtime in guests: rootful Docker by default; Podman rootful is also supported if preferred by setting `AE_RUNTIME_BACKEND=podman` in the agent start command.
- Resources: 2 vCPU / 2–3 GiB RAM per VM; adjust via `VM_CPUS`/`VM_MEM` if CI is slow.
- Network: bridge service provider for the fast path; enable overlay by setting `ENABLE_OVERLAY=1` + `AE_CI_OVERLAY=1` (requires wireguard-tools in guests).

## Helper script quickstart
- Script: `ops/ci/multinode-qemu.sh start|stop`
- Host prereqs: KVM (`/dev/kvm`), `qemu-system-x86_64`, `cloud-localds` (cloud-image-utils), `iproute2`, sudo for tap/bridge setup, and the Ubuntu 24.04 cloud image at `.cache/images/ubuntu-24.04-server-cloudimg-amd64.img`.
- What `start` does:
  - Creates bridge `k1s-br0`, tap devices, qcow2 overlays, and cloud-init seeds with static IPs 192.168.152.10/11/12.
  - Boots controller + two workers, mounts the host repo via 9p, installs the ae package, then starts controller and agents using the bridge service provider.
  - Exposes controller agent API on port 9110 (guest network). SSH with `ssh ae@192.168.152.10` using your host key.
  - Optional: set `RUN_SMOKE=1` to auto-run the multi-node smoke (apply → VIP curl → kill worker1 agent → reschedule → VIP curl).
- What `stop` does: kills controller/agent processes, stops QEMU VMs, removes taps/bridge (best effort).
- Tunables (env): `BASE_IMG`, `STATE_DIR`, `VM_MEM/VM_CPUS`, `CTRL_IP/WK1_IP/WK2_IP`, `AE_TOKEN`, `ENABLE_OVERLAY=1` (installs wireguard-tools in guests), `SSH_KEY_PATH` / `SSH_PUB_KEY`.
- Example on a KVM runner:
  ```
  BASE_IMG=.cache/images/ubuntu-24.04-server-cloudimg-amd64.img \
  AE_TOKEN=ci-token \
  ops/ci/multinode-qemu.sh start

  ssh ae@192.168.152.10 "python -m ae.cli apply -f /mnt/host/specs/examples/echo-multinode.yaml && ae status echo-mn --watch --timeout 120"

  ops/ci/multinode-qemu.sh stop
  ```
