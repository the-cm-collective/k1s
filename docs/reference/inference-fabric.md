# Inference Fabric

Status: experimental current-state reference for the `InferenceCell` fabric lane.

This page describes what exists in the repo today for distributed inference across multiple nodes and sites. It is not the long-term roadmap. For the formal phase path, see [Distributed Compute Fabric Roadmap](distributed-compute-fabric.html). For the backend HA foundation that precedes provider-edge fabric work, see [HA Control Plane Roadmap](high-availability-control-plane.html). For the control-plane design boundaries, see [Fabric Control Plane](fabric-control-plane.html). For the target deployment layout, see [Fabric Deployment Topology](fabric-deployment-topology.html).

## What Ships Today

- Native manifest kinds:
  - `InferenceCell`
  - `InferenceCellSet`
- Controller surface:
  - `InferenceCellController`
  - `InferenceCellSetController`
  - `StagePlanner`
  - `BoundaryBudgetAdmission`
  - `LocalFabricBroker`
- Node-agent fabric endpoints:
  - `POST /v1/fabric/ensure_session`
  - `POST /v1/fabric/teardown_session`
  - `GET /v1/fabric/sessions`
- CLI surfaces:
  - `ae cell apply|status|events|delete`
  - `ae cellset apply|scale|status`
  - `ae fabric sessions`

## Current Intent

The current fabric lane exists to prove that `k1s` can:

- place a distributed inference workload across explicitly chosen members
- gate admission on topology and link budgets
- reserve GPU slots and rendezvous ports before launch
- create a fabric-session contract before worker and leader startup
- drive worker and leader workloads through one controller-owned lifecycle

The current lane is suitable for labs, controller development, and staged validation. It is not yet a production-grade multi-site fabric.

## How This Maps Forward

The formal roadmap targets AI Max+ 395-first execution cells behind a provider-facing HA edge. The current `InferenceCell` lane is the precursor to that path, not the finished deployment model.

The target hardware baseline for that path is documented in [AI Max+ 395 Hardware Baseline](ai-max-395-hardware-baseline.html), with the actionable bring-up sequence in [AI Max+ 395 Cluster Prep](ai-max-395-cluster-prep.html).

Forward mapping:

- `InferenceCell` is the current controller-owned precursor to a future cell session contract
- `LocalFabricBroker` is the current precursor to a real session-provider and broker boundary
- named members and staged placement are the precursor to a formal cell membership model
- persisted fabric-session, GPU lease, and port lease records are the precursor to a broader fabric control record

## Manifest Model

`InferenceCell` is a controller-owned workload type for distributed inference execution. The current model includes:

- explicit `members` with `nodeId`, `site`, and `gpuCount`
- executor choices for Ray or mp/vLLM-style paths
- `fabric.mode` with `lan_direct` and `wg_ephemeral`
- `fabric.policyMode` including `strict_ports`
- placement hints such as `packStagesBySite`
- budget and boundary inputs including link metrics

`InferenceCellSet` is a template-and-scale wrapper around repeated `InferenceCell` creation.

## Controller Lifecycle

The controller runs a fixed phase machine:

1. `PENDING`
2. `ADMITTING`
3. `RESERVING`
4. `FABRIC`
5. `STARTING_WORKERS`
6. `STARTING_LEADER`
7. `JOINING`
8. `READY`
9. `RESTARTING`
10. `FAILED`

High-level flow:

1. validate the named members and current node readiness
2. plan stage placement with `StagePlanner`
3. evaluate boundary and budget constraints with `BoundaryBudgetAdmission`
4. reserve GPU slots, ports, and optional node locks
5. create a fabric session through the broker
6. ask each node agent to ensure the session
7. launch worker and leader workloads on the selected nodes
8. mark the cell ready only after worker, leader, fabric, and API conditions converge

## Session and Lease Behavior

Current persisted artifacts in controller state include:

- inference cell status and allocations
- inference cell events
- fabric-session records
- GPU lease records
- port lease records
- node lock records for strict-port cases

The controller records session metadata such as:

- `fabric_session_id`
- `fabric_ifname`
- `member_fabric_ips`
- `fabric_allowed_rules`
- `fabric_mode`
- `master_addr`

## What Works Now

- deterministic stage placement across named members
- cross-site boundary admission checks using manifest-supplied link metrics
- GPU slot reservation before workload launch
- rendezvous and API port reservation
- Ray primary path with optional mp fallback
- `InferenceCellSet` expansion and scale-down
- fabric session persistence in controller state
- VM and LAN validation workflows through:
  - `docs/ops/gpu-fabric-abc-lan.md`
  - `docs/ops/vm-variant-runbook.md`
  - `docs/ops/vm-metrics-and-gates.md`

The current lane remains standard-transport-first. RoCE is being documented as the first acceleration path for later phases, not as the current default execution path.

## Current Limits

- `LocalFabricBroker` is still a baseline broker, not a production provider
- node-agent session handling is lightweight and not yet a full readiness-proof system
- `lan_direct` and `wg_ephemeral` are transport modes, not mature provider families
- admission still depends heavily on manifest-provided `linkMetrics`
- typed capability facts for GPU, fabric, PMem, and RNIC state do not yet exist as first-class controller inputs
- the fabric lane is not yet the default runtime path for ordinary `Deployment` workloads
- multi-GPU and impairment-heavy validation is still incomplete compared with the non-GPU harness

## Not Here Yet

The current lane does not yet provide:

- the backend `etcd`-authoritative HA core that later provider-edge milestones depend on
- the provider-facing HA edge that fronts the formal roadmap deployment shape
- provider-backed lease lifecycle and brokered reservation flow
- typed hardware and link facts as first-class controller inputs
- RoCE-capable accelerated movement as the default or required transport
- multi-cell locality and cache-control behavior
- Hyperon advisory planning or later cognitive-fabric behavior

## Operational Entry Points

- Host and LAN pattern:
  - `docs/ops/gpu-fabric-abc-lan.md`
- AI Max hardware contract:
  - `docs/reference/ai-max-395-hardware-baseline.md`
- AI Max cluster prep:
  - `docs/ops/ai-max-395-cluster-prep.md`
- VM variants and bootstrap:
  - `docs/ops/vm-variant-runbook.md`
- Throughput and baseline gates:
  - `docs/ops/vm-metrics-and-gates.md`
- Remote GPU VM precursor:
  - `docs/ops/gpu-vm-remote-host-validation.md`

## Design Boundaries

The current repo uses the right seams for future evolution:

- `StagePlanner` is the deterministic planning baseline
- `BoundaryBudgetAdmission` is the deterministic admission baseline
- `FabricBroker` is the session-provider seam
- `FabricAgentClient` is the node materialization seam

Those seams should evolve without collapsing planning, session creation, transport realization, and external inference consumption into one provider concept.

## Non-Goals for This Reference

This page does not define:

- the long-term intelligent-fabric phase order
- Hyperon or DAS integration details
- the HA provider-edge topology
- funding narrative or market positioning
- provider-specific future wire contracts

Use [HA Control Plane Roadmap](high-availability-control-plane.html) for the backend HA foundation, [Fabric Control Plane](fabric-control-plane.html) for the formal design split, [Fabric Deployment Topology](fabric-deployment-topology.html) for the target deployment shape, and [Distributed Compute Fabric Roadmap](distributed-compute-fabric.html) for the formal dev path.
