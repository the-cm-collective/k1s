# ADR 0016 — Core/Edge Overlay, Control Plane, and Ingress Modes

Date: 2026-02-05
Status: Accepted
Implemented: 2026-02-10
Owners: controller/runtime/ingress

## Context
- k1s must run across multiple sites where edges are often behind NAT/CGNAT.
- The control plane needs durable dispatch while edges stay lightweight.
- The data plane needs a secure overlay for node-to-node traffic and remote exec/port-forward.
- Ingress must support NAT-friendly routing as well as public edge ingress.

## Decision
- **Control plane transport**: use NATS Core + JetStream at the hub, with leaf-node remotes for edge sites.
- **Durable dispatch**: JetStream streams and consumers live in the hub only; edge gateways are the only JS clients.
- **Source of truth**: etcd remains the hub SoT for desired/current state and work ledger.
- **Data plane overlay**: use WireGuard between hub and edges, with optional Rosenpass-managed PSK for PQ resistance.
- **Node endpoints**: node agents advertise a controller-facing endpoint (prefer WireGuard IPs) and pod CIDR.
- **Ingress modes** (select via `AE_EDGE_INGRESS_MODE`):
  - `core-proxy`: core ingress proxies to edge via rathole tunnel (NAT-friendly).
  - `core-to-edge-public`: core ingress proxies directly to a public edge ingress.
  - `edge-local`: edge ingress serves traffic locally; core distributes route bundles only.
  - `core-local`: core-only ingress for hub-local workloads.

## Consequences
- Hubs must run etcd + NATS (JetStream enabled) and the controller.
- Edge sites run a local Edge NATS leader, site gateway, and node agents; gateways keep a local spool for durability.
- Node agents require elevated privileges for WireGuard configuration and Rosenpass integration.
- Ingress behavior depends on the selected mode and whether edge ingress is publicly reachable.

## Action Plan
- Keep dev helpers aligned with the modes: `make k1s-core`, `make k1s-edge-core`, `EDGE_INGRESS_MODE=...`.
- Ensure node agents advertise WireGuard endpoints (`AE_AGENT_ENDPOINT`) and pod CIDRs.
- Maintain runbooks covering the three ingress modes and the hub/edge overlay steps.
