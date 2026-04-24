# GPU Fabric + CRI Edge Ops Pattern (A/B/C LAN)

Purpose
- Stand up the primary `F0n-nvidia-dev` physical-host lane first:
  - Host A: `k1s-core` plus schedulable GPU node on the local workstation.
  - Host B: `k1s-edge-core` plus GPU edge node on the second physical workstation.
- Extend to a 3-host LAN pattern later when you want the older A/B/C cross-edge pattern:
  - Host C: optional second edge site with one more GPU node.
- Validate `InferenceCell` execution with Ray primary and mp fallback.
- Use LAN-direct first, then optional WG follow-up.

This guide complements [Nvidia Development Baseline](nvidia-development-baseline.html), which defines the public two-host A/B baseline for the current Nvidia development subtrack.

Related VM Lab Docs
- Nvidia physical-host baseline: [Nvidia Development Baseline](nvidia-development-baseline.html)
- Golden image pipeline (Ubuntu 22.04 GA / kernel 5.15): `docs/ops/vm-golden-image-pipeline.md`
- Variant orchestration and bootstrap runbook: `docs/ops/vm-variant-runbook.md`
- Baseline + throughput gating workflow: `docs/ops/vm-metrics-and-gates.md`
- Remote GPU VM bring-up precursor (A+B over SSH): `docs/ops/gpu-vm-remote-host-validation.md`

Current capability baseline
- Node agent auto-discovers typed accelerator facts from `nvidia-smi` and projects compatibility `gpu.*` labels from the same inventory.
- Deployment manifests support `spec.runtimeClassName`; CRI maps this to pod sandbox `runtime_handler`.
- Inference cell executor supports `executor.runtimeClassName`; generated worker/leader workloads inherit it.
- Inference examples under `specs/examples/inference/` are set to `runtimeClassName: nvidia`.

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

## 2) Bring up host A (core plus schedulable GPU node)

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_INFRA_BACKEND=cri \
  AE_CRI_RUNTIME_HANDLER=runc \
  make k1s-core-cri
```

Primary schedulable node on host A:

```bash
sudo -E \
  AE_RUNTIME_BACKEND=cri \
  AE_CRI_ENDPOINT=unix:///run/containerd/containerd.sock \
  AE_NODE_ID=core-a--hub \
  AE_NODE_LABELS="role=hub,site=core-a,gpu.sku=titan-rtx" \
  AE_CONTROLLER_URL=http://core-a.lan:9110 \
  AE_AGENT_ENDPOINT=http://core-a.lan:9111 \
  AE_AGENT_TOKEN=devtoken \
  make k1s-core-node
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
python scripts/dev/f0n_nvidia_validate.py collect --run-id f0n-review-001
```

This captures the canonical node inventory plus apply/status/events/delete/reapply artifacts under `runs/<RUN_ID>/`.

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
