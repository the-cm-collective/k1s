# Network storage (NetFS) plan for k1s

This doc covers:
- A NetFS storage skeleton and how to wire it into runtime and apishim flows.
- A field map from AppManifest or PVC/PV to network-backed mounts.
- Storage auth, node prerequisites, and rollout phases for network storage.
- A CSI-aligned backend driver lifecycle so we can reuse existing drivers.

It is intentionally minimal and matches the current code structure.

---

## 1) NetFS storage skeleton + wiring

### 1.1 Driver lifecycle (CSI-aligned)

We already have the PV/PVC contract. The missing piece is a backend driver lifecycle
that is consistent across nodes and aligns with Kubernetes CSI semantics.

Lifecycle stages:
- Provision (CreateVolume)
- Attach/Prepare (ControllerPublish for block; optional for shared filesystems)
- Mount/Publish (NodeStage / NodePublish)
- Health/Stats (Volume health + usage)
- Expand/Snapshot/Clone (optional features)
- Delete (DeleteVolume + unpublish)

Design goal: implement CSI semantics (even partially) so we can reuse existing
storage drivers (Ceph, NFS, iSCSI, cloud disks) instead of inventing new ones.

### 1.2 New module: `src/ae/storage/netfs.py`

Goal: manage network-backed PVs and mount them on nodes.

High-level responsibilities:
- Resolve PVC -> PV -> volume source (NFS/CSI/SMB).
- Provision or attach network volumes based on StorageClass and driver.
- Mount volumes on the node and expose a host path for runtime bind mounts.
- Track mount lifecycle with reclaimPolicy semantics.

Suggested skeleton (pseudocode; not complete):

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ae.storage.types import PvcRef, PvRef
from ae.storage.state import StorageState

@dataclass
class NetFSMount:
    pvc: PvcRef
    pv: PvRef
    node_id: str
    host_path: str
    read_only: bool

class StorageDriver(Protocol):
    name: str

    # Controller-plane
    def create_volume(self, pvc: PvcRef) -> PvRef: ...
    def delete_volume(self, pv: PvRef) -> None: ...
    def controller_publish(self, pv: PvRef, node_id: str) -> None: ...
    def controller_unpublish(self, pv: PvRef, node_id: str) -> None: ...

    # Node-plane
    def node_stage(self, pv: PvRef, target_path: str) -> None: ...
    def node_publish(self, pv: PvRef, target_path: str, *, read_only: bool) -> None: ...
    def node_unpublish(self, pv: PvRef, target_path: str) -> None: ...
    def node_unstage(self, pv: PvRef, target_path: str) -> None: ...

class NetFSManager:
    def __init__(self, state: StorageState, *, root: str | None = None) -> None:
        self._state = state
        self._root = root or "/var/lib/ae/netfs"

    def ensure_mount(self, pvc: PvcRef, *, node_id: str) -> NetFSMount:
        """Ensure PV is attached (if needed) and mounted on the node."""
        raise NotImplementedError

    def release_mount(self, pvc: PvcRef, *, node_id: str) -> None:
        """Unmount and detach if the PV is no longer referenced."""
        raise NotImplementedError

    def list_mounts(self, *, node_id: str | None = None) -> list[NetFSMount]:
        return []
