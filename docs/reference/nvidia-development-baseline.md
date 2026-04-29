# Nvidia Development Baseline

Status: canonical public baseline for the secondary Nvidia fabric development lane.

This page defines the practical Nvidia-backed development baseline that can be used to advance fabric work before the AMD AI Max+ 395 systems arrive. It complements [Inference Fabric](inference-fabric.html), [Distributed Compute Fabric Roadmap](distributed-compute-fabric.html), and [Fabric Control Plane](fabric-control-plane.html).

## Summary

The formal fabric program remains AMD-first. The Nvidia lane exists so `k1s` can keep moving on controller-owned fabric hardening and early typed-capability work with hardware that is already in hand.

This development lane is intentionally bounded:

- it is a secondary public development subtrack, not a replacement for the AMD mainline
- it uses the two physical Nvidia hosts that are available now
- it stays on standard Ethernet
- it does not satisfy AMD `D0`
- it does not make vGPU or TDM part of the current milestone evidence

## Physical Host Baseline

The current public Nvidia development baseline is one two-host A/B shape:

| Host | Role | Node IDs | GPU |
| --- | --- | --- | --- |
| NixOS workstation | `core-a` controller plus one Ubuntu guest with Linux GPU passthrough | `core-a--hub` | TITAN RTX 24 GB |
| Ubuntu Server workstation | `edge-b` plus GPU worker node | `edge-b-gw`, `edge-b--gpu-1` | RTX-8000 48 GB |

This is the first practical `F0n-nvidia-dev` evidence lane:

- one single-node execution check on the NixOS-hosted TITAN passthrough guest
- one single-node execution check on the Ubuntu RTX-8000 host
- one two-host `pp=2` execution check across both hosts

The baseline is about controller and fabric behavior, not product-family claims. These hosts are close enough to exercise the current `InferenceCell` lifecycle, node registration, GPU slot reservation, readiness, restart, and teardown flows while the AMD mainline hardware is still unavailable.

## Current Development Hardware

Publishing the exact development hardware here is intentional. The point is not to promote one workstation shape. The point is to make the current dev substrate explicit enough that another operator can understand what is being exercised, what is dedicated to passthrough, and which parts of the lane are still workstation-specific.

| Host | Current role | Development hardware notes |
| --- | --- | --- |
| Host A | `core-a` controller plus one Linux passthrough guest | Current NixOS workstation based on an Intel Hades Canyon NUC. It uses dual interfaces so one NIC can stay with the host and one dedicated NIC can be handed to the guest. |
| Host A guest | `core-a--hub` execution node | Ubuntu guest on libvirt `qemu:///system`, Q35 + OVMF, host-passthrough CPU, hugepages, locked memory, and one passed-through TITAN RTX with its audio function. The guest keeps one virtio management NIC on libvirt `default` for rescue/bootstrap only. |
| Host B | `edge-b` site plus GPU worker | Ubuntu Server workstation with one RTX-8000 48 GB as the current second physical Nvidia lane. |

Current Host A posture
- one dedicated LAN NIC is reserved for passthrough testing and is the primary validation and `AE_AGENT_ENDPOINT` identity for the guest
- one separate host-facing NIC remains with the workstation for normal host access and controller duties
- the current guest shape passes through the TITAN RTX GPU function plus the associated audio function first, keeping auxiliary TITAN functions out of the first Linux compute slice
- the A/B development lane is therefore split between a host-owned control role on Host A and a guest-owned execution role on the same physical box

## Relationship to the AMD Mainline

This baseline does not change the formal roadmap order:

- `F0n-nvidia-dev` is a secondary development and validation lane
- `F0` remains the AMD-first fabric hardening milestone
- `F1` remains the first typed-facts milestone for the mainline roadmap
- `D0` remains one repeatable AI Max+ 395 cell on the documented AMD hardware baseline

The practical meaning is simple:

- Nvidia evidence can de-risk controller and fabric logic
- Nvidia evidence can inform the first typed accelerator fact shape
- Nvidia evidence does not close AMD execution-baseline milestones on its own

For the AMD public baseline, use [AI Max+ 395 Hardware Baseline](ai-max-395-hardware-baseline.html). For the AMD cell bring-up sequence, use [AI Max+ 395 Cluster Prep](ai-max-395-cluster-prep.html).

## Accelerator Fact Shape Reserved for F1

The existing `gpu.*` labels remain useful for the current Nvidia lane, but they should not become the long-term control-plane contract. `F1` should introduce typed accelerator facts that already account for:

- discrete GPUs
- APUs with unified memory
- virtual or partitioned GPU surfaces

The first reserved shape should allow one node to report one or more homogeneous execution pools:

```yaml
capabilities:
  accelerators:
    - id: titan-rtx-0
      kind: discrete_gpu | apu | virtual_gpu
      vendor: nvidia | amd
      family: TITAN RTX
      architecture: TU102
      device_count: 1
      memory_model: dedicated | unified | partitioned
      memory_bytes_per_device: 25769803776
      runtime_handlers:
        - nvidia
      partitioning_mode: none | mig | vgpu | tdm | sriov
      backing_device_id: null
      execution_role: execution | mixed | management_only
```

Important boundaries for this shape:

- `kind=apu` reserves unified-memory accelerator nodes without requiring APU-specific scheduling semantics in `F0n`
- `kind=virtual_gpu` reserves partitioned or virtual slices without making vGPU a current evidence requirement
- `partitioning_mode` exists so later MIG, vGPU, TDM, or SR-IOV variants do not require a contract break
- current `gpu.*` labels remain a backward-compatibility projection until old flows migrate

Compatibility projection guidance:

- `gpu.present=true` when any execution-capable accelerator exists
- `gpu.count` is the sum of execution-capable device count
- `gpu.models` is a human-readable summary of accelerator family names
- manual `gpu.sku` lab labels remain acceptable in examples and variants, but they are not the long-term control contract

## Validation Surface

The primary operational evidence for this baseline should come from:

- single-node and two-host `InferenceCell` checks using the A/B manifests under `specs/examples/inference/`
- the Host A libvirt passthrough procedure in [Host A Linux GPU Guest](host-a-linux-gpu-guest.html), which keeps the TITAN guest, the dedicated passthrough NIC policy, the local `state/host-a-gpu.env` workflow, and the management-NIC fallback path explicit
- the guest-coupled `scripts/dev/gpu_guest_passthrough_validate.py` helper, which proves guest attach, CRI runtime readiness, and seeded CUDA execution for the NixOS TITAN lane
- the operator-run `scripts/dev/f0n_nvidia_validate.py` helper, which captures the passthrough phase plus node inventory and apply/status/events/delete/reapply artifacts under `runs/<RUN_ID>/`
- the existing LAN GPU procedures in `docs/ops/gpu-fabric-abc-lan.md`, updated to treat the physical A/B lane as the first `F0n` evidence surface
- the remote VM GPU procedure in `docs/ops/gpu-vm-remote-host-validation.md`, treated as a supplemental validation surface rather than the main milestone lane

## Non-Goals

This baseline does not:

- redefine the formal AMD-first roadmap
- make the Nvidia lane a deployment milestone
- require vGPU or TDM validation now
- define the full final multi-domain typed heartbeat or API schema beyond the current accelerator slice
- define APU-specific or virtual-GPU-specific placement policy

Those details belong to the later `F1` implementation work after the current `F0n` development lane has produced stable evidence.
