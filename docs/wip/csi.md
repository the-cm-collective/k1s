# CSI integration plan

Status: active WIP tracker
Owner area: storage/controller
Intended destination: `docs/design/` when the implementation contract stabilizes

This document tracks the next steps to turn the current CSI compatibility layer into
production-grade CSI support that adheres to Kubernetes semantics and is suitable for
CNCF conformance testing.

## Phase 0 — Easy wins (near-term)

Goal: close semantic gaps without changing architecture.

- Respect `CSIDriver.spec.attachRequired`:
  - If `false`, do not create `VolumeAttachment` objects and do not block mounts on attachments.
  - If `true`, keep current `VolumeAttachment` gating.
- Extend the CSI marker payload (debug/traceability):
  - Write `fsType`, `readOnly`, `volumeAttributes`, and `publishContext` into the marker.
- Align read-only semantics:
  - Honor `pv.spec.csi.readOnly` for filesystem and block volumes.
- Improve eventing:
  - Emit explicit events for missing attachments, missing secrets, and mismatch between
    `CSIDriver.attachRequired` and PV expectations.
- Expand smoke tests:
  - Add `attachRequired=false` case.
  - Add CSI block PV with `volumeAttributes.devicePath` case.

Exit criteria:
- CSI PVs with `attachRequired=false` mount without a VolumeAttachment.
- CSI PVs emit clear events when prerequisites are missing.
- Tests cover block + attachRequired toggles.

## Phase 1 — Functional CSI “shim” (exec-based)

Goal: provide a minimal, driver-agnostic bridge to CSI without full gRPC.

- Add optional node hooks:
  - `AE_CSI_NODE_STAGE_HOOK`, `AE_CSI_NODE_PUBLISH_HOOK`, `AE_CSI_NODE_UNPUBLISH_HOOK`.
  - Hooks receive JSON input (PV/PVC/Secrets/VolumeContext) and must return JSON status.
- Add optional controller hooks:
  - `AE_CSI_CREATE_VOLUME_HOOK`, `AE_CSI_DELETE_VOLUME_HOOK`,
    `AE_CSI_CONTROLLER_PUBLISH_HOOK`, `AE_CSI_CONTROLLER_UNPUBLISH_HOOK`.
- Make hooks idempotent:
  - Repeated calls must be safe; return explicit `alreadyExists` / `notFound` status.
- Add hook timeouts and retries with backoff.
- Surface hook metrics and errors (events + `/metrics`).

Exit criteria:
- Basic CSI PVs can be provisioned/attached/mounted using hooks for a reference driver.
- Failure paths are visible via events and metrics.

## Phase 2 — Native CSI gRPC services (node + controller)

Goal: implement the CSI spec directly to remove the hook layer.

- Implement CSI Identity, Controller, and Node services.
- Support core calls:
  - Identity: `GetPluginInfo`, `GetPluginCapabilities`, `Probe`.
  - Controller: `CreateVolume`, `DeleteVolume`, `ControllerPublish`, `ControllerUnpublish`,
    `ListVolumes` (optional), `ValidateVolumeCapabilities`.
  - Node: `NodeStage`, `NodeUnstage`, `NodePublish`, `NodeUnpublish`, `NodeGetInfo`.
- Match CSI retry semantics (gRPC status codes + idempotency).
- Persist controller volume state (volume handle, topology, capacity).

Exit criteria:
- A CSI driver can be registered and used with k1s without external hooks.
- All controller + node calls are idempotent and recoverable after restarts.

## Phase 3 — Kubernetes sidecar parity

Goal: support the standard CSI sidecars and their expectations.

- External provisioner: full support for `CreateVolume` + `DeleteVolume` + topology.
- External attacher: `VolumeAttachment` semantics and attachment status.
- External snapshotter: `VolumeSnapshot` + `VolumeSnapshotContent` semantics.
- External resizer: `ExpandVolume` and PVC/PV capacity updates.
- Implement leader election compatibility for sidecars.

Exit criteria:
- A standard CSI deployment with sidecars works without k1s-specific patches.

## Phase 4 — Production hardening

Goal: robust behavior under churn, failures, and upgrades.

- Strong reconciliation loops for:
  - PVC/PV binding (consistent phases, claimRef updates).
  - VolumeAttachment lifecycle (cleanup on node loss).
  - Snapshot/restore state transitions.
- Recover safely after crashes (idempotency + state reconciliation).
- Add audit logging for CSI calls and storage events.
- Finer-grained metrics (latency, retries, error codes).
- Validate all inputs with clear, K8s-like error events.

Exit criteria:
- Operator runbooks cover common failure modes and remediation.
- System recovers cleanly after forced restarts with no orphaned attachments.

## Phase 5 — Conformance and CNCF readiness

Goal: strict Kubernetes semantics and conformance coverage.

- Run upstream CSI sanity tests against the implementation.
- Run Kubernetes storage conformance (where applicable) and track gaps.
- Document any deviations and close gaps with targeted fixes.
- Add CI gates for CSI sanity + storage conformance subsets.

Exit criteria:
- CSI sanity suite passes.
- Core storage conformance scenarios pass for the reference driver.

## Notes

- Always follow the Kubernetes API semantics for PVC/PV phases, VolumeAttachment
  behavior, and snapshot lifecycle transitions.
- Keep compatibility with external CSI drivers as the top design constraint.
