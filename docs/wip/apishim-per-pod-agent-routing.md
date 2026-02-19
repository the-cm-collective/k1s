# Apishim Per‑Pod Agent Routing (Proposal)

## Goal
Enable `kubectl exec` / dashboard shell and port‑forward for pods on any node
(core, SFO, SEA) while keeping edge sites lightweight. The core runs apishim;
edges only run `ae.node` + runtime + NATS gateway. Connectivity between core and
edge agents uses WireGuard reverse tunnels (NAT‑friendly).

## Current Gap
- apishim can only use a single `AE_APISHIM_AGENT_URL` (one agent) for runtime
  operations.
- Pod port‑forward in apishim connects directly to the pod IP/host ports from
  the apishim host; there is no generic agent‑backed fallback for Docker/Podman.

## Design Summary
Add an **Agent Router** inside apishim that resolves each pod to a node endpoint
and uses `RemoteRuntime` per pod for exec and port‑forward.

### Data Sources
Use controller state as the source of truth:
- `pod_nodes` mapping (pod_name -> node_id).
- `nodes` table for `node.endpoint` (the agent URL).

This keeps edges light and avoids running apishim on edges.

## Proposed Components

### 1) Agent Router (apishim)
**New internal module**: `apishim/agent_router.py`.

Responsibilities:
- Resolve `(namespace, pod_name)` -> `(node_id, agent_endpoint)`.
- Cache results with TTL.
- Retry by refreshing cache on first failure.

Routing sources (in order):
1. **Controller API** (preferred).
2. **Direct state DB** (only if explicitly configured and local).
3. **Static map** (dev fallback).

Suggested env knobs:
- `AE_APISHIM_AGENT_ROUTER=controller|db|static|off` (default: `controller`).
- `AE_APISHIM_AGENT_ROUTER_URL=http://<controller-host>:9108`.
- `AE_APISHIM_AGENT_ROUTER_TOKEN=<admin/read token>`.
- `AE_APISHIM_AGENT_ROUTER_TTL=10` (seconds).
- `AE_APISHIM_AGENT_MAP=/etc/ae/agent-map.json` (static fallback).

### 2) Controller API for Pod → Node
Expose pod‑to‑node placement and node endpoints from the controller HTTP API
(used by the router).

New endpoint proposal:
- `GET /pods/<namespace>/<pod>/node` → `{ node_id, endpoint }`.

Or extend existing:
- `GET /status/<app>?details=1` to include `node_id` and `endpoint` per pod.

Data comes from:
- `state.list_pod_nodes(app_name)` and `state.list_nodes()`.

### 3) Agent‑Backed Port‑Forward in apishim
Add a path in apishim’s port‑forward handling to use the agent’s
`/v1/portforward/attach` for **all runtimes**, not just CRI.

Mechanics:
- Resolve agent endpoint via router.
- For each requested port, open an agent port‑forward socket.
- Bridge SPDY/WebSocket channels to the agent socket instead of directly
  connecting to pod IPs.

Notes:
- Node agent port‑forward takes **one port per connection**.
- SPDY multi‑port requires multiple agent sockets; implement one socket per
  port and map by stream headers.

### 4) Exec/Shell via Agent
Exec already maps cleanly:
- Resolve agent endpoint via router.
- Use `RemoteRuntime.exec_attach` for streaming exec.
- Cache runtime per agent endpoint.

## WireGuard Reverse Tunnel Requirements
- Each edge node initiates a WireGuard tunnel to the core (NAT‑friendly).
- Node agent `AE_AGENT_ENDPOINT` must use the **WireGuard interface IP** so the
  core/apishim can reach it (e.g., `https://10.200.0.12:9109`).
- apishim should use WG IPs when routing to node endpoints.

This keeps the edges light and eliminates inbound NAT requirements.

## Security
- WireGuard provides network‑level encryption.
- Add TLS/mTLS on the node agent for defense‑in‑depth.

Suggested env on node agent:
- `AE_AGENT_TLS_CERT`, `AE_AGENT_TLS_KEY`,
  `AE_AGENT_CLIENT_CA`, `AE_AGENT_REQUIRE_CLIENT_CERT=1`.

Suggested env on apishim/controller:
- `AE_AGENT_CA_FILE`, `AE_AGENT_CERT_FILE`, `AE_AGENT_KEY_FILE` (for
  `RemoteRuntime`).

## Implementation Plan
1. **Controller API**: add endpoint(s) to return pod‑>node mapping + node
   endpoints.
2. **Agent Router**: add a small module for routing + cache.
3. **apishim exec routing**: replace single runtime path with
   `router.runtime_for_pod(...)`.
4. **apishim port‑forward routing**: add agent‑backed path (all runtimes), with
   per‑port agent sockets.
5. **Docs + ops**: update `docs/ops/core-edge-manual-test.md` with required
   `AE_AGENT_ENDPOINT` WG address and router envs.

## Risks / Edge Cases
- Stale placements: router should refresh on first failure and retry.
- Multi‑port port‑forward: agent supports one port per socket; SPDY handling
  must map streams correctly.
- Mixed runtimes: behavior should be consistent for Podman/Docker/CRI.

## Open Questions
- Should apishim learn pod→node from controller API only, or also from
  apishim store mirror for K8s objects?
- Do we need a dedicated auth token for router API calls?

