# Storage Parity Architecture Plan

Goal: reach a "complete" level where most Kubernetes YAML and Helm charts that use
PVCs and StatefulSets work against k1s with minimal or no chart changes.

This plan treats storage as a first-class compatibility layer across:
- k1s native runtime (Docker/Podman)
- apishim (Kubernetes API shim)
- exporter (K8s YAML output)

It focuses on PVC/PV/StorageClass semantics, StatefulSet volumeClaimTemplates,
existingClaim usage, and the common Helm behaviors around dynamic provisioning.

---

## 1) Current State (Baseline)

k1s runtime supports two storage mechanisms today:
- `spec.volumes` = hostPath bind mounts (runtime only, not exported).
- `spec.storage` = named persistent volumes (PV-lite), pinned to a node and mounted
  into containers; retention Delete is enforced on `ae delete --purge`.

Exporter supports:
- PVC emission from `spec.storage` when `--emit-storage` is set.
- StatefulSet volumeClaimTemplates when `--workload statefulset --emit-storage`.

Apishim currently does not handle PV/PVC/StorageClass resources.

---

## 2) Definition of "Complete" (Compatibility Targets)

We define storage compatibility as follows:

**Works out-of-the-box** for most charts:
- Deployment/StatefulSet using PVCs with a default StorageClass.
- StatefulSets with `volumeClaimTemplates`.
- Charts that reference an `existingClaim` (pre-created PVC).
- Helm charts that depend on standard PVC/PV lifecycle (`Pending` -> `Bound`, delete
  with reclaim policy `Delete` or `Retain`).

**Supported behavior and fields**:
- PVC: `spec.accessModes`, `spec.resources.requests.storage`, `spec.storageClassName`,
  `spec.volumeMode`, `spec.selector.matchLabels`, `spec.volumeName` (bind to PV).
- PV: `spec.capacity.storage`, `spec.accessModes`, `spec.storageClassName`,
  `spec.volumeMode`, `spec.claimRef`, `spec.persistentVolumeReclaimPolicy`,
  `spec.nodeAffinity` (for local volumes).
- StorageClass: `provisioner`, `parameters`, `reclaimPolicy`, `volumeBindingMode`,
  `allowVolumeExpansion`, `mountOptions`.
- StatefulSet: `volumeClaimTemplates` with per-replica PVCs.
- Pod/Deployment volume mounts using `persistentVolumeClaim.claimName`.

**Explicitly out of scope** for the “complete” target:
- Advanced volume modes like `Block` (initially opt-in later).
- Topology-aware scheduling beyond node-local pinning.

---

## 3) Architecture Overview

We add a **Storage Controller** and a **Provisioning layer** that reconcile K8s
PVC/PV/StorageClass objects (apishim) into runtime storage artifacts and enforce
lifecycle semantics.

### Components

1) **Apishim Storage API**
   - Extend apishim to serve and persist:
     - `core/v1` PVC, PV
     - `storage.k8s.io/v1` StorageClass
   - Add watch support and status subresources.
   - Store `status.phase`, `capacity`, `claimRef`, `volumeAttributesClassName` (if present).

2) **Storage Controller** (new module under `src/ae/storage/`)
   - Watches PVC/PV/StorageClass objects in apishim store.
   - Creates/updates PVs for dynamic provisioning.
   - Binds PVCs to PVs and updates status.
   - Emits runtime volume bindings so pods can mount the correct volume.

3) **Provisioners**
   - **local-path** (built-in):
     - Backed by host paths on the node where the pod will run.
     - RWO only by default.
     - Supports `WaitForFirstConsumer` binding (provision on scheduling).
   - **nfs-rwx** (required):
     - Shared RWX backend for charts expecting RWX (e.g., some databases, shared content).
     - Can be a managed NFS server or a local NFS daemon with shared path.
     - Becomes the default StorageClass when RWX is mandatory.

4) **CSI hooks (provisioner interface)**
   - Map StorageClass provisioner names to built-in drivers or external CSI drivers.
   - Provide feature gates and health checks for external provisioners.