```

### 1.3 Wiring into node/runtime

NetFS integrates at two points:
- **Storage controller**: resolves PVC/PV/StorageClass and produces mount metadata.
- **Node volume manager**: ensures mounts are present on the node and reconciles
  attach/detach/mount state.

Suggested wiring:
- Add `src/ae/storage/controller.py` (if not already created):
  - Watches PVC/PV/StorageClass objects in apishim state.
  - Binds PVCs to PVs and updates status.
  - Creates VolumeAttachment-like records for attachable volumes.
- Add a node-level volume manager:
  - Reconciles desired mounts from Pod -> PVC relationships.
  - Performs idempotent ensure_mount / release_mount.
  - Writes authoritative state so scheduler can avoid split-brain.
- Update runtime adapters (Docker/Podman/CRI):
  - For PVC-backed volumes, bind-mount `host_path` from NetFS.
  - Preserve `spec.storage` (named volume) and `spec.volumes` (hostPath) behavior.

Suggested config knobs:
- `AE_NETFS_ROOT=/var/lib/ae/netfs` (mount root)
- `AE_STORAGE_PROVISIONERS=/etc/ae/storage-provisioners.yaml`
- `AE_STORAGE_DEFAULT_CLASS=k1s-nfs`

---

## 2) AppManifest / PVC/PV -> NetFS mapping

This maps k1s-native manifest fields and Kubernetes PVC/PV fields to network
storage behavior.

### 2.1 AppManifest (k1s-native) -> PVC/PV

Extend `spec.storage` to optionally describe network-backed PVCs:
- `name` -> PVC `metadata.name`
- `mountPath` -> `volumeMount.mountPath`
- `size` -> PVC `spec.resources.requests.storage` (K8s quantity string)
- `retention` -> PV `reclaimPolicy` (`Retain` or `Delete`)
- `class` (new) -> PVC `spec.storageClassName`
- `accessModes` (new) -> PVC `spec.accessModes` (RWO/RWX/ROX/RWOP)
- `volumeMode` (new) -> PVC `spec.volumeMode` (Filesystem initially)
- `readOnly` (new) -> PV `spec.nfs.readOnly` or CSI `readOnly`

Defaults:
- `storageClassName`: use default StorageClass (typically `k1s-nfs` for RWX).
- `accessModes`: `ReadWriteOnce` for local-path, `ReadWriteMany` for NetFS.
- `volumeMode`: `Filesystem` (Block is a later phase).

### 2.2 PVC/PV -> runtime mount

For apishim-backed workloads, the Pod volume fields must align with K8s:
- `volume.persistentVolumeClaim.claimName` is the lookup key.
- PVC must be `Bound` and point to a PV (`spec.volumeName`).
- The PV volume source determines the NetFS mount:
  - **NFS**: `pv.spec.nfs.server`, `pv.spec.nfs.path`, `pv.spec.nfs.readOnly`
  - **CSI**: `pv.spec.csi.driver`, `volumeHandle`, `fsType`, `volumeAttributes`
  - **SMB**: `pv.spec.csi.driver` or a native SMB volume type (avoid flexVolume)

The runtime adapter mounts the node host path returned by NetFS at the Pod
`volumeMount.mountPath`. `subPath` and `mountPropagation` must follow K8s rules.

### 2.3 StorageClass alignment

StorageClass fields that must match Kubernetes semantics:
- `provisioner` (driver selection)
- `parameters` (driver-specific, opaque map)
- `reclaimPolicy` (`Delete` or `Retain`)
- `volumeBindingMode` (`Immediate` or `WaitForFirstConsumer`)
- `allowVolumeExpansion` (bool)
- `mountOptions` (string list)

---

## 3) Storage auth, credentials, and node prerequisites

### 3.1 Auth and secrets

Network storage credentials follow Kubernetes conventions:
- **NFS**: usually no per-volume auth; rely on network ACLs.
- **SMB/CIFS**: use `Secret` with `username`/`password`, referenced by PV or CSI.
- **CSI drivers**: use `nodeStageSecretRef` / `nodePublishSecretRef` (PV.spec.csi).

k1s handling:
- Resolve secrets via apishim `Secret` objects or SOPS-backed local files.
- Normalize secret keys to match CSI and driver docs (do not rename fields).
- Only pass secrets to the driver on the node that mounts the volume.

### 3.2 Node prerequisites

Required utilities by backend:
- NFS: `nfs-common`, `rpcbind`, `mount`
- SMB: `cifs-utils`
- CSI: driver-specific binaries + sidecars (external to k1s)

Recommended node checks (preflight):
- `showmount -e <server>` for NFS servers
- `findmnt` to verify mounted paths
- `mount` exit codes mapped to PVC events in controller logs

---

## Implementation checklist (summary)

- Add NetFS manager and wire into runtime volume resolution.
- Add a node volume manager reconciler for idempotent mount lifecycle.
- Extend storage controller to reconcile PVC/PV/StorageClass and driver actions.
- Add NFS-backed StorageClass and PV creation flow for RWX.
- Plumb storageClass/accessModes/size into k1s-native `spec.storage`.
- Implement mount lifecycle and reclaimPolicy handling.
- Add basic tests for PVC -> mount mapping and mount/unmount idempotency.

---

## Backend compatibility matrix (current vs NetFS)

Legend:
- OK: works today
- Needs work: new NetFS support required

| Subsystem | Docker | Podman | CRI | NetFS (PVC/PV) | Notes |
| --- | --- | --- | --- | --- | --- |
| spec.storage (named volumes) | OK | OK | Needs work | N/A | Local-only today |
| PVC/PV resolution (apishim) | Needs work | Needs work | Needs work | Needs work | Storage controller required |
| RWX storage | Needs work | Needs work | Needs work | Needs work | NFS/CSI required |
| Volume lifecycle (reclaimPolicy) | Needs work | Needs work | Needs work | Needs work | Align PV phases |
| Mount options / subPath | OK | OK | Needs work | Needs work | Align with K8s volumeMount rules |

Summary:
- NetFS is runtime-agnostic once the node host path is mounted.
- The design impact is on **storage controller + mount resolution**, not container runtime.

---

## Phased rollout plan (safe adoption)

### Phase 0 -- Design + scaffolding (1-2 days)

- Define NetFS interfaces (manager, provisioner, mount resolver).
- Add config schema for StorageClass and provisioner registry.
- Document NFS and CSI prerequisites and node preflight checks.

Exit criteria:
- NetFS module exists with stubs and configuration hooks.
- Storage controller skeleton is wired into apishim storage objects.

### Phase 1 -- RWX network filesystem (mount-only, no attach) (3-7 days)

- Implement NFS-backed PV support (static PVs only).
- Add `k1s-nfs` StorageClass (RWX, Immediate binding).
- Implement node mount flow for NFS PVs.

Exit criteria:
- PVC bound to an NFS PV can be mounted on a node and used by a Pod.
- `ReadWriteMany` semantics are supported for multiple replicas on the same node.

### Phase 2 -- Dynamic provisioning (5-10 days)

- Add built-in NFS provisioner (Create/Delete volume).
- Support `WaitForFirstConsumer` for local-path storage classes.
- Implement reclaimPolicy Delete/Retain semantics on PV delete.

Exit criteria:
- PVCs without `volumeName` are dynamically provisioned.
- PVC/PV phases follow Kubernetes transitions.

### Phase 3 -- Block volumes (attach + mount) (7-14 days)

- Add attach/detach workflow for CSI-backed block volumes.
- Enforce single-writer semantics for RWO volumes.
- Add VolumeAttachment objects and node publish semantics.

Exit criteria:
- PVCs bound to block PVs attach to a node and mount successfully.
- RWO volumes cannot be attached to two nodes simultaneously.

### Phase 4 -- Nice-to-haves (future) (7-14 days)

- Snapshots/clones, online expansion, volume health reporting.
- RWOP support and topology-aware provisioning.

Exit criteria:
- Snapshot and resize flows available for supported drivers.

---

## Rollout strategy (mixed local + NetFS)

- Keep `spec.storage` (named volumes) as default until Phase 2 completes.
- Enable NetFS only on nodes labeled `storage=netfs` (or equivalent).
- Gate StorageClass defaults per environment (local vs NFS).
- Document known limitations (no Block mode, snapshots are hostPath-backed only).

---

## Concrete task breakdown

### Phase 0 -- Design + scaffolding
- Add `src/ae/storage/netfs.py` (interfaces + stubs)
  - Align naming with Kubernetes objects: `PersistentVolume`, `PersistentVolumeClaim`, `StorageClass`.
  - Use K8s field names (`accessModes`, `volumeMode`, `reclaimPolicy`) in data models.
- Add storage config schema and defaults
  - Default StorageClass annotation: `storageclass.kubernetes.io/is-default-class: "true"`.
  - Match K8s `provisioner` string format (e.g., `k1s.io/nfs`).
- Add storage controller skeleton
  - Track PVC/PV `status.phase` (`Pending` -> `Bound`), match K8s phases exactly.
  - Populate `claimRef` with `namespace`, `name`, `uid` on bind.

### Phase 1 -- RWX network filesystem (mount-only)
- Implement PV source mapping for NFS
  - Use `pv.spec.nfs.server`, `pv.spec.nfs.path`, `pv.spec.nfs.readOnly` fields verbatim.
  - Enforce `accessModes` includes `ReadWriteMany` for RWX workloads.
- Implement node mount flow
  - Use `mountOptions` from StorageClass and PV (`spec.mountOptions`).
  - Match K8s `volumeMode=Filesystem` semantics (no block devices yet).
- Add NFS StorageClass default
  - `provisioner: k1s.io/nfs`, `volumeBindingMode: Immediate`, `reclaimPolicy: Retain`.
  - Set default class annotation per K8s conventions.

### Phase 2 -- Dynamic provisioning
- Add built-in NFS provisioner
  - PV name: `pvc-<uid>` (kube-controller-manager convention).
  - Annotations: `pv.kubernetes.io/provisioned-by`, `volume.kubernetes.io/storage-provisioner`.
- Implement `WaitForFirstConsumer` for local-path
  - Bind PV only after node is selected; set `pv.spec.nodeAffinity` accordingly.
  - PVC stays `Pending` until a Pod references it (K8s behavior).
- Enforce reclaimPolicy behavior
  - `Delete` removes PV + backing path, `Retain` leaves PV in `Released`.
  - PVC deletion must not delete PV when `Retain` is set.

### Phase 3 -- Block volumes (attach + mount)
- Add CSI attachment/mount workflow
  - Create `VolumeAttachment` objects (storage.k8s.io/v1) for attachable volumes.
  - Use `pv.spec.csi.driver` and `volumeHandle` fields as the driver contract.
- Implement secret handling for CSI
  - Honor `nodeStageSecretRef` and `nodePublishSecretRef` namespaces.
  - Align secret data keys to CSI driver docs (do not rename fields).
- Expose CSI API objects for sidecars
  - Serve `CSIDriver`, `CSINode`, and `CSIStorageCapacity` resources via apishim.
- Enforce single-writer semantics
  - Prevent multiple node attachments for RWO volumes.
  - Treat multi-attach as an error event aligned with K8s behavior.

### Phase 4 -- Nice-to-haves
- Volume expansion (basic controller-side support implemented)
  - When `allowVolumeExpansion=true`, larger PVC requests update PV `spec.capacity`
    and PVC `status.capacity`, and emit a `VolumeExpanded` event.
  - Filesystem resize inside containers is best-effort on the node when
    `AE_NETFS_FS_RESIZE=1` (ext4/xfs only); NFS/local-path mounts are a no-op.
- Snapshot / clone (basic hostPath-backed snapshots implemented)
  - `VolumeSnapshot` reconciliation creates `VolumeSnapshotContent` and copies
    hostPath data into `.snapshots/<snapshot-uid>` under the storage root.
  - Snapshot classes follow default-class semantics via
    `snapshot.storage.kubernetes.io/is-default-class: "true"`.
  - PVC `dataSource` `VolumeSnapshot` restores snapshot data into new hostPath
    volumes for NFS and local-path provisioners.
  - CSI snapshot contents are created with `source.volumeHandle` and snapshot
    readiness reflects CSI-populated `VolumeSnapshotContent.status` when present.
    CSI restore/provisioning still requires external snapshotter/provisioner.
- RWOP + topology
  - Add `ReadWriteOncePod` access mode support (K8s 1.22+).
  - Respect `topologyKeys` and `allowedTopologies` where provided.

---

## Recommended baseline backends

- **NFS**: fastest path to RWX network storage and a good baseline default.
- **CSI compatibility layer**: unlocks external drivers (Ceph, Longhorn, OpenEBS).
- **Reference backend (choose one)**:
  - **Ceph + ceph-csi** for block (RBD) and file (CephFS).
  - **Longhorn** for simpler RWO replicated block.
  - **OpenEBS** for multiple data engines and flexible deployment.

Recommendation for a "blessed" path:
- Ship NFS RWX first (static, then dynamic).
- Add CSI compatibility layer next.
- Pick one distributed backend (Ceph or Longhorn) based on your target profile.

---

## Kubernetes storage glossary

Use this as the source of truth for naming and alignment in code and docs.

- **PersistentVolume (PV)**: cluster-scoped volume resource.
- **PersistentVolumeClaim (PVC)**: namespace-scoped request for a PV.
- **StorageClass**: describes a provisioner and defaults for PV creation.
- **VolumeAttachment**: CSI attach/detach object (storage.k8s.io/v1).
- **Access modes**: RWO, RWX, ROX, RWOP.
- **VolumeBindingMode**: Immediate or WaitForFirstConsumer.
- **ReclaimPolicy**: Delete or Retain.
- **VolumeMode**: Filesystem or Block.
- **Mount options**: `mountOptions` list on StorageClass or PV.

Alignment checklist:
- PVC/PV phases must follow K8s (`Pending` -> `Bound`, PV `Available` -> `Bound` -> `Released`).
- PV `claimRef` must include `namespace`, `name`, and `uid`.
- PV naming for dynamic provisioning should be `pvc-<uid>`.
- Default StorageClass annotation must be set on only one class.
- VolumeMount `subPath` and `mountPropagation` must match K8s semantics.

---

## NetFS utilities and dependencies (project + node)

### Project Python dependencies

Add to `requirements.in` (or a storage extras group):
- `pyyaml` (if config parsing is not already in use)
- `types-PyYAML` (optional, mypy type hints)

### Repo scripts (recommended)

- `scripts/netfs_preflight.sh`
  - Check NFS/SMB utilities and validate mount targets.
- `scripts/netfs_smoke.sh`
  - Create a PVC/PV pair and mount it on the node, then clean up.
- `scripts/netfs_snapshot_clone.sh`
  - Create a snapshot and clone PVC from an NFS-backed volume and verify data.
- `scripts/netfs_csi_smoke.sh`
  - Validate CSI PV/PVC binding, VolumeAttachment creation, and CSI marker output.
  - Set `NETFS_MULTIATTACH=1` to verify multi-attach is blocked.

### Node/system utilities (document in runbook)

Required (NFS):
- `nfs-common`, `rpcbind`, `mount`, `findmnt`

Optional (SMB / CSI):
- `cifs-utils`
- `open-iscsi` (iSCSI-based CSI drivers)
- `ceph-common` (CephFS/RBD drivers)

---

## Future NetFS features to consider (post-MVP)

- Block volume mode and raw device mapping.
- VolumeSnapshots and VolumeSnapshotClass support.
- Volume cloning via `dataSourceRef`.
- Volume expansion with filesystem resize in containers.
- Topology-aware provisioning and capacity tracking (`CSIStorageCapacity`).
- Volume health checks and metrics aligned with K8s events.
- SELinux and fsGroup policies for shared volumes.
- Per-namespace quota enforcement and usage tracking.
