# HA Closeout

Status: canonical HA audit and evidence artifact for the completed `H*` track.

This document is the canonical closeout artifact for the HA control-plane roadmap. Its job is to answer three questions in one place:

- which `H0` through `H5` promises are already implemented
- what concrete evidence exists for those promises
- whether any remaining gaps must be fixed before the HA track can be marked complete

Operator bootstrap entrypoint: [HA Cluster Bring-Up](ha-cluster-bring-up.html). This document is the audit and evidence surface, not the primary numbered day-0 bring-up guide.

## Current Decision

- Result: the primary VM/lab HA lane is green on the checked-in topology, the drill-enabled HA variant is green, and the `H*` track is now marked complete in the roadmap table.
- Evidence: `runs/ha-cp-drills-20260319T213601Z/summary.json` and `runs/ha-cp-drills-20260319T213601Z/ha_summary.json` are green, including the optional `ha_drill_leader_failover`, `ha_drill_etcd_restart`, and `ha_drill_transport_recovery` checks.
- Secondary evidence: `make ha-closeout-e2e` passed locally on 2026-03-19. The wrapper-backed reduced harness now primes the Nix `libstdc++` runtime path, preflights `import grpc`, and then runs `tests/integration/test_ha_closeout_e2e.py`.
- Reason the track is closed: the primary and secondary evidence lanes are green, `must_fix_before_closeout` is empty, and the roadmap decision checkpoint has been recorded.

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
  - `lab/variants/ha-control-plane-core.yaml` is the checked-in HA closeout variant for the 3-core-plus-1-site topology.
  - `lab/variants/ha-control-plane-core-drills.yaml` is the checked-in deeper-validation variant that enables the optional disruptive drill hooks.
- Shared backend bootstrap:
  - `scripts/lab/vm/ha_shared_infra.sh` now bootstraps shared `etcd` and shared hub NATS/JetStream on the three `k1s-ha-core` VMs before `k1s-ha-core` starts.
  - `scripts/lab/vm/smoke_v2.py` runs that step as the `ha_shared_infra` global phase when the HA variant points its endpoints back at the three HA core VM IPs.
- Bootstrap:
  - `scripts/lab/vm/k1s_bootstrap.sh` can launch `k1s-ha-core` nodes with HA env instead of assuming a singleton `k1s-core`.
  - The VM lane now treats prereq-ready images as part of the contract: first-pass bootstrap assumes baked `python` aliasing, `crictl`, CNI binaries/config, and valid containerd config.
  - `AE_VM_BOOTSTRAP_AUTOFIX=1` remains available only as a manual debug fallback; the default lane fails stale-image boots fast and points operators back to `image build` plus `image verify`.
- Acceptance lane:
  - `make lab-vm-smoke` is the preferred operator entrypoint for the VM lane.
  - That target now wraps `scripts/lab/vm/smoke_helper.py`, which in turn wraps `smoke_v2.py`, prints live phase/check status from the run artifacts, and can auto-run `variant_down.sh` after a successful pass.
  - `scripts/lab/vm/smoke_v2.py` now supports `ha_control_plane`.
  - The lane writes `runs/<RUN_ID>/ha_summary.json`.
- Acceptance engine:
  - `ha_core_preflight.py`
  - `ha_core_upgrade.py`
  - `ha_transport_upgrade.py`
  - `ha_edge_transport.py`
  - optional `ha_core_drills.py` subcommands when the variant supplies disruptive drill commands
    - `ha-control-plane-core-drills.yaml` wires those commands through `scripts/lab/vm/ha_drill_actions.sh`

### Secondary: Reduced Local HA Harness

#### Relation to HA Bring-Up

- Real multi-host strict-CRI HA commands now live on [HA Cluster Bring-Up](ha-cluster-bring-up.html#ha-command-readout).
- This section remains the reduced one-box regression lane, not a supported single-host 3x `k1s-ha-core` cluster.

- Entry point:
  - `make ha-closeout-e2e`
  - wrapper: `scripts/dev/ha_closeout_e2e.sh`
- Scope:
  - 2 HA controllers
  - shared etcd
  - single local hub NATS/JetStream for reduced regression only
  - 1 apishim
  - 1 edge site
  - 1 gateway / worker path
- What it proves:
  - shared-authority writes remain usable during HA mode
  - controller failover advances authority to a new leader
  - gateway replay stays bounded after restart under the new leader
  - the reduced harness forces `AE_JS_REPLICAS=1`; it is not the milestone transport-fidelity lane
  - the wrapper-backed entrypoint validates the supported local runtime path for gRPC-backed HA authority before the test begins

## Gap Register

### must_fix_before_closeout

- none

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
- no remaining closeout blocker invalidates the current HA roadmap/runbook claims
- the HA track is complete as of the 2026-03-19 roadmap closeout checkpoint

### Post-Closeout Dashboard Amendment

- `H5c-amend-ha-dashboard` extends the built-in Hive dashboard after closeout so operators can see HA authority, etcd summary, transport pressure, and edge-site status from one live snapshot.
- This is a post-closeout observability amendment, not a reopened HA correctness milestone.
- The amendment does not change the original `H5c-ha-closeout` evidence rule, capability matrix, or closure decision; it only improves the integrated operator surface.

## Close Criteria

The HA track can be marked complete only when all of the following are true:

1. The VM/lab `ha_control_plane` lane has been executed on the intended HA topology and `runs/<RUN_ID>/ha_summary.json` is green.
   - Current strongest evidence run: `runs/ha-cp-drills-20260319T213601Z/ha_summary.json`
   - This drill-enabled run includes the optional leader-failover, etcd-restart, and transport-recovery hooks.
2. The reduced local HA harness has been run successfully as a secondary/manual regression check.
   - Current strongest evidence command: `make ha-closeout-e2e`
   - Current result: passed locally on 2026-03-19 through the wrapper-backed reduced harness entrypoint.
3. [HA Control Plane Roadmap](high-availability-control-plane.html), [Roadmap Status](roadmap-status.html), [Operations Runbook](runbook.html), and generated `docs/site` output match the implemented HA surface.
   - The canonical day-0 operator bootstrap page is [HA Cluster Bring-Up](ha-cluster-bring-up.html).
4. No `must_fix_before_closeout` gaps remain in this document.
5. A final roadmap decision checkpoint is recorded when the status table flips from `In progress` to complete.
   - Current checkpoint: 2026-03-19 HA control-plane closeout checkpoint recorded in [Roadmap Status](roadmap-status.html).

## Operator Notes

- The milestone-defining control-plane role is `k1s-ha-core`, not `k1s-core`.
- The milestone-defining edge lane remains `k1s-edge-core` / `k1s-edge-core-cri`.
- The helper scripts listed above are the real operator contract; the closeout lane is an integration wrapper around them, not a second HA API.
