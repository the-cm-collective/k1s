# Host A Linux GPU Guest

Purpose
- Bring up the first Linux execution guest on Host A as a libvirt `qemu:///system` VM.
- Reuse the repo GPU qcow2 image flow while keeping PCI passthrough separate from the local `variant up` harness.
- Keep machine-specific PCI, IOMMU, GPU, and NIC values in an uncommitted local env file instead of committed script defaults.

Canonical retest flow
- Use [Host A Strict-CRI Retest](host-a-strict-cri-retest.html) for the exact copy/paste `A`/`B`/`C` sequence that rebuilds the guest, starts `k1s-core-cri`, and brings `core-a--hub` back to `Ready`.
- Keep this page focused on the local hardware profile, artifact layout, and passthrough topology.

Operating rules
- This guest requires exclusive access to the passed-through GPU functions and the dedicated primary passthrough NIC configured for your workstation.
- The guest uses libvirt on `qemu:///system`, not `scripts/lab/vm/variant_up.sh`.
- The primary guest data path is the passed-through LAN NIC. This is the LAN identity used for `AE_AGENT_ENDPOINT`, controller reachability, and validation.
- The secondary virtio NIC on libvirt `default` NAT is rescue and bootstrap only. Do not treat it as the published k1s endpoint.
- The guest overlay remains in `~/VMs/` by default and uses `artifacts/images/ubuntu-22.04-k1s-gpu.qcow2` as the default backing image.

Artifacts
- Local machine profile: `state/host-a-gpu.env`
- Sample template: `ops/dev/host-a-gpu.env.sample`
- Rendered XML and cloud-init files: `state/libvirt-host-a/k1s-core-a-gpu/`
- qcow2 overlay: `~/VMs/k1s-core-a-gpu.qcow2`
- seed ISO: `~/VMs/k1s-core-a-gpu-seed.iso`
- guest IP inventory for validator reuse: `state/libvirt-host-a/k1s-core-a-gpu/inventory.json`

## 1) Create the local machine profile

Copy the tracked sample into `state/`, then fill in the PCI and interface values for the workstation you are testing on:

```bash
cp ops/dev/host-a-gpu.env.sample state/host-a-gpu.env
```

Required local values
- `HOST_A_GPU_PRIMARY_NIC_NAME` or `HOST_A_GPU_PRIMARY_NIC_PCI`
- `HOST_A_GPU_GPU_PCI`
- `HOST_A_GPU_GPU_AUDIO_PCI`

Useful optional values
- `HOST_A_GPU_EXPECTED_PRIMARY_IOMMU_GROUP`
- `HOST_A_GPU_PRIMARY_NIC_MAC`
- `HOST_A_GPU_BASE_IMAGE`
- `HOST_A_GPU_OVERLAY_DIR`
- `HOST_A_GPU_STATE_ROOT`
- `HOST_A_GPU_OVMF_CODE`
- `HOST_A_GPU_OVMF_VARS_TEMPLATE`

Behavior
- `scripts/lab/vm/labctl.sh host-a-gpu ...` auto-loads `state/host-a-gpu.env` when it exists.
- Override the path with `HOST_A_GPU_ENV_FILE=/path/to/file.env` or `--env-file /path/to/file.env`.
- CLI flags win over process env, process env wins over the env file, and the env file wins over generic committed defaults.

## 2) Host preflight

Run the host-side gate before defining the guest:

```bash
scripts/lab/vm/labctl.sh host-a-gpu preflight
```

Expected
- the dedicated passthrough NIC is isolated in the intended IOMMU group when `HOST_A_GPU_EXPECTED_PRIMARY_IOMMU_GROUP` is set
- the passed-through GPU functions are bound to `vfio-pci`
- free `2 MiB` hugepages remain sufficient for `16 GiB`

If you do not set `HOST_A_GPU_EXPECTED_PRIMARY_IOMMU_GROUP`, the helper reports the observed group without failing purely because no expected value was configured.

## 3) Render the guest, create the overlay, and create the seed ISO

```bash
scripts/lab/vm/labctl.sh host-a-gpu render
scripts/lab/vm/labctl.sh host-a-gpu create-overlay
scripts/lab/vm/labctl.sh host-a-gpu create-seed
```

