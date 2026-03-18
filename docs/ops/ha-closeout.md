# HA Closeout

Status: final HA audit and evidence gate before the `H*` track can close.

This document is the canonical closeout artifact for the HA control-plane roadmap. Its job is to answer three questions in one place:

- which `H0` through `H5` promises are already implemented
- what concrete evidence exists for those promises
- whether any remaining gaps must be fixed before the HA track can be marked complete

## Current Decision

- Result: `H5c-ha-closeout` is implemented, but the HA track is not yet marked complete in the roadmap table.
- Reason: the source audit is clean enough to proceed, but the milestone-defining VM/lab `ha_control_plane` lane still needs to be executed and reviewed as integrated evidence.

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

## Integrated Evidence Lanes

### Primary: VM/Lab HA Lane

- Variant role model:
  - `scripts/lab/vm/lib/variant.py` now accepts explicit `k1s-ha-core` hosts.
- Bootstrap:
  - `scripts/lab/vm/k1s_bootstrap.sh` can launch `k1s-ha-core` nodes with HA env instead of assuming a singleton `k1s-core`.
- Acceptance lane:
  - `scripts/lab/vm/smoke_v2.py` now supports `ha_control_plane`.
  - The lane writes `runs/<RUN_ID>/ha_summary.json`.
- Acceptance engine:
  - `ha_core_preflight.py`
  - `ha_core_upgrade.py`
  - `ha_transport_upgrade.py`
  - `ha_edge_transport.py`
  - optional `ha_core_drills.py` subcommands when the variant supplies disruptive drill commands

### Secondary: Reduced Local HA Harness

- Entry point:
  - `AE_E2E_HA_CLOSEOUT=1 PYTHONPATH=src pytest -q tests/integration/test_ha_closeout_e2e.py`
- Scope:
  - 2 HA controllers
  - shared etcd
  - shared hub NATS/JetStream
  - 1 apishim
  - 1 edge site
  - 1 gateway / worker path
- What it proves:
  - shared-authority writes remain usable during HA mode
  - controller failover advances authority to a new leader
  - gateway replay stays bounded after restart under the new leader

## Gap Register

### must_fix_before_closeout

- None currently identified from the source audit.
- Track closure is still blocked on evidence, not on an open source-visible correctness gap:
  - the VM/lab `ha_control_plane` lane must be executed
  - the resulting `ha_summary.json` must be reviewed and retained with the closeout decision

### follow_on_non_blocking

- Single-host 3x HA emulation remains a useful lab convenience, but it is not required for the declared HA operator contract.
- Docker/Podman HA runtime parity remains out of scope; the declared HA runtime lane is strict `containerd`/CRI.
- Repo-managed orchestration for etcd or NATS recovery/upgrades remains out of scope; the operator contract is helper-driven, not one-shot orchestration.
- Shared NATS JWT/operator auth rotation and `nsc` workflow automation remain out of scope for the current HA milestone.

## Amendment Review

Items that were previously left outside individual HA slices were reviewed again for closeout:

- Single-host 3x HA harness: not required to make any current public HA claim true.
- Docker/Podman HA parity: not required because the public HA contract is strict `containerd`/CRI.
- Repo-managed NATS install/config surfaces: not required because the public HA contract explicitly treats shared NATS as externally managed.
- Additional apishim compatibility polish outside the converged resource sets: not required for the HA control-plane claims themselves.

Current conclusion:

- no deferred item needs to be pulled back into the `H*` track to make current HA roadmap/runbook claims honest
- the remaining blocker is integrated evidence, not a newly discovered missing feature slice

## Close Criteria

The HA track can be marked complete only when all of the following are true:

1. The VM/lab `ha_control_plane` lane has been executed on the intended HA topology and `runs/<RUN_ID>/ha_summary.json` is green.
2. The reduced local HA harness has been run successfully as a secondary/manual regression check.
3. `docs/roadmap/high-availability-control-plane.md`, `docs/roadmap/status.md`, `docs/ops/runbook.md`, and generated `docs/site` output match the implemented HA surface.
4. No `must_fix_before_closeout` gaps remain in this document.
5. A final roadmap decision checkpoint is recorded when the status table flips from `In progress` to complete.

## Operator Notes

- The milestone-defining control-plane role is `k1s-ha-core`, not `k1s-core`.
- The milestone-defining edge lane remains `k1s-edge-core` / `k1s-edge-core-cri`.
- The helper scripts listed above are the real operator contract; the closeout lane is an integration wrapper around them, not a second HA API.
