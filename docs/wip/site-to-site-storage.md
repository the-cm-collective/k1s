# Site-to-Site Storage (Chosen Path: CSI gRPC Client)

This document reframes site-to-site storage around the selected entry point:
Option 2 (native CSI gRPC client integration). It documents scope, architecture,
and the phased implementation steps required to land cross-site CSI storage.
Alternate paths are preserved in the legacy section at the end.

## Goals

- Provide cross-site RWX storage using a standard CSI driver.
- Keep k1s aligned with Kubernetes CSI semantics (PVC/PV, VolumeAttachment, events).
- Enable hub-and-spoke deployment where controller runs on the hub and node agents
  run on edge sites over the WireGuard overlay.

## Current State (Baseline)

- NetFS + NFS works today for cross-site RWX.
- CSI PVs support gRPC staging/publish when endpoints are configured.
- VolumeAttachment objects are tied to controller publish/unpublish semantics.
- Remote nodes must use the apishim store (SQLite is not suitable across sites).

## Decisions (Entry Point)

- Entry point: Option 2 (native CSI gRPC client integration).
- Reference driver: CephFS (RWX filesystem storage).
- Volume priority: RWX filesystem first.
- Placement: CSI controller calls from hub, CSI node calls from each edge node.
- Topology: site-aware placement using `topologyKeys: ["site"]`.

## Target Architecture

- Controller (hub):
  - Provisions CSI volumes via CreateVolume/DeleteVolume.
  - Performs ControllerPublish/ControllerUnpublish when attachRequired=true.
  - Writes VolumeAttachment objects and attachment status.
- Node agent (edge):
  - Performs NodeStage/NodePublish/NodeUnpublish/NodeUnstage.
  - Writes CSI marker payload for traceability.
- Configuration:
  - Storage provisioner registry defines controller and node endpoints.
  - StorageClass holds CSI parameters, secrets, mount options, topology.

## Implementation Phases (Step by Step)

### Status Overview

| Phase | Scope | Status |
| --- | --- | --- |
| Phase 0 | CSI semantic fixes | Done |
| Phase 1 | Controller gRPC client | Done |
| Phase 2 | Node gRPC client | Done |
| Phase 3 | Site-to-site operationalization | In progress |
| Phase 4 | Sidecar parity | Future |

### Phase 0 — CSI semantic fixes (completed or near-term)

1. Respect `CSIDriver.spec.attachRequired`:
   - If false, do not create VolumeAttachment and do not block mounts.
   - If true, enforce VolumeAttachment gating.
2. Align read-only semantics:
   - Honor `pv.spec.csi.readOnly` for filesystem and block.
3. Extend CSI marker payload:
   - Add fsType, readOnly, volumeAttributes, publishContext.
4. Improve eventing:
   - Emit explicit events for missing attachments, missing secrets, missing endpoints.
5. Add tests:
   - attachRequired=false path.
   - CSI block PV with devicePath.

Exit criteria:
- CSI PVs with attachRequired=false mount without a VolumeAttachment.
- Marker payload contains the extended fields.
- Unit tests cover attachRequired toggles and CSI block PV behavior.

### Phase 1 — Controller gRPC client integration

1. Add CSI controller gRPC client wrapper with timeouts and error mapping.
2. Add provisioner registry file format:
   - Per-entry controller and node endpoints.
   - Associate entries with StorageClass/provisioner names.
3. Implement dynamic provisioning:
   - CreateVolume using StorageClass parameters and secret refs.
   - DeleteVolume for reclaimPolicy=Delete paths.
4. Implement ControllerPublish/ControllerUnpublish:
   - Use VolumeAttachment as the canonical state object.
   - Write attached=true/false plus attachError details.
5. Add reconciliation rules:
   - Multi-attach guardrails for RWO.
   - Cleanup attachments on PV delete.
6. Add events and metrics:
   - AttachFailed, CsiEndpointMissing, CsiGrpcUnavailable.
7. Add unit tests:
   - Provisioned CSI PVs.
   - VolumeAttachment create/update and attachError behavior.

