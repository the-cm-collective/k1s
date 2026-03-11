# Fabric Control Plane

Status: proposed design record for the formal fabric direction.

This document freezes the main architecture boundary for the AMD-first fabric program. `k1s` can support more than one future fabric path only if controller authority, planning, provider-edge intake, session realization, and transport are treated as separate axes instead of one overloaded "fabric controller" concept.

The underlying HA control-plane authority model is tracked separately in [HA Control Plane Roadmap](high-availability-control-plane.html). This document assumes that backend foundation rather than re-defining election, fencing, and transport authority rules inside the fabric program.

## v1 Decisions

- `k1s` remains authoritative for reconcile, safety gates, and rollback behavior.
- The near-term v1 path is AMD-first, using AI Max+ 395 cells as the first authoritative fabric-session implementation target.
- The near-term deployment topology uses a provider-facing HA edge in front of the `k1s` AMD fabric. That topology is described in [Fabric Deployment Topology](fabric-deployment-topology.html).
- The backend HA authority path is `etcd` for truth and NATS/JetStream for transport. Provider-edge HA does not replace backend controller HA.
- Hyperon is advisory in v1. It may rank or explain safe choices, but it does not become controller authority.
- DAS and locality-first knowledge cells are later-phase workload families, not v1 prerequisites.
- External consumption remains API or ingress based. It is not modeled as one shared cluster fabric session.

## Why the Split Matters

The current branch already contains separate seams for:

- deterministic planning
- admission checking
- provider-edge intake
- session creation
- node-side session materialization

That is the correct direction. The system becomes brittle if these concerns are collapsed into one `fabric.provider` idea or if the provider edge is allowed to become the authority for internal fabric state.

## Control-Plane Axes

| Axis | Question | Current baseline | Planned v1 default |
| --- | --- | --- | --- |
| Controller authority | Who is authoritative for reconcile and safety? | `k1s` | `k1s` |
| Planning engine | Who ranks or explains placements? | deterministic | deterministic, later Hyperon advisory |
| Provider edge | Who owns lease-facing ingress and tenancy boundary? | none | HA edge |
| Session provider | Who creates the execution session contract? | baseline broker | AMD-first provider path |
| Node materializer | Who makes the session real on each node? | baseline node agent | provider-aware node materializer |
| Transport mode | How is connectivity realized? | `lan_direct`, `wg_ephemeral` | same modes, provider-specific expansion later |
| External provider mode | How does one cluster consume another cluster's inference? | out of band | ingress/API, not shared session state |

## Shared Contract

All future variants need one shared controller-facing contract.

Required shared objects:

- `FabricControlRequest`
- `FabricControlDecision`
- `FabricSessionRecord`
- `ProviderCapabilities`
- `DecisionTrace`

Required shared lifecycle:

1. `requested`
2. `validating`
3. `ready`
4. `degraded`
5. `tearing-down`
6. `removed`

Provider-specific metadata can extend the shared objects, but it must not replace the shared lifecycle or shared readiness fields.

## Safe Pairings

The supported pairings for the formal path are:

| Planning engine | Provider edge | Session provider | Authority | Result |
| --- | --- | --- | --- | --- |
| deterministic | HA edge | AMD-first provider | `k1s` | v1 default |
| Hyperon advisory | HA edge | AMD-first provider | `k1s` | later safe pairing |

The pairing rule is fixed:

- planning may change without changing controller authority
- the provider edge may change without redefining internal fabric lifecycle
- Hyperon may advise the controller, but it does not replace the controller as the source of hard safety truth in v1

## Capability-Driven Hardware Policy

The public AI Max node profile is documented in [AI Max+ 395 Hardware Baseline](ai-max-395-hardware-baseline.html), but the control plane should not hard-code decisions directly against product names.

The controller and planner should instead consume typed capability facts such as:

- storage and media class
- persistent-memory class when present
- RNIC family and RDMA mode support
- negotiated PCIe width and speed where it materially affects the data path
- interface role such as management, execution, or fabric

That keeps the public hardware story concrete without turning the control plane into a SKU-specific rules engine.

## Current Branch Mapping

| Current seam | Formal role |
| --- | --- |
| `StagePlanner` | deterministic planning engine |
| `BoundaryBudgetAdmission` | deterministic admission evaluator |
| `FabricBroker` | baseline session-provider seam |
| `FabricAgentClient` | node materializer seam |
| `fabric.mode` | transport mode |
| current fabric session metadata | precursor to a shared session record |

## Later Hyperon Path

Hyperon belongs at the control-plane and fabric layer, but in a bounded sequence:

- first as an advisory planner that ranks, explains, and records divergence
- then as a bounded automation layer operating against typed facts and explicit locality state
- later as part of a self-optimizing cognitive substrate

That final step is a later-stage roadmap objective. It is not a statement that the current repo already contains an autonomous cognitive control plane.

## Philosophy Constraints

Advanced cognitive-fabric work is constrained by the project's philosophy docs:

- [Project Philosophy](project-philosophy.html)
- [Cognitive Welfare and Continuity Safeguards](cognitive-welfare-and-continuity.html)

That means:

- later autonomy does not outrun governance
- continuity-changing operations become review-sensitive as substrate maturity rises
- capability metrics do not replace continuity, coherence, legibility, or bounded-distress concerns
- Hyperon integration is not only a capability question; it is also a responsibility and systems-governance question

## Immediate Documentation Consequences

Public docs should stop using "fabric controller" as one overloaded term for all future behavior.

The formal docs should instead describe:

- current inference fabric behavior
- the HA control-plane foundation that provider-edge and broker work depend on
- deployment topology and trust boundaries
- the authoritative control-plane split
- the staged roadmap from current fabric hardening to locality, advisory planning, and later cognitive behavior

## v1 Scope

The first formal development path should focus on:

1. making the current fabric lane operationally trustworthy
2. standing up the HA edge and broker boundary
3. introducing typed facts and telemetry
4. keeping the controller authoritative while new provider and planning seams mature
5. documenting the first AI Max hardware baseline separately from the control contract

## Non-Goals

This design does not:

- make Hyperon authoritative in v1
- require a Hyperon runtime for AMD-oriented execution
- require the provider edge to own internal fabric session truth
- require external providers to share internal cluster state
- define the full leader-election, epoch, or fencing schema for the HA backend core
- define final wire-level schemas for provider payloads

Those details live in the HA control-plane roadmap and related ADRs once the controller-facing contract is stable.
