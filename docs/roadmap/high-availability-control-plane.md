# High Availability Control Plane Roadmap

Status: proposed public roadmap for true HA `k1s` core.

This roadmap defines the control-plane foundation that must exist before `k1s` can claim a true HA mode. It precedes the AMD fabric deployment work in [Distributed Compute Fabric Roadmap](distributed-compute-fabric.html) and gives that later roadmap a stable authority model to depend on instead of re-defining core HA inside provider-edge or fabric docs.

The core position is deliberate:

- `etcd` is the single source of truth for controller authority, desired state, leases, revisions, and fencing
- `NATS` and `JetStream` are the transport and replay plane for work, events, and route distribution
- a 3-controller deployment is not a controller voting cluster; the quorum lives in the 3-member `etcd` cluster

## Summary

The repo already contains several of the primitives needed for this path:

- an `etcd`-backed controller state lane
- controller and node leases with `controller_epoch` fields
- NATS Core and JetStream transport modes
- outbox-based dispatch and gateway spool durability

The first HA slice now removes local `specs/` authority in HA mode, elects one mutating controller, gates controller-native mutation and transport publication on that authority, and makes non-converged apishim mutation explicitly read-only until the later `H4b*` slices. `H2` fencing is now in place across controller work, gateway lease/work/route flows, remote runtime calls, and fabric session HTTP calls; gateways and node agents persist fence state and reject stale epochs, and controller ingress rejects stale work results and stale route acknowledgements. `H3` now hardens outbox replay, gateway replay, and JetStream HA validation without turning transport into truth. `H4a` routes workload-core resources through shared controller authority in HA mode. `H4b1` converges `ConfigMap`, `Secret`, `ServiceAccount`, and `CronJob`, `H4b-hpa` runs leader-only HPA scaling from shared workload metrics, `H4b2a` extends shared authority to `Namespace`, RBAC, and `PodDisruptionBudget`, `H4b2b-crd` converges CRDs plus dynamic custom-resource routing, `H4b2c-core` routes `StorageClass`, PVC, and PV through shared authority while the elected main controller owns the core storage reconcile loop, and `H4b2c-csi` now brings the remaining snapshot and CSI resources onto the same shared-authority model while CRI and node-agent storage reads stop depending on local shim DB state. `H5a-core`, `H5b1-etcd-recovery`, and `H5b2a-core-upgrades` now give that surface a real bootstrap, snapshot, drill, recovery, and rolling-upgrade story. `H5b2b-hub-transport-upgrades` now covers shared hub NATS/JetStream upgrade and replacement procedures, `H5b2c-edge-transport-upgrades` extends that posture to edge-site gateway-first / leader-last transport choreography with per-gateway build visibility and bounded leaf reconnect validation, and `H5c-ha-closeout` adds the final audit plus integrated evidence lanes needed before the `H*` track can close.

The two design rules for the whole program are:

- watches and messages trigger work; only `etcd` transactions authorize work
- transport may duplicate; executors must be idempotent; fencing makes duplicates harmless

For the underlying consistency and transport behavior, see:

