# GPU Fabric + CRI Edge Ops Pattern (A/B/C LAN)

Purpose
- Stand up the primary `F0n-nvidia-dev` physical-host lane first:
  - Host A: `k1s-core` on the NixOS workstation plus one dedicated Ubuntu GPU guest with TITAN RTX passthrough, registered as `core-a--hub`.
  - Host B: `k1s-edge-core` plus GPU edge node on the second physical workstation.
- Extend to a 3-host LAN pattern later when you want the older A/B/C cross-edge pattern:
  - Host C: optional second edge site with one more GPU node.
- Validate `InferenceCell` execution with Ray primary and mp fallback.
- Use LAN-direct first, then optional WG follow-up.

This guide complements [Nvidia Development Baseline](nvidia-development-baseline.html), which defines the public two-host A/B baseline for the current Nvidia development subtrack.

Related VM Lab Docs
- Nvidia physical-host baseline: [Nvidia Development Baseline](nvidia-development-baseline.html)
- Host A libvirt passthrough guest: [Host A Linux GPU Guest](host-a-linux-gpu-guest.html)
- Canonical Host A strict-CRI retest flow: [Host A Strict-CRI Retest](host-a-strict-cri-retest.html)
- Golden image pipeline (Ubuntu 22.04 GA / kernel 5.15): `docs/ops/vm-golden-image-pipeline.md`
- Variant orchestration and bootstrap runbook: `docs/ops/vm-variant-runbook.md`
- Baseline + throughput gating workflow: `docs/ops/vm-metrics-and-gates.md`
- Remote GPU VM bring-up precursor (A+B over SSH): `docs/ops/gpu-vm-remote-host-validation.md`

Current capability baseline
- Node agent auto-discovers typed accelerator facts from `nvidia-smi` and projects compatibility `gpu.*` labels from the same inventory.
- Deployment manifests support `spec.runtimeClassName`; CRI maps this to pod sandbox `runtime_handler`.
- Inference cell executor supports `executor.runtimeClassName`; generated worker/leader workloads inherit it.
- Inference examples under `specs/examples/inference/` are set to `runtimeClassName: nvidia`.

Validation boundaries
- `scripts/lab/vm/labctl.sh image verify --variant gpu` remains an image-contract check only.
- `python scripts/dev/gpu_guest_passthrough_validate.py validate ...` is the host-coupled passthrough proof for the NixOS TITAN guest.
- `ae cell ...` and `python scripts/dev/f0n_nvidia_validate.py collect ...` are the fabric and controller evidence steps.

Endpoint policy

| Lane | `AE_CONTROLLER_URL` | `AE_AGENT_ENDPOINT` |
| --- | --- | --- |
| Same LAN (this guide) | IP or resolvable hostname | IP or resolvable hostname |
| Remote / cloud | Prefer hostname + TLS | Prefer hostname + TLS |

Rules
- Do not use loopback (`127.0.0.1`) for cross-host endpoints.
- If TLS is enabled, set `AE_CONTROLLER_URL=https://...` and provide `AE_CONTROLLER_TLS_CA` on nodes.

## 1) GPU host preflight (hosts B and C)

```bash
nvidia-smi -L
AE_CRI_RUNTIME_HANDLER=nvidia scripts/cri_preflight.sh
```

Expected
- `nvidia-smi` lists GPUs.
- preflight reports `required runtime handler=nvidia` as available.

## 2) Bring up host A (core plus Ubuntu GPU guest)

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  make k1s-core-cri
```

Then verify the guest image contract, boot the Ubuntu GPU guest through the Host A libvirt helper, and prove passthrough before starting `core-a--hub`.

Use [Host A Strict-CRI Retest](host-a-strict-cri-retest.html) as the canonical copy/paste source for that sequence. The older abbreviated snippets on this page are intentionally not the source of truth anymore because the current Host A lane now depends on:
- rebuilding and validating the guest before starting the controller
- running `k1s-core-cri` with `POSTGRES_BIND_IP` and `POSTGRES_PORT=55432`
- installing guest repo deps before `make k1s-core-node`
- probing `http://<guest-primary-ip>:9111/v1/containers`
- verifying controller registration via `/v1/nodes/core-a--hub/overlay`

Background-only outline:

```bash
cp ops/dev/host-a-gpu.env.sample state/host-a-gpu.env
scripts/lab/vm/labctl.sh image verify --variant gpu
scripts/lab/vm/labctl.sh host-a-gpu preflight
scripts/lab/vm/labctl.sh host-a-gpu create-overlay
scripts/lab/vm/labctl.sh host-a-gpu create-seed
scripts/lab/vm/labctl.sh host-a-gpu define
scripts/lab/vm/labctl.sh host-a-gpu start
scripts/lab/vm/labctl.sh host-a-gpu ips --json
python scripts/dev/gpu_guest_passthrough_validate.py validate \
  --run-id "$RUN_ID" \
  --vm-name k1s-core-a-gpu \
  --expected-gpu "TITAN RTX" \
  --min-vram-gib 24
```