Notes
- `render` writes the libvirt domain XML plus the cloud-init `user-data`, `meta-data`, and `network-config`.
- `create-overlay` can run before you finish passthrough-device configuration because it only depends on the backing image path and overlay settings.
- `render` and `create-seed` require a complete local hardware profile because the emitted XML and network config include the selected PCI and NIC values.
- The helper writes `/etc/default/k1s-host-a-gpu` in the guest so the node id and `AE_CRI_RUNTIME_HANDLER=nvidia` expectation are visible inside the VM.

## 4) Define and start the libvirt guest

```bash
scripts/lab/vm/labctl.sh host-a-gpu define
scripts/lab/vm/labctl.sh host-a-gpu start
```

Before starting this guest, make sure no other process is holding the passed-through GPU or the dedicated passthrough NIC.

## 5) Wait for qemu-guest-agent and print both IPs

The management NIC should come up first, then the reserved DHCP lease on the passed-through LAN NIC.

```bash
scripts/lab/vm/labctl.sh host-a-gpu ips --json
```

Use the emitted `inventory.json` in later validation steps:
- `primary_ip`: the passed-through LAN address
- `management_ip`: the libvirt `default` NAT address

The validation and node bring-up steps below use `primary_ip` first. Only fall back to `management_ip` when you need rescue access.

## 6) Guest passthrough validation

Keep `image verify` as the qcow boot-contract gate, then run the guest-coupled passthrough validator after the guest boots:

```bash
scripts/lab/vm/labctl.sh image verify --variant gpu

python scripts/dev/gpu_guest_passthrough_validate.py validate \
  --run-id "$RUN_ID" \
  --vm-name k1s-core-a-gpu \
  --inventory state/libvirt-host-a/k1s-core-a-gpu/inventory.json \
  --guest-repo /mnt/host \
  --expected-gpu "TITAN RTX" \
  --min-vram-gib 24
```

Current smoke-image baseline
- The validator default compute image is `nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1` for the current Host A guest driver stack.

If the repo is not mounted at `/mnt/host`, pass the checked-out path that exists inside the guest with `--guest-repo`. A practical fallback is to sync `scripts/` into `/home/ae/k1s` and run the validator with `--guest-repo /home/ae/k1s`.

Do not continue to `make k1s-core-node` until this validator passes.

## 7) Start `core-a--hub` inside the guest

Use the guest's primary LAN IP for `AE_AGENT_ENDPOINT`, not the management NIC.

The current validated command sequence now lives on [Host A Strict-CRI Retest](host-a-strict-cri-retest.html). Use that page instead of the older direct `make k1s-core-node` snippet here because the strict-CRI retest lane now depends on:
- the host-side `k1s-core-cri` controller flow with `POSTGRES_BIND_IP` and `POSTGRES_PORT=55432`
- installing guest repo deps from `requirements.in` before the node starts
- probing `http://<primary-lan-ip>:9111/v1/containers` instead of `/readyz`
- confirming registration via the controller agent API overlay endpoint

Minimal intent reminder:

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_CRI_RUNTIME_HANDLER=nvidia \
  AE_NODE_ID=core-a--hub \
  AE_NODE_LABELS="role=hub,site=core-a,gpu.sku=titan-rtx" \
  AE_CONTROLLER_URL=http://core-a.lan:9110 \
  AE_AGENT_ENDPOINT=http://<primary-lan-ip>:9111 \
  AE_AGENT_TOKEN=devtoken \
  make k1s-core-node
```

Expected
- `AE_AGENT_ENDPOINT` resolves to the LAN IP on the passed-through NIC
- the management NIC is not used for published k1s traffic
- later `F0n` A/B checks run against the primary LAN identity

## 8) Stop or remove the guest

Graceful stop:

```bash
scripts/lab/vm/labctl.sh host-a-gpu stop
```

Forced stop:

```bash
scripts/lab/vm/labctl.sh host-a-gpu stop --force
```

Undefine the domain but keep the overlay and seed artifacts:

```bash
scripts/lab/vm/labctl.sh host-a-gpu undefine
```

Undefine and remove the generated artifacts:

```bash
scripts/lab/vm/labctl.sh host-a-gpu undefine --purge-artifacts
```
