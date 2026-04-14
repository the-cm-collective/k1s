# HA Closeout

Status: canonical HA audit and evidence artifact for the completed `H*` track.

This document is the canonical closeout artifact for the HA control-plane roadmap. Its job is to answer three questions in one place:

- which `H0` through `H5` promises are already implemented
- what concrete evidence exists for those promises
- whether any remaining gaps must be fixed before the HA track can be marked complete

Operator bootstrap entrypoint: [HA Cluster Bring-Up: day-0 guide](ha-cluster-bring-up.html). This document is the audit and evidence surface, not the primary numbered day-0 bring-up guide.

## Current Decision

- The original `H*` track closure remains the 2026-03-19 roadmap checkpoint after the primary VM/lab HA lane and the reduced local harness both passed.
- Historical closeout evidence remains `runs/ha-cp-drills-20260319T213601Z/summary.json` and `runs/ha-cp-drills-20260319T213601Z/ha_summary.json`, including the optional `ha_drill_leader_failover`, `ha_drill_etcd_restart`, and `ha_drill_transport_recovery` checks.
- The strongest post-closeout maintenance rerun remains 2026-04-07: `make lab-vm-ha-validation` stayed green across `stage1`, `retained`, `drain`, `stage2`, `stage2-live`, and `drills`, and `make ha-closeout-e2e` also passed.
- Strongest 2026-04-07 run artifacts:
  - `runs/20260407T172320Z_ha_attached_node_stage1/ha_summary.json`
  - `runs/20260407T174926Z_multi_non_gpu_drain/summary.json`
  - `runs/20260407T175928Z_ha_core_stage2/ha_summary.json`
  - `runs/20260407T181153Z_ha_core_live/ha_summary.json`
  - `runs/20260407T182343Z_ha_core_drills/ha_summary.json`
- `retained` and the helper portion of `stage2-live` remain wrapper-level stage results from `make lab-vm-ha-validation`; the one-shot and drill stages above are the stages that emit standalone `ha_summary.json` or `summary.json` artifacts.
- `must_fix_before_closeout` remains empty. Use [Roadmap Status](roadmap-status.html) for the dated checkpoint history and post-closeout amendment timeline.

## Capability Matrix

| Slice | Current capability | Primary evidence |
| --- | --- | --- |
| H0 | HA mode uses shared controller state as desired-state authority and disables local `specs/` authority | `tests/unit/test_controller_loop.py`, `tests/unit/test_registry_resource_version.py`, `tests/unit/test_apishim_ha_mode.py` |
| H1 | Lease-backed controller leadership and controller epochs gate mutating control-plane work | `tests/unit/test_controller_authority.py`, `tests/unit/test_transport_authority.py` |
| H2 | Fenced mutation envelopes plus executor duplicate/stale rejection are in place | `tests/unit/test_ha_fencing.py`, `tests/unit/test_gateway_service_fencing.py`, `tests/unit/test_node_server.py`, `tests/unit/test_remote_runtime_fencing.py` |
| H3 | Outbox replay, gateway replay, and JetStream HA validation are hardened without turning transport into truth | `tests/unit/test_transport_config.py`, `tests/unit/test_work_outbox.py`, `tests/unit/test_gateway_spool.py`, `tests/unit/test_route_bundle_sites.py` |
| H4a | Workload-core apishim mutation converges on shared controller authority | `tests/unit/test_apishim_ha_workload_authority.py`, `tests/unit/test_controller_apishim_mirror.py` |
| H4b1 | `CronJob`, `ConfigMap`, `Secret`, and `ServiceAccount` use shared authority; CronJob execution is leader-owned | `tests/unit/test_apishim_ha_passive_authority.py`, `tests/unit/test_cronjob_authority_controller.py`, `tests/unit/test_cri_runtime_apishim_reads.py` |
| H4b-hpa | HPA uses shared metrics plus shared authority and leader-owned scale writes | `tests/unit/test_hpa_authority.py`, `tests/unit/test_cri_runtime_workload_metrics.py`, `tests/unit/test_node_server.py` |
| H4b2a | `Namespace`, RBAC, and PDB resources use shared authority | `tests/unit/test_apishim_rbac.py`, `tests/unit/test_apishim_ha_passive_authority.py` |
| H4b2b-crd | CRDs and dynamic custom resources use shared authority and shared discovery refresh | `tests/unit/test_apishim_ha_crd_authority.py`, `tests/unit/test_apishim_ha_store.py` |
| H4b2c-core | `StorageClass`, PVC, and PV use shared authority and leader-owned storage reconcile | `tests/unit/test_storage_authority.py`, `tests/unit/test_storage_authority_startup.py`, `tests/unit/test_storage_controller.py` |
| H4b2c-csi | Snapshot and CSI resources converge on shared authority and controller-owned storage objects become API-edge read-only | `tests/unit/test_storage_authority.py`, `tests/unit/test_storage_controller.py`, `tests/unit/test_cri_runtime_apishim_reads.py` |
| H5a-core | `k1s-ha-core` bootstrap, snapshot, and first-line drills exist | `scripts/dev/ha_core_preflight.py`, `scripts/dev/etcd_snapshot.py`, `scripts/dev/ha_core_drills.py`, `tests/unit/test_ha_ops.py` |
| H5b1 | etcd recovery/member-replacement/quorum-restore helpers and authority metrics exist | `scripts/dev/etcd_recovery.py`, `tests/unit/test_etcd_recovery_script.py`, `tests/unit/test_metrics_per_app.py` |
| H5b2a | systemd-managed `k1s-ha-core` rolling-upgrade helpers and build identity surfaces exist | `scripts/dev/ha_core_upgrade.py`, `tests/unit/test_ha_core_upgrade_script.py`, `tests/unit/test_http_api_version.py`, `tests/unit/test_apishim_version.py` |
| H5b2b | shared hub NATS/JetStream upgrade and replacement helpers exist | `scripts/dev/ha_transport_upgrade.py`, `tests/unit/test_ha_transport_upgrade_script.py`, `tests/unit/test_ha_ops.py` |
| H5b2c | edge-site gateway-first / leader-last transport helpers plus per-gateway build visibility exist | `scripts/dev/ha_edge_transport.py`, `tests/unit/test_ha_edge_transport_script.py`, `tests/unit/test_metrics_per_app.py` |

