# ADR 0007 — Multi-node architecture and scope

Date: 2025-12-16
Status: Accepted
Owners: controller/runtime/network

## Context
- Multi-node support is required while keeping the existing API/CLI surface stable for users.
- We need routable Service VIPs across nodes, node-aware scheduling, and remote exec/log/probe support.

## Decision
- Keep a single controller (SQLite by default; Postgres optional) and add per-node agents (`ae-node`) plus a RemoteRuntime adapter.
- Use Service VIP allocation with an overlay provider (HAProxy per Service) as the primary cross-node dataplane; bridge provider remains for single-node fallback.
- Preserve a single ingress entrypoint (Caddy on the controller) that targets Service VIPs.
- Enforce storage locality by pinning retained volumes to the node that created them.
- Secure controller↔agent traffic with mTLS and single-use join tokens.

## Non-goals (initial cut)
- HA controller/state, distributed storage replication, Windows nodes, or full kube-proxy parity.

## Consequences
- Scheduler honors `nodeSelector`, taints/tolerations, and storage pinning; NotReady nodes are skipped with reschedule limits.
- Remote exec/logs/probes go through the agent; HTTP/TCP probes prefer Service VIPs.
- Docs and labs live in `docs/guides/multinode-lab.md` and `ops/dev/multinode-lab.sh`.