5) **Runtime adapters**
   - Extend Docker/Podman runtime to mount volumes by *resolved PVC* instead of
     app-level named volume only.
   - Add a per-replica volume naming mode for StatefulSet semantics.

6) **Adapter mappings (apishim -> k1s App)**
   - Deployments/StatefulSets in apishim are translated to k1s Apps.
   - PVC-backed volumes are represented in an extended k1s manifest format
     or via sidecar metadata in state (see Phase 1 for schema decision).

---

## 4) Data Model Extensions

### New storage tables (state DB)
- `storage_classes`: name, provisioner, parameters, reclaim_policy,
  volume_binding_mode, allow_volume_expansion
- `persistent_volumes`: name, capacity, access_modes, volume_mode,
  storage_class, reclaim_policy, claim_ref, node_affinity, phase
- `persistent_volume_claims`: namespace, name, request_size, access_modes,
  storage_class, volume_name, selector, volume_mode, phase, bound_pv
- `volume_bindings`: app, replica_id, pvc_ref, pv_name, node_id, mount_path

### Runtime volume identity
We need an explicit mapping from PVC -> runtime storage:
- Local-path: host path under a known root, e.g. `/var/lib/k1s/storage/<pv>`
  (configurable via `AE_STORAGE_ROOT`).
- Docker/Podman named volume: `ae-<ns>--<pvc>` or `ae-<app>-<pvc>-<ordinal>`.

---

## 5) Behavior & Reconciliation Flows

### A) PVC created (dynamic provisioning)
1) PVC appears in apishim store.
2) Storage Controller resolves StorageClass (default if none specified).
3) If `volumeBindingMode=Immediate`, provision a PV immediately.
4) If `WaitForFirstConsumer`, defer until a Pod/Deployment/StatefulSet is scheduled.
5) Update PVC status: `Pending` -> `Bound`.

### B) Pod scheduled to node (WaitForFirstConsumer)
1) Scheduler chooses a node (respecting nodeSelector/taints + storage pinning).
2) Storage Controller provisions a local PV bound to that node.
3) PV includes `nodeAffinity` to the chosen node.
4) Pod creation proceeds with PVC mounted.

### C) StatefulSet volumeClaimTemplates
1) For each replica ordinal, create per-replica PVC objects:
   `claimName = <templateName>-<statefulsetName>-<ordinal>`.
2) Bind each PVC to a PV and map to per-replica runtime volume name.
3) Ensure that the runtime container for that replica mounts its own volume
   (not a shared volume).

### D) existingClaim
1) If a PVC already exists, bind directly and mount into pods.
2) No dynamic provisioning unless the PVC is Pending with a StorageClass.

### E) Deletion & reclaim policy
- `Delete` => remove runtime volume + PV, set PVC to `Lost` if needed.
- `Retain` => keep PV + volume; PVC deletion leaves PV `Released`.

---

## 6) Runtime Mount Strategy

We introduce a new storage mount resolution path that is PVC-aware:

1) **PVC-backed mounts** (apishim-managed):
   - For each container mount with `persistentVolumeClaim.claimName`, resolve
     the bound PV + runtime volume path.
   - Mount the resolved host path or named volume at the requested mountPath.

2) **k1s native storage** (`spec.storage`):
   - Continue to support named volumes for k1s-native manifests.
   - Optionally map `spec.storage` to a pseudo-PVC for parity when exporting.

3) **HostPath mounts** (`spec.volumes`):
   - Still supported for native runtime, not part of portable storage.

### Per-replica storage support
To support StatefulSet semantics:
- Extend runtime volume naming to include `replica_id` or `ordinal` when a
  `storage.perReplica=true` flag is set.
- Example naming: `ae-<app>-<claim>-r<ordinal>`.

---

## 7) StorageClass Support

