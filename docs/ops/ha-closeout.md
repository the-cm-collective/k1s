# HA Closeout

Status: canonical HA audit and evidence artifact for the completed `H*` track.

This document is the canonical closeout artifact for the HA control-plane roadmap. Its job is to answer three questions in one place:

- which `H0` through `H5` promises are already implemented
- what concrete evidence exists for those promises
- whether any remaining gaps must be fixed before the HA track can be marked complete

Operator bootstrap entrypoint: [HA Cluster Bring-Up](ha-cluster-bring-up.html). This document is the audit and evidence surface, not the primary numbered day-0 bring-up guide.

## Current Decision

- Closure checkpoint: the original `H*` track closure remains the 2026-03-19 roadmap checkpoint, recorded after the primary VM/lab HA lane, the drill-enabled HA variant, and the reduced local harness all passed.
- Historical closeout evidence: `runs/ha-cp-drills-20260319T213601Z/summary.json` and `runs/ha-cp-drills-20260319T213601Z/ha_summary.json` are green, including the optional `ha_drill_leader_failover`, `ha_drill_etcd_restart`, and `ha_drill_transport_recovery` checks.
- Secondary closeout evidence: `make ha-closeout-e2e` passed locally on 2026-03-19. The wrapper-backed reduced harness now primes the Nix `libstdc++` runtime path, preflights `import grpc`, and then runs `tests/integration/test_ha_closeout_e2e.py`.
- Latest post-closeout validation: image verification hardening closed the verifier overlay/backing-image mismatch on 2026-04-07, `make lab-vm-ha-validation` reran green with `stage1`, `retained`, `drain`, `stage2`, `stage2-live`, and `drills` all passing, and `make ha-closeout-e2e` also passed.
- Strongest 2026-04-07 run artifacts:
  - `runs/20260407T172320Z_ha_attached_node_stage1/ha_summary.json`
  - `runs/20260407T174926Z_multi_non_gpu_drain/summary.json`
  - `runs/20260407T175928Z_ha_core_stage2/ha_summary.json`
  - `runs/20260407T181153Z_ha_core_live/ha_summary.json`
  - `runs/20260407T182343Z_ha_core_drills/ha_summary.json`
- Artifact boundary: `retained` and the helper portion of `stage2-live` are wrapper-level stage results from `make lab-vm-ha-validation`; the one-shot and drill stages above are the stages that emit standalone `ha_summary.json` or `summary.json` artifacts.
- Reason the track stays closed: the historical closeout evidence still stands, the latest post-closeout validation is green, the reduced harness remains green, `must_fix_before_closeout` is empty, and no reopened roadmap gap has appeared.

## Practical HA Capabilities Today

- Shared authority: HA mode uses shared controller state in `etcd` as desired-state authority instead of per-node local truth.
- Single mutating leader with hot standbys: one controller owns leader-only mutation and transport publication, while healthy followers still serve read surfaces.
- Fenced correctness: controller epochs and fenced mutation envelopes reject stale or duplicate leader-era work instead of allowing split-brain side effects.
- Shared-authority convergence: workloads, passive objects, HPA, RBAC, CRDs, storage, CSI, and snapshot-facing resource flows now converge on shared authority.
- Strict-CRI HA bootstrap: the supported HA operator contract is `k1s-ha-core` plus shared `etcd` and shared NATS/JetStream on the strict-CRI path.
- Stage-1 ingress: the attached-node HA topology proves a schedulable worker can register with the HA core and serve traffic through `core-local` Envoy ingress.
- Stage-2 edge routing: the checked-in edge topology proves a worker-pinned edge app can serve traffic through true `core-proxy` routing from the HA core.
- Worker operations: the retained stage-1 lane covers node inventory plus `cordon` / `uncordon`, and the separate two-worker validation lane covers real drain-plus-reschedule behavior.
- Disruptive recovery drills: leader failover, external-etcd restart, and transport recovery drills now rerun green on the checked-in drill topology.

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
  - `make lab-vm-ha-validation` is the preferred umbrella rerun for the checked-in HA validation flow.
  - That target wraps `scripts/lab/vm/run_ha_validation.sh`, which runs the documented HA stages in sequence and prints a final per-stage pass/fail summary.
  - `make lab-vm-smoke` remains the lower-level one-shot entrypoint for `stage1`, `stage2`, and `drills`.
  - The one-shot path still wraps `scripts/lab/vm/smoke_helper.py`, which in turn wraps `smoke_v2.py`, prints live phase/check status from the run artifacts, and can auto-run `variant_down.sh` after a successful pass.
  - `scripts/lab/vm/smoke_v2.py` now supports `ha_control_plane`.
  - One-shot and drill runs write `runs/<RUN_ID>/ha_summary.json`; the supplemental drain lane writes `runs/<RUN_ID>/summary.json`.
  - `retained` and the helper portion of `stage2-live` are wrapper-level stage checks from `run_ha_validation.sh`; they do not currently emit separate standalone `ha_summary.json` artifacts.
- Acceptance engine:
  - `ha_core_preflight.py`
  - `ha_core_upgrade.py`
  - `ha_transport_upgrade.py`
  - `ha_edge_transport.py`
  - optional `ha_core_drills.py` subcommands when the variant supplies disruptive drill commands
    - `ha-control-plane-core-drills.yaml` wires those commands through `scripts/lab/vm/ha_drill_actions.sh`

#### Umbrella Validation Stages

