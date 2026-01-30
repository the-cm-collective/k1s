# CRI parity tracking (containerd)

Last updated: 2026-01-30

Goal: make CRI/containerd a safe default backend for k1s dev/labs with a
Kubernetes-aligned registry-first image flow and core runtime parity vs Podman.

## Current parity snapshot (done)

- Runtime adapter core: create/update/remove pods, init/sidecars, exec sync, logs.
- Streaming exec/attach/port-forward via node agent + crictl.
- Registry-first flow: in-cluster NodePort registry, TLS+htpasswd, delete enabled.
- CRI image helpers and registry HTTP helpers (`ae cri images`, `ae cri registry`).
- Build pipeline without Podman (buildkit-only pod + crictl-only ops pod).
- HostPath + emptyDir semantics, hostAliases, DNS config, security context mapping.
- CRI smoke + NodePort smoke scripts (gated for CRI nodes).

## P0: Required for CRI as default backend (dev/labs)

- [x] CRI runtime adapter core (pods, init/sidecars, logs, exec sync).
  - Evidence: `src/ae/runtime/cri_runtime.py`
- [x] Streaming exec/attach/port-forward via node agent + crictl.
  - Evidence: `src/ae/runtime/remote_runtime.py`, `src/ae/runtime/cri_runtime.py`
  - Tests: `tests/integration/test_agent_streaming_proxy.py`, `tests/integration/test_apishim_agent_streaming.py`
- [x] Registry-first flow with in-cluster NodePort registry + TLS/htpasswd + delete enabled.
  - Evidence: `ops/dev/registry-k8s.yaml`, `CRI-REG.md`, `docs/reference/cri-containerd.md`
- [x] Containerd trust helper for TLS/insecure registries.
  - Evidence: `scripts/containerd_registry_trust.sh`, `ae cri trust` in `src/ae/cli/__main__.py`
- [x] CRI image helpers + registry HTTP helpers (list/pull/rm/tag/delete).
  - Evidence: `src/ae/cli/__main__.py`
- [x] Build pipeline without Podman (buildkit-only pod + crictl-only ops pod), built/pushed on demo/labs.
  - Evidence: `scripts/build_cri_buildkit_image.sh`, `scripts/build_cri_crictl_image.sh`, `scripts/init_demo.sh`
  - Manifests: `specs/examples/cri-buildkit-k8s.yaml`, `specs/examples/cri-crictl-k8s.yaml`
- [x] Socket access helper for non-root dev sessions.
  - Evidence: `scripts/containerd_socket_access.sh`, `docs/reference/cri-containerd.md`
- [x] Service exposure for CRI nodes (NodePort/ClusterIP via iptables proxy, optional).
  - Evidence: `src/ae/network/provider_iptables.py`, `src/ae/controller/__main__.py`, `docs/ops/runbook.md`
- [x] CRI smoke coverage (basic + NodePort) gated.
  - Evidence: `scripts/cri_smoke.sh`, `scripts/cri_nodeport_smoke.sh`, `scripts/multinode_nodeport_smoke.sh`

## P1: Next parity items (close common chart gaps)

- [ ] PVC/PV/StorageClass controller parity (Pending/Bound/Released, default StorageClass).
  - See: `docs/wip/storage-parity.md`
- [ ] NetFS mount lifecycle coverage on CRI nodes (NFS/SMB), PVC->mount reconciliation in apishim.
- [ ] CSI external provisioner hook + VolumeSnapshot/clone parity.
  - See: `docs/wip/csi.md`
- [ ] StatefulSet volumeClaimTemplates per-ordinal mount naming for CRI runtime (if any gaps remain).
- [ ] Storage reclaimPolicy + finalizers for PVC/PV parity.
- [ ] Multi-node service proxy with podCIDR auto-discovery (reduce manual AE_POD_CIDR use).

## P2: Longer-term parity (k8s conformance adjacent)

- [ ] NetworkPolicy enforcement.
- [ ] Node/Lease heartbeats + eviction semantics.
- [ ] metrics.k8s.io.
- [ ] Admission webhooks / Pod Security Admission.
- [ ] Server-side apply managedFields parity.

## P0 verification (2026-01-30)

All P0 items are present in code and documented. Smoke and integration tests are
available but remain gated for CRI-capable nodes. The remaining blockers for a
CRI-default experience are storage parity items (PVC/PV/StorageClass) and CSI
integration tracked in P1.