Provide built-in StorageClasses with Kubernetes-aligned defaults:
- `k1s-nfs` (default, RWX mandatory)
  - provisioner: `k1s.io/nfs`
  - volumeBindingMode: `Immediate`
  - reclaimPolicy: `Retain` (safer default for shared storage)
  - allowVolumeExpansion: true
- `k1s-local`
  - provisioner: `k1s.io/local-path`
  - volumeBindingMode: `WaitForFirstConsumer`
  - reclaimPolicy: `Delete`
  - allowVolumeExpansion: false (initial), true in Phase 5

K8s-aligned conventions:
- Default StorageClass annotation: `storageclass.kubernetes.io/is-default-class: "true"`.
- PV naming for dynamic provisioning: `pvc-<uid>` (matching kube-controller-manager).
- Standard annotations:
  - `pv.kubernetes.io/bound-by-controller: "yes"`
  - `pv.kubernetes.io/provisioned-by: <provisioner>`
  - `volume.kubernetes.io/storage-provisioner: <provisioner>`
- PVC/PV phase transitions mirror Kubernetes (`Pending` -> `Bound`, PV `Available` -> `Bound` -> `Released`).
- `volumeBindingMode` defaults: `WaitForFirstConsumer` for local storage, `Immediate` for RWX.

K8s conventions checklist (implementation):
- PVC/PV finalizers: `kubernetes.io/pvc-protection`, `kubernetes.io/pv-protection`.
- `claimRef` includes `namespace`, `name`, and `uid` when bound.
- PVC `status.capacity` and `status.accessModes` are populated on bind.
- PV `status.capacity`, `status.accessModes`, and `status.phase` are updated.
- StatefulSet PVC names follow `<claimTemplateName>-<statefulsetName>-<ordinal>`.
- Local PVs carry `nodeAffinity` to the chosen node (for WaitForFirstConsumer).

### CSI Hook Interface (Initial)

The CSI hooks provide a Kubernetes-aligned integration point so external CSI
drivers can be used without changing Helm charts.

**Provisioner registry configuration** (default path, override with `AE_STORAGE_PROVISIONERS`):
- `configs/storage-provisioners.yaml` lists built-in and external drivers and the
  StorageClass defaults they map to.

Example:
```yaml
provisioners:
  - name: k1s-local
    provisioner: k1s.io/local-path
    type: builtin
    accessModes: [ReadWriteOnce]
    volumeBindingMode: WaitForFirstConsumer
    reclaimPolicy: Delete
  - name: k1s-nfs
    provisioner: k1s.io/nfs
    type: builtin
    accessModes: [ReadWriteMany]
    volumeBindingMode: Immediate
    reclaimPolicy: Retain
  - name: csi-fast
    provisioner: csi.example.com
    type: csi
    endpoint: unix:///run/csi.sock
    accessModes: [ReadWriteOnce, ReadWriteMany]
    volumeBindingMode: Immediate
    reclaimPolicy: Delete
```

**Hook contract (exec or gRPC bridge)**:
- CreateVolume: input PVC + StorageClass + optional `selectedNode`, output PV spec
  (including `capacity`, `accessModes`, `volumeMode`, `nodeAffinity` as needed).
- DeleteVolume: input PV name + parameters, perform cleanup and return success.
- ExpandVolume: input PV name + new size, update PV capacity and return success.
- GetCapabilities: return supported access modes, volume modes, expansion.

**Objects surfaced in apishim** (when CSI is enabled):
- `storage.k8s.io/v1` `CSIDriver`, `CSINode`, `CSIStorageCapacity`
  (minimum set for Helm and CSI-sidecar compatibility).

**CSI-aligned annotations/conditions**:
- Populate `pv.kubernetes.io/provisioned-by` and `volume.kubernetes.io/storage-provisioner`
  using the StorageClass provisioner string for compatibility with external-provisioner.

---

## 8) Compatibility Matrix (Target)

