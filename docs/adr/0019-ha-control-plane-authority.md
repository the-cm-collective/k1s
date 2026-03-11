# ADR 0019 - HA control-plane authority and fencing

Date: 2026-03-11
Status: Proposed
Owners: controller/transport/apishim

## Context

- The repo already supports `etcd` as a controller state backend, but the controller still behaves like a single-process authority.
- The controller imports local `specs/` files into the shared registry and reconciles by polling.
- Transport already supports `nats-core` and `nats-js`, with gateway spooling and outbox-based JetStream publish paths.
- `controller_epoch` exists in node lease records, but the current transport stack still defaults to a static `AE_CONTROLLER_EPOCH`.
- Apishim HA currently documents a shared Postgres path, which creates a second HA authority story beside `etcd`.

We need one control-plane authority model that works for strict-CRI `k1s` core, backend fabric work, and later provider-edge integrations.

## Decision

- `etcd` is the single source of truth for desired state, leadership, controller epochs, resource ownership, reconcile progress, and fencing.
- `NATS` and `JetStream` are the transport plane only. They carry commands, results, events, and route publication, but they do not decide authority.
- HA controller mode is single-writer with hot standbys. Exactly one elected controller may mutate cluster state or publish mutating work at a time.
- Leadership is implemented with the existing `etcd` KV and lease APIs, not a second client stack. v1 uses a lease-backed leader key claimed through `etcd` transaction semantics.
- The controller epoch is derived from the winning leader key revision in `etcd`, not from a static env var and not from JetStream metadata.
- Mutating messages carry `controller_id`, `controller_epoch`, and `operation_id`. Executors persist the highest accepted epoch and reject stale commands.
- Local `specs/` import remains a dev convenience, but it is not the authoritative desired-state ingress path for multi-controller HA mode.
- The long-term HA target for apishim converges on the same `etcd` authority model. Postgres remains transitional support, not the target backend authority.

## Authority Rules

The system follows two hard rules:

1. watches and messages trigger work; only `etcd` transactions authorize work
2. transport may duplicate; executors must be idempotent; fencing makes duplicates harmless

Those rules apply to:

- controller reconcile writes
- gateway lease handling
- work dispatch
- route bundle publication
- service and ingress mutation
- fabric reservation and teardown

## Key Layout And Defaults

The HA controller authority tree lives under the existing `AE_ETCD_PREFIX`:

- `${AE_ETCD_PREFIX}/controlplane/leader`
- `${AE_ETCD_PREFIX}/controlplane/controllers/<controller_id>`

Leader record contents:

- `controller_id`
- `lease_id`
- `acquired_at`
- `version`
- `advertise_addr` when relevant

Default timings:

- controller leadership lease TTL: 15 seconds
- keepalive cadence: every 5 seconds
- standby election retry: 1 to 2 seconds with jitter when no leader record exists
- follower leader-key poll cadence for v1: 2 seconds

The leader epoch is the `etcd` create/mod revision of the current leader record. It is monotonic, cluster-issued, and does not require a second counter key.

## Transport And Payload Contract

Keep the current subject families:

- `k1s.v1.site.<site_id>.lease.acquire`
- `k1s.v1.site.<site_id>.lease.renew`
- `k1s.v1.work.site.<site_id>`
- `k1s.v1.site.<site_id>.routes.bundle`
- existing result, status, log, and capability subjects

Do not rename subjects for HA. Extend the payload envelope instead.

Required mutating envelope fields:

- `controller_id`
- `controller_epoch`
- `operation_id`
- `work_id` and `attempt` when the action is work-queue based
- desired generation or revision when the action reflects controller intent

Required receiver behavior:

- reject commands with an epoch lower than the highest accepted epoch for that scope
- treat a repeated `operation_id` for the same desired generation as a successful no-op
- echo `controller_id`, `controller_epoch`, and `operation_id` in results and acknowledgements

## Desired-State Ingress Contract

In HA mode:

- authoritative writes land in shared controller state
- the registry stored in `etcd` is the desired-state ingress point
- local file watching remains valid for single-node and development flows only

This keeps all controllers pointed at one desired-state surface and removes the need to replicate local files across controllers.

## Failure Behavior

- If the active controller loses `etcd` quorum or lease keepalive, it must stop mutating immediately.
- If a standby wins leadership, it uses the new leader-key revision as its epoch and resumes mutation from shared state.
- If a stale leader later reconnects, its commands are rejected by epoch fencing.
- If JetStream is impaired while `etcd` quorum survives, dispatch may stall or replay, but ownership remains correct because only the elected leader may authorize work.
- If `etcd` loses quorum, the control plane becomes read-only. No controller may allocate, publish new mutating work, or update ownership.

## Options Considered

1. Use JetStream or JetStream KV as the source of truth
- Rejected because it creates a second consensus authority for state ownership and leader election, and its read/consistency model is a poorer fit for hard authority.

2. Let controllers coordinate directly without a shared authority store
- Rejected because it adds a custom quorum problem where `etcd` already provides leases, revisions, and transactions.

3. Use `etcd` for authority and NATS/JetStream for transport
- Chosen because it matches the current repo seams and keeps authority and dispatch concerns cleanly separated.

## Consequences

- `AE_CONTROLLER_EPOCH` must stop being the mutating authority source in HA mode.
- Gateways, agents, ingress writers, and fabric allocators need durable fencing state, not best-effort in-memory checks.
- Polling reconcile remains acceptable for the first HA implementation because correctness comes from leader-gated writes, not from watch-driven scheduling.
- Apishim documentation and implementation should converge away from a separate Postgres-first HA authority story.

## Action Plan

1. Add controller leader acquisition and lease renewal under the `controlplane` `etcd` prefix.
2. Replace static controller epochs with `etcd`-derived leader epochs in controller transport paths.
3. Extend mutating work, route, lease, and result payloads with the fencing envelope.
4. Make gateways, node agents, ingress paths, and fabric allocators reject stale epochs and dedupe repeated operations.
5. Converge apishim HA storage semantics on the same `etcd` revision and lease model.
6. Document bootstrap, restore, and split-brain handling for the 3-node control-plane shape.