Rules for this lane
- The local `state/host-a-gpu.env` file defines which dedicated NIC and GPU PCI functions are passed through on this workstation.
- The guest needs exclusive access to the configured passthrough GPU and primary NIC device set.
- The guest's primary LAN IP on the passed-through NIC is the k1s validation target.
- The libvirt `default` NAT NIC is management and rescue only.
- The current CUDA smoke baseline for this lane is `nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1`.

Only after that passes, start the schedulable guest node on host A through the retest guide flow:

```bash
# Follow docs/ops/host-a-strict-cri-retest.md step C
```

## 3) Bring up host B (edge-core + GPU node)

```bash
sudo -E make edge-site-cri SITE_ID=edge-b EDGE_PORT=4224 EDGE_HTTP_PORT=8224
```

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_SITE_ID=edge-b \
  AE_NODE_ID=edge-b-gw \
  make k1s-edge-core-cri
```

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID=edge-b--gpu-1 \
  AE_NODE_LABELS="role=worker,site=edge-b,gpu.sku=rtx-8000" \
  AE_CONTROLLER_URL=http://core-a.lan:9110 \
  AE_AGENT_ENDPOINT=http://edge-b.lan:9112 \
  AE_AGENT_TOKEN=devtoken \
  make k1s-edge-node
```

## 4) Optional extension: bring up host C (edge-core + GPU node)

```bash
sudo -E make edge-site-cri SITE_ID=edge-c EDGE_PORT=4324 EDGE_HTTP_PORT=8324
```

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_SITE_ID=edge-c \
  AE_NODE_ID=edge-c-gw \
  make k1s-edge-core-cri
```

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID=edge-c--gpu-1 \
  AE_NODE_LABELS="role=worker,site=edge-c,gpu.sku=<gpu-model>" \
  AE_CONTROLLER_URL=http://core-a.lan:9110 \
  AE_AGENT_ENDPOINT=http://edge-c.lan:9112 \
  AE_AGENT_TOKEN=devtoken \
  make k1s-edge-node
```

## 5) Validate node capabilities

```bash
ae nodes
ae nodes core-a--hub
ae nodes edge-b--gpu-1
ae nodes edge-c--gpu-1   # only when host C is present
```

Expected
- The A/B physical-host nodes are `Ready`.
- Typed node capabilities include `capabilities.accelerators[]`.
- Compatibility labels include `gpu.present=true` and `gpu.count>=1`.

## 6) Inference cell checks

Enable execution mode:

```bash
export AE_INFERENCE_EXPERIMENTAL=1
```

Primary A/B checks:

```bash
ae cell apply -f specs/examples/inference/cell-a-single.yaml
ae cell status cell-a-single --json
ae cell apply -f specs/examples/inference/cell-b-single.yaml
ae cell status cell-b-single --json
ae cell apply -f specs/examples/inference/cell-ab-pp2-ray.yaml
ae cell status cell-ab-pp2-ray --json
ae cell apply -f specs/examples/inference/cell-ab-pp2-mp.yaml
ae cell status cell-ab-pp2-mp --json
```

Expanded A/B/C checks:

```bash
ae cell apply -f specs/examples/inference/cell-c-single.yaml
ae cell status cell-c-single --json
ae cell apply -f specs/examples/inference/cell-bc-pp2-ray.yaml
ae cell status cell-bc-pp2-ray --json
ae cell apply -f specs/examples/inference/cell-bc-pp2-mp.yaml
ae cell status cell-bc-pp2-mp --json
```

Expected
- `phase=READY`
- `allocations.active_executor` is `ray` or `mp`
- fabric + worker + leader + api conditions are `true`

## 7) Reliability loop and cleanup

Primary evidence helper:

```bash
python scripts/dev/f0n_nvidia_validate.py collect \
  --run-id f0n-review-001 \
  --vm-name k1s-core-a-gpu \
  --inventory "state/libvirt-host-a/k1s-core-a-gpu/inventory.json"
```

This captures the NixOS guest passthrough proof plus the canonical node inventory and apply/status/events/delete/reapply artifacts under `runs/<RUN_ID>/`.

```bash
for i in $(seq 1 20); do
  ae cell apply -f specs/examples/inference/cell-ab-pp2-ray.yaml || break
  ae cell delete cell-ab-pp2-ray
done
```

```bash
ae cell delete cell-a-single
ae cell delete cell-b-single
ae cell delete cell-ab-pp2-ray
ae cell delete cell-ab-pp2-mp
ae cell delete cell-c-single
ae cell delete cell-bc-pp2-ray
ae cell delete cell-bc-pp2-mp
```