| Feature | Native k1s | apishim storage | Exporter |
|--------|------------|----------------|----------|
| PVC + Deployment | Yes (via shim) | Yes | Yes |
| PVC + StatefulSet | Yes (per-replica) | Yes | Yes |
| existingClaim | Yes | Yes | Yes |
| StorageClass default | Yes | Yes | Yes |
| accessModes RWO | Yes | Yes | Yes |
| accessModes RWX | Required (NFS/CSI) | Yes | Yes |
| volumeClaimTemplates | Yes | Yes | Yes |
| reclaimPolicy Delete/Retain | Yes | Yes | Yes |

---

## 9) Implementation Phases

### Phase 0: Schema + Storage Objects (apishim)
- Add CRUD + watch for PVC, PV, StorageClass in apishim.
- Update OpenAPI and discovery lists.
- Add migration for apishim store to include new resources.

### Phase 1: Storage Controller (dynamic provisioning)
- Create `src/ae/storage/controller.py`.
- Implement local-path provisioner.
- Bind PVCs to PVs, update statuses.
- Store bindings in state DB.

### Phase 2: CSI Hooks + Provisioning Interface (required)
- Add StorageClass/CSI capability objects in apishim (CSIDriver, CSINode, CSIStorageCapacity as needed).
- Add a provisioner interface that can map StorageClass provisioner names to:
  - built-in `k1s.io/local-path`
  - built-in `k1s.io/nfs`
  - external CSI drivers via hooks (exec or gRPC sidecar), with feature gating.
- Implement CSI-style annotations and conditions on PV/PVC for Helm compatibility.

### Phase 3: Runtime mount resolution
- Extend runtime adapter to resolve PVC-backed mounts (mapping to host path or
  named volume).
- Add per-replica volume support for StatefulSet semantics.
- Ensure initContainers also mount PVC volumes.

### Phase 4: StatefulSet + volumeClaimTemplates
- Implement per-replica PVC generation in apishim adapter.
- Ensure proper binding and scheduling.

### Phase 5: Expansion + volumeMode
- Support PVC expansion for local-path (grow directory quota if supported).
- Respect `volumeMode: Block` as opt-in (skip in default).

---

## 10) Backwards Compatibility

- Existing `spec.storage` remains functional for native k1s manifests.
- apishim PVC support is additive and does not break existing apps.
- Exporter stays unchanged except for optional additional flags to emit hostPath
  volumes in a non-portable mode.

---

## 11) Tests & Validation

- Unit tests:
  - PVC -> PV binding logic (Immediate + WaitForFirstConsumer).
  - Per-replica volume naming and mounts.
  - Reclaim policy handling.
- Integration tests:
  - Helm chart smoke tests for common workloads (Postgres, Redis, Prometheus).
  - StatefulSet with volumeClaimTemplates + scale up/down.
  - existingClaim install path.

---

## 12) Configuration Knobs

- `AE_STORAGE_ROOT`: root path for local-path provisioner (default: `/var/lib/k1s/storage`).
- `AE_STORAGE_DEFAULT_CLASS`: name of default StorageClass (default: `k1s-nfs`).
- `AE_STORAGE_NFS_SERVER` / `AE_STORAGE_NFS_PATH`: RWX backend.
- `AE_STORAGE_VOLUME_BINDING_MODE`: default binding mode (Immediate / WaitForFirstConsumer).

---

## 13) Risks & Mitigations

- **Data loss with Delete reclaim**: make Delete explicit, default to Retain for
  user-created StorageClass unless overridden.
- **Multi-node locality**: enforce node affinity and scheduler pinning on local volumes.
- **Chart expectations**: ensure status conditions and PVC phases are updated so
  Helm `--wait` converges.

---

## 14) Deliverables (MVP for "Complete")

- PVC/PV/StorageClass support in apishim (CRUD + watch).
- RWX-capable default StorageClass (`k1s-nfs`) plus local-path class.
- CSI hooks (provisioner interface + core CSI objects) so external drivers can plug in.
- Local-path + NFS provisioners with dynamic PV creation.
- PVC-aware runtime mounts with per-replica volumes for StatefulSet.
- Basic Helm chart compatibility for stateful charts using PVCs.
