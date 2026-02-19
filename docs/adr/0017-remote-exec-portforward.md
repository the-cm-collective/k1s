# ADR 0017 — Remote Exec and Port-Forward via apishim

Date: 2026-01-26
Status: Accepted
Implemented: 2026-02-10
Owners: runtime/controller/observability

## Context
- k1s needs kubectl/k9s-compatible exec and port-forward behavior.
- Dashboards and playgrounds require browser-friendly streaming.
- Pods may run on remote edge nodes reachable only through the WireGuard overlay.

## Decision
- **SPDY for CLI compatibility**: apishim exposes SPDY/3.1 exec and port-forward endpoints compatible with kubectl/k9s.
- **WebSocket for browsers**: apishim supports WebSocket exec (`v5.channel.k8s.io`) and port-forward (`portforward.k8s.io`).
- **Node-aware routing**: apishim resolves pod → node and uses `RemoteRuntime` to stream through the node agent endpoint.
- **Token gating**: exec and port-forward require scoped tokens and RBAC checks; dashboard tokens are short-lived session tokens minted by the controller (`AE_APISHIM_SESSION_SECRET`).
- **CLI behavior**: `ae exec` / `ae shell` default to SPDY and can optionally fall back to WebSocket with `--ws-fallback`.

## Consequences
- apishim must reach node agent endpoints (prefer WireGuard endpoints advertised via `AE_AGENT_ENDPOINT`).
- Remote exec/port-forward reliability depends on overlay health and node agent availability.
- Token scope and pod UID/RV checks are enforced to prevent stale or out-of-scope sessions.

## Action Plan
- Keep apishim’s SPDY/WS handlers aligned with kubectl semantics.
- Ensure node endpoints are populated in state for remote pods and are reachable from the hub.
- Maintain smoke tests for SPDY exec/port-forward and dashboard WS sessions.
