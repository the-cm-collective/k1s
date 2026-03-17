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

The first HA slice now removes local `specs/` authority in HA mode, elects one mutating controller, gates controller-native mutation and transport publication on that authority, and makes apishim workload mutation explicitly read-only until `H4`. `H2` fencing is now in place across controller work, gateway lease/work/route flows, remote runtime calls, and fabric session HTTP calls; gateways and node agents persist fence state and reject stale epochs, and controller ingress rejects stale work results and stale route acknowledgements. `H3` is now in progress: outbox rows persist deterministic publish metadata, HA JetStream work streams validate `R=3`, gateway spool replay uses bounded backoff and survives restart, route bundles stay on the periodic publish/ack path with reconnect-triggered resend, and transport metrics expose replay backlog and route ack age.

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
- apishim remains read/list/watch-capable in HA mode, but workload mutation is rejected until `H4`
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

### H4: Shared API and apishim convergence

Goal:
- eliminate the separate HA authority story for the Kubernetes-compatible API layer

Primary outcomes:

- apishim HA converges on `etcd`-backed revision, watch, compaction, and lease semantics
- the current Postgres-backed HA story is treated as transitional rather than the target end state
- list/watch behavior aligns with the same monotonic revision model used by the core controller
- controller and shim no longer depend on different durable authority backends in HA mode
- before `H4`, apishim workload mutation in HA mode stays intentionally read-only rather than pretending to share controller authority

### H5: Control-plane operations and recovery patterns

Goal:
- make the HA core operationally repeatable, not only architecturally plausible

Primary outcomes:

- documented bootstrap for a 3-node control plane
- backup, restore, member replacement, and rolling-upgrade procedures
- clear behavior for controller loss, node loss, `etcd` quorum loss, and JetStream impairment
- split-brain drills and stale-leader recovery documented in operator terms
- control-plane node role separation documented for AMD fabric deployments

## Dependency Model

This HA track is the foundation for later deployment work:

| Phase | Depends on | Why |
| --- | --- | --- |
| H1 | H0 | Leader election only matters after desired-state authority is shared. |
| H2 | H1 | Fencing depends on a real elected leader and an `etcd`-issued epoch. |
| H3 | H1, H2 | Durable transport is only safe after mutations are leader-gated and fenced. |
| H4 | H0, H1, H2 | The shared API should converge on the same authority model, not invent another one. |
| H5 | H1, H3, H4 | Recovery docs are only meaningful after authority, transport, and API convergence are defined. |

The fabric deployment milestones depend on this track rather than re-stating it:

| Fabric milestone | Additional HA dependency | Why |
| --- | --- | --- |
| D1 | H3 | The HA edge and broker boundary should not front a single-process backend authority. |
| D2 | H4 | Provider-backed intake should not depend on a second HA truth store. |
| D3 | H5 | Multi-cell operation needs tested failover and recovery patterns, not only topology docs. |
| D4 | H5 | Partner and domain operations require operator-readable recovery and governance paths. |

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
