# ADR 0003 — Multi-node CI smoke via QEMU/libvirt

Date: 2025-12-15
Status: Accepted
Owners: controller/runtime/ci

## Context
- We need a reproducible, multi-node smoke test that mirrors real deployments (systemd + Docker/Podman) and validates Service VIP routing and reschedule behavior.
- KinD-based jobs are useful for quick gating, but do not exercise the same OS/runtime surface as full VMs.

## Decision
- Use a QEMU/libvirt-based 3-VM topology (1 controller + 2 workers) as the canonical multi-node CI smoke.
- Implement the flow in `ops/ci/multinode-qemu.sh` with cloud-init, a shared repo mount, and SSH-driven orchestration.
- Gate the job behind KVM-capable runners and an explicit env flag (e.g., `AE_CI_MULTINODE_QEMU=1`), with optional overlay/WireGuard coverage.

## Consequences
- CI requires KVM-capable runners and VM image caching.
- Failure artifacts must include controller/agent logs plus `ae nodes/services/events` snapshots.
- KinD remains a fast path, but the QEMU job is the authoritative multi-node verification.