- [etcd API guarantees](https://etcd.io/docs/v3.5/learning/api_guarantees/)
- [etcd leader election tutorial](https://etcd.io/docs/v3.5/tutorials/how-to-conduct-elections/)
- [NATS JetStream clustering](https://docs.nats.io/running-a-nats-service/configuration/clustering/jetstream_clustering)
- [NATS queue groups](https://docs.nats.io/nats-concepts/core-nats/queue)

## Why This Precedes Fabric HA

The fabric roadmap already assumes an HA edge, broker boundary, and later multi-cell growth. Those claims become ambiguous if the backend `k1s` core itself is still a single-process authority with no fencing.

This roadmap therefore comes first:

- fabric D0 may continue as a single-cell validation track
- fabric D1 and later deployment milestones depend on the control-plane HA phases here
- provider-edge HA does not replace backend controller HA; it fronts it

## Authority Model

The target HA shape is:

- 3 control-plane nodes
- 3 `etcd` members across those nodes
- 3 controller processes, one per control-plane node
- 3 hub NATS servers with JetStream enabled for durable streams at `R=3`
- dedicated worker and fabric nodes outside the control-plane set whenever size permits

Role split:

- `etcd`: authority, revisions, elections, leases, ownership, fencing epochs
- active controller: the only process allowed to mutate cluster state or publish mutating work
- standby controllers: hot spares that observe shared state and take over on leader loss
- NATS/JetStream: work dispatch, replay, route publication, async events, partition buffering
- agents, gateways, ingress writers, fabric allocators: idempotent executors that honor fencing tokens

HA mode also tightens the desired-state boundary:

- authoritative desired-state writes must land in shared controller state
- local `specs/` import remains valid for single-node and dev flows
- local `specs/` import is not the authority path for multi-controller HA mode

## Phases

### H0: Shared desired-state authority

Goal:
- remove local-only desired-state assumptions from true HA mode

Primary outcomes:

- `etcd`-backed shared controller state is the only authoritative desired-state registry in HA mode
- file import from `specs/` is explicitly documented as dev-only for HA deployments
- controller-native CLI and controller API writers land intent into the same shared registry in HA mode
- apishim remains read/list/watch-capable in HA mode, but non-converged mutation is rejected until the relevant later `H4b*` slice
- revision and generation semantics are shared across controller replicas

### H1: Leader election and controller epochs

Goal:
- ensure exactly one controller can authorize mutations at a time

Primary outcomes:

- controllers campaign for leadership through a lease-backed `etcd` key under the controller prefix
- the winner becomes the only mutating controller until its lease expires or is revoked
- every leadership win yields a monotonically increasing controller epoch derived from `etcd`
- non-leaders remain hot standbys and never publish mutating work
- followers reject leader-only controller mutations with leader hints and stop mutating background loops on authority loss

### H2: Fencing and idempotent mutation envelopes

Goal:
- turn split-brain from a correctness failure into a stale-command rejection path

Primary outcomes:

- every mutating action carries `controller_id`, `controller_epoch`, and `operation_id`
- gateways, node agents, ingress writers, and fabric allocators persist the highest accepted epoch
- stale epochs are rejected and duplicate `operation_id` replays become no-ops
- destructive and allocative paths become epoch-aware, including container lifecycle, route publication, service updates, and fabric reservation work

Current implementation status:

- controller work attempts, route bundles, remote runtime calls, and fabric session mutations now emit fenced mutation envelopes
- site gateways persist fence state, reject stale lease/work/route commands, and echo accepted envelopes on results and route acknowledgements
- node agents reject stale runtime and fabric mutations and treat duplicate `operation_id` requests as successful no-ops
- controller ingress now validates work acknowledgements and rejects stale work results and stale route acknowledgements instead of trusting transport ordering
- `/metrics` exposes `ae_ha_fence_stale_total`, `ae_ha_fence_duplicate_total`, and `ae_ha_fence_epoch_advance_total` so failover and stale-command behavior are visible during rollout

### H3: Transport hardening around `etcd` authority

Goal:
- make durable transport safe without turning transport into authority

Primary outcomes:

- work dispatch, route bundles, and async reconcile messages publish only from the active leader
- outbox dispatch re-checks leadership before publish and after failover
- JetStream remains the durable hub path with `R=3` streams for critical work subjects
- gateway result replay is restart-safe and uses bounded retry/backoff instead of a hot loop
- route bundles remain on the current periodic publish/ack path in this phase; reconnect forces immediate resend and metrics expose pending ack age
- transport degradation may pause or delay work, but it does not corrupt ownership because truth remains in `etcd`
- the first HA slice already moves mutating NATS request/reply bindings to the active leader; full replay safety still depends on `H2`

Current implementation status:

- outbox entries persist deterministic publish subject and `Nats-Msg-Id`, and the publisher re-checks authority before publish and before marking rows published
- HA mode validates JetStream work stream and consumer replicas at bootstrap instead of silently accepting topology drift
- gateway spool results now persist replay attempts, next retry time, and last replay error so buffered results can survive restart and replay with bounded backoff
- gateway reconnect resets replay scheduling immediately instead of waiting for a fixed retry interval
- route bundles still use periodic publish/ack, but reconnect now resets pending sites for immediate resend and `/metrics` exposes route publish counters, pending state, and `ae_route_bundle_ack_age_seconds`
- gateway telemetry now exposes `ae_gateway_result_replay_total`, `ae_gateway_result_replay_fail_total`, and `ae_gateway_result_replay_backlog`

### H4a: Workload-core apishim convergence

Goal:
- restore HA mutation for converged workload-core resources without re-introducing a second truth store

Primary outcomes:

- apishim HA workload mutation for `Deployment`, `StatefulSet`, `DaemonSet`, `Job`, `Deployment/scale`, and attached `Service`/`Ingress` now lands in shared controller authority instead of the local shim DB
- the converged workload surface reads back from shared controller state instead of the adapter/mirror path in HA mode
- attached `Service` and `Ingress` writes are treated as workload-owned intent and fail closed on ambiguous mappings
- the current Postgres-backed shim store is treated as transitional and legacy-only for non-converged resources in HA mode
- non-converged shim resource families remain read/list/watch-capable, but mutation stays explicitly unsupported until the relevant later `H4b*` slice

Current implementation status:

- HA mode now uses a multiplex shim store that routes converged workload-core resources onto shared controller authority while leaving legacy resources on the existing object store
- controller registry entries carry workload-kind and attached `Service`/`Ingress` identity labels so the shim can synthesize converged reads without consulting the legacy DB
- HA apishim workload PUT/DELETE/scale flows now perform CAS writes against controller authority and return `409 Conflict` on stale `resourceVersion`
- controller HA mode disables the old apishim mirror fallback and materializes converged `DaemonSet` desired replicas from controller-visible node count during reconcile

### H4b1: CronJob and passive shim-object convergence

Goal:
- converge `CronJob`, `ConfigMap`, `Secret`, and `ServiceAccount` onto shared HA authority without reopening storage or HPA side paths

Primary outcomes:

- `CronJob`, `ConfigMap`, `Secret`, and `ServiceAccount` gain shared-authority HA read/write/watch behavior
- the elected controller owns `CronJob` execution in HA mode; schedule cursor and last-run status live in shared authority state
- HA mode disables the apishim `StorageController`, so storage watch loops do not keep mutating outside the converged authority boundary
- containerd/CRI runtime reads for converged passive resources stop depending on local shim DB authority in HA mode

Current implementation status:

- HA mode now routes `ConfigMap`, `Secret`, `ServiceAccount`, and `CronJob` writes through the generic shared-authority apishim store instead of the legacy shim DB
- the apishim `StorageController` no longer starts in HA mode, keeping storage watch loops off shim replicas while later controller-owned storage slices converge the authority model
- `CriRuntime` now prefers the HA apishim HTTP read client for converged `Secret` and `ServiceAccount` lookups in HA mode and does not fall back to local shim DB authority for that surface
- the new controller-native `CronJobAuthorityController` runs only on the elected controller, creates deterministic child `Job` entries through workload authority, and persists `lastScheduleTime`/`lastSuccessfulTime` in shared authority state

### H4b-hpa: Shared-metrics HPA convergence

Goal:
- re-enable HA `HorizontalPodAutoscaler` mutation only after a controller-visible shared metrics source exists

Primary outcomes:

- HPA state and scale writes converge on the same shared HA authority model as `H4a` and `H4b1`
- autoscaling decisions no longer depend on per-replica adapter-host runtime stats
- HPA remains read-only in HA mode until this shared-metrics slice lands

Current implementation status:

- HA apishim now routes `autoscaling/v2` HPA CRUD/list/watch through shared authority with HA validation for supported resource metrics and converged target kinds
- node agents expose `/v1/workload_metrics`, `CriRuntime` aggregates per-workload CRI metrics, and the elected controller writes shared workload-metrics snapshots before applying HPA decisions
- the controller-native HPA authority loop now persists `lastScaleTime`, conditions, and current metrics in shared authority state and scales converged workloads through the existing CAS-protected workload authority path

### H4b2a: Built-in passive resource convergence

Goal:
- converge the remaining built-in passive shim resources that do not need new active control loops

Primary outcomes:

- `Namespace`, RBAC resources, and `PodDisruptionBudget` gain shared-authority HA CRUD/list/watch behavior
- RBAC evaluation becomes shared-authority-based across shim replicas instead of depending on local shim DB state
- storage stays explicitly read-only in HA mode during this slice
- the storage controller remains disabled in HA mode; `H4b2a` does not reopen PVC/PV/snapshot watch loops

Current implementation status:

- HA apishim now routes `Namespace`, `Role`, `RoleBinding`, `ClusterRole`, `ClusterRoleBinding`, and `PodDisruptionBudget` through the generic shared-authority store instead of the legacy shim DB
- the H4b2a cut expanded the HA mutation guard to those built-in passive resources without reopening storage-controller authority paths
- RBAC authorization evaluation now reads shared-authority `Role`/`Binding` objects across shim replicas, so cross-replica auth decisions no longer depend on local DB contents

### H4b2b-crd: CRD and custom-resource convergence

Goal:
- converge CRDs and dynamic custom-resource instances onto shared HA authority without reopening storage reconciliation

Primary outcomes:

- `CustomResourceDefinition` gains shared-authority HA CRUD/list/watch behavior
- served custom-resource GVRs route onto shared authority in HA mode instead of the legacy shim DB
- discovery for CRD-served groups and versions refreshes across shim replicas without restart
- storage, snapshot, and CSI resources stay explicitly read-only in HA mode during this slice

Current implementation status:

- HA apishim now routes `apiextensions.k8s.io/v1 CustomResourceDefinition` through shared authority instead of the legacy shim DB
- the HA store now treats served CRD GVRs as dynamic shared-authority resources, so custom-resource CRUD/list/watch bypasses the legacy shim DB in HA mode
- shim request handling refreshes its CRD discovery cache from shared authority in HA mode, so CRD discovery and custom-resource routing converge across replicas without process restart

### H4b2c-core: Core storage authority convergence

Goal:
- converge the core storage API path first without reopening snapshot or CSI scope

Primary outcomes:

- `StorageClass`, `PersistentVolumeClaim`, and `PersistentVolume` now route through shared HA authority in apishim
- the elected main controller now hosts the active core storage reconcile loop, so PVC/PV binding and the existing local-path/NFS provisioning path are leader-owned instead of shim-local
- the apishim-local `StorageController` stays disabled in HA mode
- snapshot and CSI resources remain explicitly read-only in HA mode during this slice

Current implementation status:

- HA apishim now routes `storage.k8s.io/v1 StorageClass`, core PVC, and core PV through shared authority instead of the legacy shim DB
- the main controller now hosts a leader-owned storage authority runner that seeds storage classes and runs the core PVC/PV reconcile loop over shared authority state
- the storage reconcile engine now has a core-storage mode that suppresses snapshot, CSI, and CSI capacity branches for this slice

### H4b2c-csi: Snapshot, CSI, and remaining storage-resource convergence

Goal:
- finish the remaining storage-specific convergence work after the lower-risk core storage cut lands

Primary outcomes:

- snapshot, CSI, and the remaining storage-specific mutation surfaces converge on the same shared HA authority model instead of the transitional shim DB path
- watch/resourceVersion/compaction behavior for the remaining HA storage surface aligns with the same monotonic revision model used by the controller and the earlier `H4*` slices
- Postgres and SQLite remain optional deployment backends, but they are no longer the HA authority story for public API behavior

Current implementation status:

- HA apishim now routes snapshot and CSI storage resources through shared authority
- controller-owned storage resources (`VolumeAttachment`, `CSIStorageCapacity`, `VolumeSnapshotContent`) are read-only at the HA API edge and are written only by the elected storage controller
- the leader-owned storage authority runner now re-enables snapshot, CSI, and storage-capacity reconciliation from the main controller
- CRI and node-agent storage reads now use the HA apishim HTTP path for PVC/PV/StorageClass/VolumeAttachment/CSIDriver lookups instead of falling back to local shim DB authority

### H5a-core: HA bootstrap, snapshots, and first-line drills

Goal:
- make the HA core operationally repeatable on the intended strict-CRI core profile, not only architecturally plausible

Primary outcomes:

- documented `k1s-ha-core` bootstrap contract for one HA core node in a shared 3-controller control plane
- fail-fast validation for shared/external `etcd` and NATS dependencies before starting the core profile
- etcd snapshot save/status/restore helper for HA backup workflows
- repeatable drills for leader failover, transport recovery, and external-etcd restart validation
- clear separation between `k1s-core` single-host dev behavior and `k1s-ha-core` shared-authority behavior

Current implementation status:

- `make k1s-ha-core` now starts a strict-CRI HA core node with `AE_HA_MODE=1` and refuses to bootstrap local singleton `etcd`, NATS, or Postgres
- the new `ha_core_preflight.py` helper validates required HA env, shared `etcd` reachability, and NATS reachability before startup
- `etcd_snapshot.py` provides explicit etcd snapshot save/status/restore tooling instead of overloading the existing SQLite/specs `ae backup` path
- `ha_core_drills.py` provides focused verification helpers for leader failover, transport recovery, and external-etcd restart drills

### H5b1-etcd-recovery: Member replacement, quorum loss, and stale-leader recovery

Goal:
- turn the HA control plane from bootstrap-ready into operator-recoverable around `etcd` authority loss and recovery

Primary outcomes:

- documented etcd member replacement procedure using the v3.5 learner workflow
- documented quorum-loss recovery path from snapshot into a fresh 3-member cluster
- stale-leader isolation, follower takeover, and safe rejoin documented in operator terms
- control-plane node role separation documented for AMD fabric deployments
- controller metrics expose leader state, authority health, and current epoch for recovery validation

Current implementation status:

- `scripts/dev/etcd_recovery.py` now exposes `endpoint-status`, `member-list`, `member-remove`, `member-add --learner`, `member-promote`, and `quorum-restore-plan`
- `src/ae/ha/ops.py` now provides shared etcd recovery command builders, learner-add parsing, and 3-member quorum-restore plan rendering
- controller metrics now expose `ae_controller_is_leader`, `ae_controller_epoch`, and `ae_controller_authority_healthy` for operator-visible recovery checks
- the runbook now documents member replacement, quorum-loss restore, stale-leader isolation, and control-plane role separation on top of the `k1s-ha-core` bootstrap surface

### H5b2a-core-upgrades: Rolling upgrades for `k1s-ha-core`

Goal:
- turn the recovery-ready HA core profile into a repeatable rolling-upgrade surface for controller, apishim, and core-node services

Primary outcomes:

- explicit installed-service surface for systemd-managed `k1s-ha-core` nodes
- operator-assisted precheck, per-node plan, and cluster verification helpers for follower-first and leader-last upgrades
- controller and apishim build/version visibility suitable for upgrade gating
- narrow two-build skew contract for one-node-at-a-time upgrades
- NATS/JetStream member replacement and transport-cluster upgrade sequencing stay explicitly deferred to a later slice

Current implementation status:

- `make install-ha-core-systemd` and `make uninstall-ha-core-systemd` now manage an explicit HA node-role service surface built around `ae-ha-core.service`, `/etc/ae/ha-core.env`, and `/usr/local/bin/ae-ha-core-service`
- `scripts/install.sh` now exposes `ha-core-install` and `ha-core-uninstall` actions without changing the older single-node install path
- `scripts/dev/ha_core_upgrade.py` now provides `precheck`, `node-plan`, and `cluster-verify` commands for operator-assisted rolling upgrades
- controller and apishim now expose `GET /__ae/version`, and controller metrics now expose `ae_controller_build_info{version,sha,date} 1`
- the runbook now documents follower-first, leader-last rolling upgrades with a strict two-build window for `k1s-ha-core`

### H5b2b-hub-transport-upgrades: Shared hub NATS/JetStream upgrade sequencing and replacement

Goal:
- extend the HA operating model from core-node upgrades to the shared hub NATS/JetStream cluster that `k1s-ha-core` depends on

Primary outcomes:

- documented hub NATS/JetStream member replacement and upgrade sequencing
- helper-driven hub transport precheck, per-node plan, cluster verification, and replacement planning that build on the finished `k1s-ha-core` rolling-upgrade surface
- controller transport health and NATS monitor endpoints are the validation surface, without introducing a repo-managed NATS install path
- any later edge-site NATS leader choreography stays explicitly separate from hub transport procedures

Current implementation status:

- `scripts/dev/ha_transport_upgrade.py` now provides `precheck`, `node-plan`, `cluster-verify`, and `member-replace-plan` for operator-assisted hub transport changes
- `src/ae/ha/ops.py` now provides shared hub NATS monitor target parsing, `/varz`/`/routez`/`/jsz`/optional `/leafz` fetch helpers, and health evaluation for route mesh plus JetStream replication
- the runbook now documents shared hub transport upgrade checks, helper usage, and controller metrics that gate hub restarts and replacement verification

### H5b2c-edge-transport-upgrades: Edge-site NATS leader upgrades and replacement choreography

Goal:
- extend the HA operating model from the shared hub transport cluster to edge-site NATS leader restart, replacement, and reconnect choreography

Primary outcomes:

- documented edge-site transport upgrade and replacement sequencing that builds on the finished shared hub procedures
- operator-assisted helper-driven gateway-first / leader-last choreography for one edge site at a time
- per-gateway visibility at the controller metrics surface so site recovery can be verified by node as well as by site
- explicit separation between shared hub transport operations and edge gateway/site transport operations
- `k1s-edge-core` / `k1s-edge-core-cri` remain the milestone-defining HA lane; `k1s-edge` / `k1s-core-edge` remain secondary compatibility rather than exit criteria

Current implementation status:

- `scripts/dev/ha_edge_transport.py` now provides `precheck`, `gateway-plan`, `site-verify`, `leader-plan`, and `leader-replace-plan` for operator-assisted edge-site transport changes
- `src/ae/ha/ops.py` now provides edge site target parsing, `/varz` and `/leafz` fetch helpers, site-health evaluation, and per-gateway status collection from controller metrics
- gateway status telemetry now includes build identity, and `/metrics` now exports `ae_site_gateway_last_seen_seconds{site,node}` plus `ae_site_gateway_build_info{site,node,version,sha,date}` alongside the existing site-wide replay and route convergence series
- the runbook now documents gateway-first / leader-last sequencing, helper usage, and the recovery checks required before moving on to the next gateway or edge leader step

### H5c-ha-closeout: Audit, integrated evidence, and track closure

Goal:
- turn the implemented `H0` through `H5` slices into a decision-complete HA track instead of leaving them as a long set of individually landed but never-audited slices

Primary outcomes:

- one canonical HA closeout artifact records the capability matrix, evidence map, open gaps, and closure criteria
- the VM/lab harness now understands explicit `k1s-ha-core` hosts and a `ha_control_plane` smoke lane that drives the already-landed HA helper family against a real HA-oriented variant
- a smaller reduced local HA e2e harness exists for nightly/manual regression outside the primary VM/lab evidence lane
- the HA track only closes after the audit shows no `must_fix_before_closeout` gaps and the primary evidence lane is green

Current implementation status:

- `docs/ops/ha-closeout.md` now records the H0-H5 capability matrix, evidence map, gap register, and close criteria
- `scripts/lab/vm/lib/variant.py`, `scripts/lab/vm/k1s_bootstrap.sh`, and `scripts/lab/vm/smoke_v2.py` now support explicit `k1s-ha-core` hosts plus a `ha_control_plane` lane that emits a machine-readable `ha_summary.json`
- the VM lane uses the existing HA helper surfaces for precheck, cluster verification, hub transport validation, edge transport validation, and optional drills instead of inventing a second HA operator contract
- `tests/e2e/ha_closeout.py` and `tests/integration/test_ha_closeout_e2e.py` now provide a reduced local HA topology with two controllers, one apishim, one edge site, and a failover-plus-replay check
- current audit result: no new source-visible `must_fix_before_closeout` gaps were found, but the track remains open until the primary VM/lab lane is executed and reviewed

## Dependency Model

This HA track is the foundation for later deployment work:

| Phase | Depends on | Why |
| --- | --- | --- |
| H1 | H0 | Leader election only matters after desired-state authority is shared. |
| H2 | H1 | Fencing depends on a real elected leader and an `etcd`-issued epoch. |
| H3 | H1, H2 | Durable transport is only safe after mutations are leader-gated and fenced. |
| H4a | H0, H1, H2 | Workload-core shim mutation can only converge after shared authority, leadership, and fencing exist. |
| H4b1 | H4a | `CronJob` and passive shim objects can extend the same authority model without reopening storage or HPA metrics scope. |
| H4b-hpa | H4b1 | HPA should only converge after the passive-resource cut and a controller-visible shared metrics source exist. |
| H4b2a | H4b1, H4b-hpa | Built-in passive resources should converge after the narrower CronJob/passive-object and shared-metrics HPA cuts land. |
| H4b2b-crd | H4b2a | CRD and custom-resource convergence should land before the remaining storage controller authority cut. |
| H4b2c-core | H4b2b-crd | Core storage authority can converge before the riskier snapshot and CSI paths. |
| H4b2c-csi | H4b2c-core | Snapshot and CSI convergence should wait until the core PVC/PV/StorageClass cut is stable. |
| H5a-core | H1, H3, H4b2c-csi | HA bootstrap and drills are only meaningful after authority, transport, and API convergence are defined. |
| H5b1-etcd-recovery | H5a-core | Member replacement, quorum loss, and stale-leader recovery should build on one real HA bootstrap and drill surface, not precede it. |
| H5b2a-core-upgrades | H5b1-etcd-recovery | Rolling upgrades for systemd-managed `k1s-ha-core` nodes should build on the finished etcd recovery posture instead of redefining it. |
| H5b2b-hub-transport-upgrades | H5b2a-core-upgrades | Shared hub NATS/JetStream upgrade sequencing should build on the finished core-node upgrade surface instead of mixing the first operator contract with transport-cluster replacement. |
| H5b2c-edge-transport-upgrades | H5b2b-hub-transport-upgrades | Edge-site NATS leader choreography should build on the shared hub upgrade posture instead of mixing shared-cluster and per-site recovery in one slice. |
| H5c-ha-closeout | H5b2c-edge-transport-upgrades | The HA track should only close after edge transport is in place and one integrated audit plus acceptance lane proves the full operator contract. |

The fabric deployment milestones depend on this track rather than re-stating it:

| Fabric milestone | Additional HA dependency | Why |
| --- | --- | --- |
| D1 | H3 | The HA edge and broker boundary should not front a single-process backend authority. |
| D2 | H4b2c-csi | Provider-backed intake should not depend on a second HA truth store or a partially converged shim authority model. |
| D3 | H5c-ha-closeout | Multi-cell operation needs a closed HA evidence lane and audited operator contract, not only individual recovery and upgrade slices. |
| D4 | H5c-ha-closeout | Partner and domain operations require the HA track to be decision-closed with auditable evidence and documented boundaries. |

## Failure Model

The desired failure behavior is simple and explicit:

- if one controller dies, another controller wins leadership through `etcd` lease expiry and continues
- if one control-plane node dies, `etcd` and JetStream remain available at 2/3 quorum
- if the old leader is partitioned, it loses lease keepalive and becomes stale; new commands from it are fenced
- if JetStream is impaired but `etcd` still has quorum, the system keeps truth and ownership in `etcd` while dispatch pauses or retries
- if `etcd` loses quorum, no controller may authorize new mutations; the system degrades to read-only truth plus buffered transport

## Tracking

Use [Roadmap Status](roadmap-status.html) as the canonical progress table for the `H*`, `F*`, and `D*` tracks.

Implementation-level authority details for this roadmap are frozen in `docs/adr/0019-ha-control-plane-authority.md`.
