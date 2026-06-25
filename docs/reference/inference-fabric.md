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

Today the most accessible hardware-backed validation surface for that lane is the bounded Nvidia development track documented in [Nvidia Development Baseline](nvidia-development-baseline.html). The formal deployment mainline remains AMD-first.

## How This Maps Forward

The formal roadmap targets AI Max+ 395-first execution cells behind a provider-facing HA edge. The current `InferenceCell` lane is the precursor to that path, not the finished deployment model.

The current physical-host development baseline for accessible validation is documented in [Nvidia Development Baseline](nvidia-development-baseline.html). The target deployment hardware baseline for the formal mainline is documented in [AI Max+ 395 Hardware Baseline](ai-max-395-hardware-baseline.html), with the actionable bring-up sequence in [AI Max+ 395 Cluster Prep](ai-max-395-cluster-prep.html).

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

### AI Max Edge Cell Contract

The opt-in `cellContract.profile: ai-max-edge-cell-v1` surface represents the
public contract for the first AI Max edge-cell shape. It validates:

- exactly four total members
- exactly one `gateway` member and three `cell-node` members
- all four members remain compute eligible
- optional gateway capacity reservation with `gatewayReservedGpuFraction`
- disconnected-operation intent under `cellContract.autonomy`
- LAN-local gateway discovery intent under `cellContract.gatewayDiscovery`
- NixOS installer signing and boot assurance intent under
  `cellContract.installer`

The autonomy block is intentionally declarative in the current repo. It gives
simulators, manifest validators, and later controller work stable names for the
edge behavior without claiming full runtime enforcement today:

```yaml
cellContract:
  profile: ai-max-edge-cell-v1
  gatewayReservedGpuFraction: 0.25
  autonomy:
    connectedMode: normal-connected
    coreLinkUnavailableMode: degraded-local-only
    reconnectMode: reconcile-on-restore
    coreLinkUptimeThresholdPct: 80
  gatewayDiscovery:
    mode: lan-local
    fabricCellCount: 4
    lanScope: floor-a
    gatewayPeerIds:
      - gateway-cell-b
      - gateway-cell-c
      - gateway-cell-d
  installer:
    profile: nixos-ai-max-edge-cell-installer-v1
    image: nixos-ai-max-edge-cell-installer
    signedBy: k1s-core-root-of-trust
    artifact:
      name: nixos-ai-max-edge-cell-installer
      profile: nixos-ai-max-edge-cell-installer-v1
      image: nixos-ai-max-edge-cell-installer
      version: stage7-local
      artifactDigest: sha256:1111111111111111111111111111111111111111111111111111111111111111
      manifestDigest: sha256:2222222222222222222222222222222222222222222222222222222222222222
      pathCoverage:
        - gateway
        - cell-node
      provenance:
        builder: k1s-public-stage7-local-simulator
        sourceRevision: public-dev-stage7
        createdAt: "2026-06-25T00:00:00Z"
    signature:
      algorithm: k1s-local-sim-ed25519-sha256
      signingKeyId: k1s-core-root-of-trust
      signedDigest: sha256:2222222222222222222222222222222222222222222222222222222222222222
      signature: k1s-sim-signature:3333333333333333333333333333333333333333333333333333333333333333
    roleScaffolds:
      - role: gateway
        moduleRef: nixos/modules/ai-max/installer/gateway.nix
        configRef: nixos/configs/ai-max/gateway-installed-system.nix
        derivedFromManifestDigest: sha256:2222222222222222222222222222222222222222222222222222222222222222
        postInstall:
          autoBoot: enabled
          connectTarget: core
          usbDevicePolicy: signed-only
          displayMode: telemetry
      - role: cell-node
        moduleRef: nixos/modules/ai-max/installer/cell-node.nix
        configRef: nixos/configs/ai-max/cell-node-installed-system.nix
        derivedFromManifestDigest: sha256:2222222222222222222222222222222222222222222222222222222222222222
        postInstall:
          autoBoot: enabled
          connectTarget: gateway
          usbDevicePolicy: limited
          displayMode: connect-monitor-to-gateway
    assurance:
      secureImageValidation: enabled
      bootValidation: measured-verified
      tamperDetection: enabled
      validationFailureAction: disable-quarantine
      coreAlerting: when-connected
    installPaths:
      - path: gateway
        postInstall:
          autoBoot: enabled
          connectTarget: core
          usbDevicePolicy: signed-only
          displayMode: telemetry
      - path: cell-node
        postInstall:
          autoBoot: enabled
          connectTarget: gateway
          usbDevicePolicy: limited
          displayMode: connect-monitor-to-gateway
```

