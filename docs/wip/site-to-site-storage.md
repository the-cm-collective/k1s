# Site-to-Site Storage Mounts (NetFS + CSI Options)

This document summarizes current cross-site storage behavior when a WireGuard-based
site-to-site overlay is in use, then outlines next CSI options with pros/cons and
implementation scope.

## Current Capabilities

### What works today (NetFS)
- NFS-backed PVs mount on the node agent and are bind-mounted into pods.
- Dynamic NFS provisioning works when the controller has `AE_STORAGE_NFS_SERVER` and
  `AE_STORAGE_NFS_PATH` set and seeds the `k1s-nfs` StorageClass.
- The node agent resolves PVCs using the apishim store when `AE_ENABLE_NETFS=1`.
- Mounts land under `AE_NETFS_ROOT` (default `/var/lib/ae/netfs/<ns>/<pvc>`).
- Mount options are honored from the StorageClass and PV `mountOptions`.
- RWX is supported for NFS; block volumes are not supported for the NFS provisioner.

### Overlay impact
- The WG overlay is transport only. Storage traffic flows directly between the
  edge node and the NFS server address (often the hub WG IP).
- The controller does not proxy storage traffic.

### What is not cross-site
- Local-path storage (`k1s.io/local-path`) is node-local and not suitable for
  site-to-site use.

## Operational Pattern (Core Hub + SEA Edge)

- Hub controller seeds StorageClass and PV/PVC objects.
- SEA node runs the node agent with `AE_ENABLE_NETFS=1` and access to the apishim
  store (`AE_APISHIM_DSN` strongly preferred for remote nodes).
- SEA node mounts `server:path` using NFS and binds it into the container.
- NFS server address should be the hub WG IP so the SEA node routes over the overlay.
- The SEA node must have `mount`/`umount` plus an NFS helper (`mount.nfs` or
  `mount.nfs4`) and sufficient privileges.

## Current Gaps and Constraints

- CSI is marker-only. PVs with `spec.csi` are validated and recorded, but there is
  no CSI gRPC staging/publish in the node agent.
- VolumeAttachment is enforced for CSI PVs, but attach/publish semantics are not
  executed by k1s.
- Remote nodes require apishim store access; local sqlite (`state/apishim.db`) is
  not viable across sites.
- NFS provisioning with `AE_STORAGE_NFS_HOSTPATH` only works when the controller
  host is also the NFS server host.
- Storage conformance is partial and still evolving (PVC/PV/StorageClass parity
  is a workstream).

## CSI Options for Initial Implementation

### Option 1: Exec-based CSI hooks (Phase 1 in `docs/wip/csi.md`)
- Scope: Add hook env vars for create/delete/publish/stage and pass PV/PVC/secret
  context via JSON.
- Capabilities: Enables real mounts for a chosen CSI driver without full gRPC.
- Pros: Fast to implement, driver-agnostic, minimal architecture changes.
- Cons: Non-standard, bespoke wiring per driver, harder to support sidecars.

### Option 2: Native CSI gRPC services (Phase 2)
- Scope: Implement CSI Identity, Controller, and Node services inside k1s.
- Capabilities: Standard CSI semantics, direct integration with CSI drivers.
- Pros: Aligns with upstream CSI spec; unlocks broader driver compatibility.
- Cons: Larger engineering effort; must manage idempotency, retries, and state.

### Option 3: CSI sidecar parity (Phase 3)
- Scope: Make apishim + controller fully compatible with external provisioner,
  attacher, snapshotter, and resizer sidecars.
- Capabilities: End-to-end CSI workflows with standard Kubernetes sidecars.
- Pros: Best compatibility with vendor drivers and tooling.
- Cons: Requires solid CSI gRPC foundation and robust storage object semantics.

## Recommended Path (Near-Term)

- Continue using NFS-backed NetFS for cross-site RWX storage.
- Add exec-based CSI hooks for a reference driver (one driver, one topology) to
  validate the mount lifecycle end-to-end.
- Use the hook-based path to validate secrets, attachments, and publish semantics
  before committing to full CSI gRPC.

## Open Questions

- Which driver should be the reference CSI target (CephFS, RBD, EBS, etc.)?
- Do we need RWO block volumes first, or is RWX file storage the priority?
- Where should CSI controller and node components run (hub only, edge only, both)?
- What topology or failure domains must be enforced for the first release?
