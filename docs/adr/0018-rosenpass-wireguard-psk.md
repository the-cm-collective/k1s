# ADR 0018 — Managed Rosenpass PSK for WireGuard Overlay

Date: 2026-02-09
Status: Accepted
Implemented: 2026-02-10
Owners: runtime/controller

## Context
- WireGuard supports a preshared key (PSK) for post-quantum resistance.
- Manual PSK management is error-prone across multi-site deployments.
- The overlay already depends on node agents to apply WireGuard config.

## Decision
- **Enable Rosenpass support in the node agent** behind `AE_ROSENPASS_ENABLED=1`.
- **Configuration sources**: allow `AE_ROSENPASS_CONFIG=controller` for controller-managed peers or a static config file path.
- **Key storage**: store WireGuard and Rosenpass keys under `AE_ROSENPASS_DIR` (default `/var/lib/ae/rosenpass`).
- **Overlay integration**: generate/apply WireGuard configs with Rosenpass PSK and refresh peers on a timer when controller-managed.
- **Observability**: write Rosenpass status and optional WireGuard debug configs under the Rosenpass dir.

## Consequences
- Node agents require elevated privileges to apply WireGuard config and run Rosenpass.
- Key material must be protected with strict file permissions.
- Deployments must ensure Rosenpass binaries are present on nodes.

## Action Plan
- Keep node runbooks aligned with `AE_ROSENPASS_*` envs and overlay labels.
- Validate Rosenpass status and handshake health via `/system` and `wg show`.
- Document single-host hub+edge patterns with distinct Rosenpass directories and interfaces.