Exit criteria:
- Controller can provision and delete CSI volumes for the reference driver.
- VolumeAttachment status reflects controller publish results.
- Attach/detach events and metrics are visible and actionable.

### Phase 2 — Node gRPC client integration

1. Add CSI node gRPC client wrapper with timeouts and error mapping.
2. Implement NodeStage/NodePublish/NodeUnpublish/NodeUnstage:
   - Use staging paths under `AE_CSI_STAGE_ROOT`.
   - Honor mount options and readOnly flag.
3. Read publishContext from VolumeAttachment and include in Node calls.
4. Write CSI marker payload on successful publish.
5. Add error handling:
   - NodeStage unimplemented fallback (skip staging if UNIMPLEMENTED).
   - Clear events for failures (NodeStageFailed/NodePublishFailed).
6. Add unit tests:
   - Stage/publish flow with a fake node client.
   - attachRequired=false path bypassing attachment gating.

Exit criteria:
- A CSI volume mounts on edge nodes using NodeStage/NodePublish.
- Marker payload is written and contains publishContext.
- Node failures produce clear, K8s-aligned events.

### Phase 3 — Site-to-site operationalization

1. Topology and placement:
   - Define `topologyKeys: ["site"]` in the StorageClass.
   - Configure nodes with site labels used by the CSI driver.
2. Reference deployment:
   - Document CephFS CSI deployment expectations (controller on hub, nodes on edge).
3. Overlay validation:
   - Ensure CSI endpoints are reachable over WG overlay.
4. Runbook updates:
   - Add operational checks to `docs/ops/runbook.md`.
   - Include recovery steps for attachment or publish failures.
5. End-to-end smoke tests:
   - PVC -> PV -> VolumeAttachment -> NodePublish -> mounted path.

Exit criteria:
- Cross-site RWX mount works with CephFS over the overlay.
- Runbook covers common failure modes.
- A documented, repeatable smoke test exists.

#### Phase 3 Step 1 — Topology + Labels (Concrete Guidance)

Label key and StorageClass topology:
- Use the `site` label for placement (example: `site=hub`, `site=sea-edge-02`).
- Set `topologyKeys: ["site"]` in the StorageClass.

Node labeling examples:
```
AE_NODE_LABELS="role=hub,site=hub"
AE_NODE_LABELS="role=worker,site=sea-edge-02"
```

StorageClass example:
```
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: cephfs-rwx
provisioner: csi.ceph.com
parameters:
  csi.storage.k8s.io/fstype: ceph
topologyKeys:
  - site
```

Inputs:
- StorageClass with `topologyKeys: ["site"]`.
- Node labels including `site=<site-id>`.
- Provisioner registry entry with controller and node endpoints.

Outputs:
- PVC binds to a PV whose selected node matches the `site` label.
- VolumeAttachment references the selected node (when attachRequired=true).
- NodePublish is executed on the matching site node.

Validation checklist:
- `ae nodes` shows `site` labels on all nodes.
- PVC binds to the expected site.
- VolumeAttachment exists and `status.attached=true` (when attachRequired=true).

### Phase 4 — Sidecar parity (future)

1. Ensure full compatibility for external provisioner, attacher, snapshotter, resizer.
2. Implement leader election compatibility and required CRDs.
3. Expand reconciliation for VolumeSnapshot/VolumeSnapshotContent.
4. Add conformance and CSI sanity test coverage.

Exit criteria:
- Standard CSI sidecar deployments run without k1s-specific patches.
- Core CSI sanity tests pass for the reference driver.

## Legacy / Alternate Paths (Not chosen as entry point)

### Option 1 — Exec-based CSI hooks

- Fast to implement but non-standard and driver-specific.
- Adds bespoke hook wiring and JSON contracts that drift from CSI sidecars.
- Not selected as the entry point, but could be used as a short-term bridge.

### Option 3 — Sidecar parity as the entry point

- Full compatibility, but requires the gRPC foundation and robust storage semantics.
- High effort and risk without first landing controller/node gRPC clients.
- Deferred until after Option 2 is fully validated.