## Evidence Lanes

### Primary: VM/Lab HA Lane

- Canonical rerun: `make lab-vm-ha-validation`
- Checked-in variants: `lab/variants/ha-control-plane-core.yaml` and `lab/variants/ha-control-plane-core-drills.yaml`
- Proves:
  - shared `etcd` and shared hub NATS/JetStream bootstrap on explicit `k1s-ha-core` hosts
  - stage-1 attached-node validation, stage-2 edge/core-proxy validation, and disruptive drill coverage
  - machine-readable one-shot and drill evidence via `ha_summary.json`
- Artifact boundary:
  - one-shot and drill stages emit standalone `ha_summary.json`
  - the supplemental drain lane emits `summary.json`
  - `retained` and the helper portion of `stage2-live` remain wrapper-level checks only
- Operator context: [HA Cluster Bring-Up](ha-cluster-bring-up.html), [VM Variant Runbook](vm-variant-runbook.html), and [Operations Runbook](runbook.html)

### Secondary: Reduced Local HA Harness

- Canonical rerun: `make ha-closeout-e2e`
- Wrapper: `scripts/dev/ha_closeout_e2e.sh`
- Proves a lightweight failover-plus-replay regression lane and the supported local gRPC-backed HA authority path.
- Boundary: this lane forces `AE_JS_REPLICAS=1`; it is not the primary transport-fidelity evidence lane.

### Companion: Retained Local HA VM Harness

- Canonical helper path: `make lab-vm-ha-attached-node-up`, `make lab-vm-ha-attached-node-status`, `make lab-vm-ha-attached-node-workload-smoke`, and `make lab-vm-ha-attached-node-purge`
- Proves workstation-facing docs/dashboard/API reachability, NixOS bridge validation, stage-1 `core-local` workload-through-Envoy smoke, and retained node inventory plus `cordon` / `uncordon`.
- Boundary: this lane is a companion workstation surface, not the milestone-defining closeout lane or the drain-plus-reschedule evidence lane.

## Gap Register

### must_fix_before_closeout

- none

### follow_on_non_blocking

- Single-host 3x HA emulation remains a useful lab convenience, but it is not required for the declared HA operator contract.
- Docker/Podman HA runtime parity remains out of scope; the declared HA runtime lane is strict `containerd`/CRI.
- Repo-managed orchestration for etcd or NATS recovery/upgrades remains out of scope; the operator contract is helper-driven, not one-shot orchestration.
- Shared NATS JWT/operator auth rotation and `nsc` workflow automation remain out of scope for the current HA milestone.

## Close Criteria

The HA track can be marked complete only when all of the following are true:

- The VM/lab `ha_control_plane` lane has been executed on the intended HA topology and the primary one-shot/drill artifacts are green.
  Canonical rerun surface: `make lab-vm-ha-validation`. Historical closeout anchor: `runs/ha-cp-drills-20260319T213601Z/ha_summary.json`.
- The reduced local HA harness has been run successfully as a secondary/manual regression check.
  Canonical entrypoint: `make ha-closeout-e2e`.
- [HA Control Plane Roadmap](high-availability-control-plane.html), [Roadmap Status](roadmap-status.html), [Operations Runbook](runbook.html), and generated `docs/site` output match the implemented HA surface.
  The canonical day-0 operator bootstrap page is [HA Cluster Bring-Up](ha-cluster-bring-up.html).
- No `must_fix_before_closeout` gaps remain in this document.
- The original closeout checkpoint and later maintenance validation are recorded on [Roadmap Status](roadmap-status.html).