Current implementation:

- validates the contract shape and accepted autonomy mode names
- validates LAN-local discovery mode names and fabric cell counts of `1`, `2`,
  `4`, or `8`
- requires peer gateway IDs to match the requested fabric cell count
- validates that the AI Max cell uses one installer profile/image with exactly
  two install paths: `gateway` and `cell-node`
- requires the installer contract to be signed by the k1s core root of trust
  and to declare enabled secure image validation, measured/verified boot intent,
  tamper detection, disable/quarantine response, and connected-core alerting
- validates a local Stage 7 installer artifact signing envelope with
  deterministic artifact and manifest digests, root-of-trust signing key ID,
  simulation signature, provenance fields, and path coverage for both installer
  roles
- validates a local Stage 8 NixOS role scaffold for the single installer image,
  including exactly one `gateway` and one `cell-node` installed-system intent,
  non-empty module/config references, derivation from the signed Stage 7
  manifest digest, and role-specific post-install services
- validates post-install posture for gateway and cell-node paths, including
  auto-boot, role-specific connect targets, constrained USB policy, and display
  mode
- constrains `coreLinkUptimeThresholdPct` to `0..80`
- keeps the gateway compute eligible while letting reservation affect placement planning

Planned runtime mapping:

- `normal-connected` means normal controller/core connectivity is available
- `degraded-local-only` means edge-local services should continue when core or
  internet connectivity is unavailable
- `reconcile-on-restore` means local state should reconcile when core
  connectivity returns, rather than being discarded
- `lan-local` means gateway discovery is scoped to one physical LAN or lab LAN
  simulator namespace
- `fabricCellCount` counts four-node cells, not individual nodes; a value of
  `4` represents sixteen compute-eligible members across four cells
- `nixos-ai-max-edge-cell-installer-v1` describes a single NixOS installer
  image that supports gateway and cell-node install paths
- `artifactDigest`, `manifestDigest`, and `signature` are Stage 7 local
  signing-envelope scaffold fields. They make verification behavior executable
  in manifest tests; they are not a real ISO build or production signature.
- `roleScaffolds` is the Stage 8 role-scaffold-ready surface. It describes the
  NixOS module/config intent that the single signed installer would use to
  produce gateway and cell-node installed systems.
- `secureImageValidation`, `bootValidation`, `tamperDetection`,
  `validationFailureAction`, and `coreAlerting` are declarative assurance
  requirements for later installer, TPM/Secure Boot, attestation, and alerting
  enforcement

This is contract-level behavior only. It does not yet implement disconnected
gateway execution, local service failover, live LAN discovery, multi-cell
routing, post-reconnect state replay, actual ISO build/signing, TPM/Secure Boot
enforcement, attestation verification, USB policy enforcement, real key custody,
production ISO signing, real NixOS image realization, or hardened
alert transport.

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
  - [Nvidia Development Baseline](nvidia-development-baseline.html)
  - `docs/ops/gpu-fabric-abc-lan.md`
  - [VM Variant Runbook](vm-variant-runbook.html)
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
- Current Nvidia development baseline:
  - [Nvidia Development Baseline](nvidia-development-baseline.html)
- AI Max hardware contract:
  - [AI Max+ 395 Hardware Baseline](ai-max-395-hardware-baseline.html)
- AI Max cluster prep:
  - [AI Max+ 395 Cluster Prep](ai-max-395-cluster-prep.html)
- VM variants, HA lab wrappers, and bootstrap:
  - [VM Variant Runbook](vm-variant-runbook.html)
- Backend HA operator contract:
  - [Operations Runbook](runbook.html)
  - [HA Closeout](ha-closeout.html)
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