- `stage1`: stage-1 one-shot acceptance on `lab/variants/ha-control-plane-attached-node.yaml`
- `retained`: retained stage-1 workstation flow covering `purge -> up -> status -> workload-smoke` plus node inventory and `cordon` / `uncordon`
- `drain`: supplemental two-worker non-HA drain/reschedule validation
- `stage2`: stage-2 one-shot acceptance on `lab/variants/ha-control-plane-core.yaml`
- `stage2-live`: live `make lab-vm-ha-core-workload-smoke` helper check against a live `ha-control-plane-core` run
- `drills`: deeper disruptive validation on `lab/variants/ha-control-plane-core-drills.yaml`

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

### Companion: Retained Local HA VM Harness

- Umbrella runner stage: `make lab-vm-ha-validation` includes this lane as the `retained` stage.
- Entry points:
  - `make lab-vm-ha-attached-node-up`
  - `make lab-vm-ha-attached-node-status`
  - `make lab-vm-ha-attached-node-workload-smoke`
  - `make lab-vm-ha-attached-node-purge`
- What it proves:
  - the workstation-facing retained HA lane can be rerun as `purge -> up` without manual bridge cleanup
  - on NixOS, the retained helper applies and verifies the local DNS/TLS bridge before `up` reports success
  - the public Envoy docs/dashboard/API hosts are reachable from one workstation in the retained lane
  - a schedulable attached node can register with the HA core, receive a workload through shared HA state, and serve it through `core-local` Envoy ingress with `curl --resolve`
  - node inventory plus `cordon` / `uncordon` on `attached-node-1` remain green in the retained topology
- What it is not:
  - not the milestone-defining closeout evidence lane
  - not a replacement for the checked-in `ha_control_plane` and drill-enabled variants
  - not the drain-plus-reschedule evidence lane; that coverage remains the separate `drain` stage because `attached-node-1` is the only schedulable `role=worker,site=core` node in this retained topology
  - not a widened workstation host-mapping contract; `ha-web-smoke.home.arpa` stays helper-only / `curl --resolve` only

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
- the 2026-04-07 maintenance rerun confirms those same claims still hold on the checked-in HA validation flow

### Post-Closeout Dashboard Amendment

- `H5c-amend-ha-dashboard` extends the built-in Hive dashboard after closeout so operators can see HA authority, etcd summary, transport pressure, and edge-site status from one live snapshot.
- This is a post-closeout observability amendment, not a reopened HA correctness milestone.
- The amendment does not change the original `H5c-ha-closeout` evidence rule, capability matrix, or closure decision; it only improves the integrated operator surface.

## Close Criteria

The HA track can be marked complete only when all of the following are true:

1. The VM/lab `ha_control_plane` lane has been executed on the intended HA topology and `runs/<RUN_ID>/ha_summary.json` is green.
   - Current strongest evidence run: `runs/ha-cp-drills-20260319T213601Z/ha_summary.json`
   - This drill-enabled run includes the optional leader-failover, etcd-restart, and transport-recovery hooks.
   - Current post-closeout rerun: `make lab-vm-ha-validation` passed on 2026-04-07 with green `stage1`, `retained`, `drain`, `stage2`, `stage2-live`, and `drills` results.
2. The reduced local HA harness has been run successfully as a secondary/manual regression check.
   - Current strongest evidence command: `make ha-closeout-e2e`
   - Current result: passed locally again on 2026-04-07 through the wrapper-backed reduced harness entrypoint.
3. [HA Control Plane Roadmap](high-availability-control-plane.html), [Roadmap Status](roadmap-status.html), [Operations Runbook](runbook.html), and generated `docs/site` output match the implemented HA surface.
   - The canonical day-0 operator bootstrap page is [HA Cluster Bring-Up](ha-cluster-bring-up.html), including the retained VM companion harness and its workload-through-Envoy smoke path.
4. No `must_fix_before_closeout` gaps remain in this document.
5. A final roadmap decision checkpoint is recorded when the status table flips from `In progress` to complete.
   - Current checkpoint: 2026-03-19 HA control-plane closeout checkpoint recorded in [Roadmap Status](roadmap-status.html).
   - Current post-closeout validation checkpoint: 2026-04-07 HA validation maintenance checkpoint recorded in [Roadmap Status](roadmap-status.html).

## Operator Notes

- The milestone-defining control-plane role is `k1s-ha-core`, not `k1s-core`.
- The milestone-defining edge lane remains `k1s-edge-core` / `k1s-edge-core-cri`.
- The helper scripts listed above are the real operator contract; the closeout lane is an integration wrapper around them, not a second HA API.
- `make lab-vm-ha-validation` is now the fastest umbrella rerun for the checked-in HA capability story; drop to the stage-specific `make lab-vm-smoke`, retained helpers, or drill helpers only when you need a narrower lane.
- The retained HA VM harness is the preferred workstation companion lane for manual docs/dashboard smoke, NixOS bridge validation, and stage-1 attached-node `core-local` workload ingress checks on `attached-node-1`.
- `make lab-vm-ha-attached-node-workload-smoke` deploys `ha-web-smoke` on `attached-node-1` and verifies stage-1 `core-local` routing through the HA core Envoy with `curl --resolve`.
- `make lab-vm-ha-core-workload-smoke` is the stage-2 helper for live `lab/variants/ha-control-plane-core.yaml` runs; it deploys `ha-edge-web-smoke` on `edge-sea-node` and verifies true `core-proxy` routing through the edge gateway/worker topology.
