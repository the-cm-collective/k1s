# ADR 0002 — Service VIP provider and ClusterIP emulation

Date: 2025-11-07
Status: Accepted
Owners: controller/network/ingress

## Context
- k1s needs a stable Service identity for ingress and app-to-app routing, especially for multi-replica and multi-node runs.
- HostPorts alone are insufficient: they are per-pod and change during rollouts.
- We want a minimal, portable dataplane that works on single-node labs and can grow into multi-node overlay routing.

## Decision
- Allocate Service VIPs from a Service CIDR and expose a provider interface that backs those VIPs.
- Use a per‑Service proxy on single-node (bridge) mode to emulate ClusterIP semantics.
- Provide an overlay-backed provider for multi-node runs, keeping ingress upstreams pointed at Service VIPs.

## Options Considered
1) **Keep HostPorts only**: simple but unstable, breaks multi‑replica ingress; rejected.
2) **Embed kube‑proxy/IPVS**: high complexity for the project scope; rejected.
3) **Service VIP + provider abstraction (chosen)**: smallest viable abstraction with clear growth path.

## Consequences
- Controller owns Service IP allocation and persists it for stability.
- Ingress configuration prefers Service VIPs to avoid per-pod endpoint churn.
- Single-node uses a lightweight proxy; multi-node uses an overlay provider, both behind the same interface.

## Action Plan
1) Maintain Service VIP allocation in the state store.
2) Keep bridge + overlay providers behind a shared interface.
3) Ensure ingress templates default to VIP upstreams when available.
