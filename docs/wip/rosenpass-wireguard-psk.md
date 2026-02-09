# Rosenpass PSK for WireGuard (PQ Resistance)

## Summary
Add an optional Rosenpass-backed PSK workflow for WireGuard tunnels to improve
post-quantum resistance. This doc captures current state, a minimal ops-only
approach, and a full integration plan for k1s.

## Current State (k1s)
- Node agent can apply a full WireGuard config from `AE_WG_CONFIG` via
  `wg syncconf` / `wg-quick` (best-effort). There is no PSK generation or
  rotation logic in k1s.
- Overlay routing is external to k1s; peers, routes, and AllowedIPs are all
  expected to be provided in the WireGuard config.

## Rosenpass Overview
- Rosenpass runs alongside WireGuard and can produce a PSK for a peer.
- It can write the PSK to a file (`outfile`) or push it directly into a
  WireGuard peer (`wireguard <dev> <peer> ...`).
- WireGuard supports an optional PSK mixed into the handshake for post-quantum
  resistance.

References:
- Rosenpass manual (wireguard/output options): https://rosenpass.eu/manual/
- WireGuard protocol (PSK for post-quantum resistance): https://www.wireguard.com/protocol/

## Integration Options

### Option A — Ops-Only (No k1s Code Changes)
**Goal:** Use Rosenpass to rotate PSKs and keep k1s unaware of the details.

Steps (per node):
1. Install and configure Rosenpass.
2. Configure Rosenpass peers with `wireguard <dev> <peer>` so Rosenpass updates
   the WireGuard peer PSK directly.
3. Start `ae.node` with `AE_WG_CONFIG` pointing at a static base config for the
   interface (addresses, AllowedIPs, listen port, peer pubkeys, endpoints).
4. Let Rosenpass handle PSK rotation out-of-band.

Notes:
- This is the least invasive path.
- k1s does not need to reload WireGuard as long as Rosenpass writes PSKs directly
  into the peer (`wg set` equivalent).
- Rotation cadence is controlled by Rosenpass; verify rekey with `wg show`.

### Option B — k1s-Assisted (PSK File Watch + Apply)
**Goal:** k1s integrates with Rosenpass output files to keep PSKs in sync.

Design:
- Rosenpass writes PSKs to files (`outfile`).
- Node agent watches those files and applies updates to WireGuard peers.

Work items:
- Add `AE_WG_PSK_DIR` and a small watcher in `ae.node` to apply PSK updates to
  `wg set` for each peer.
- Define a mapping format for `peer_pubkey -> psk_file` (simple JSON or INI).
- Add metrics/events for rekey success/failure.

### Option C — Full Integration (Managed Rosenpass)
**Goal:** k1s provisions, starts, and supervises Rosenpass alongside the node
agent.

Work items:
- Add a lightweight `ae.rosenpass` runner (systemd service or subprocess).
- Generate keys (or import from disk) and manage `rosenpass` peer definitions.
- Provide a `k1s` config format for peers + endpoints, with NAT-friendly defaults
  (initiator/receiver roles).
- Implement health checks and expose tunnel PSK age and handshake status.

## Security Considerations
- PSK files are sensitive; enforce mode `0600` and root ownership.
- Avoid logging PSK material.
- Require explicit opt-in; default to disabled.

## Operational Notes
- For NATed nodes, Rosenpass (like WireGuard) should be configured so the node
  behind NAT initiates outbound handshakes to a publicly reachable peer.
- Ensure AllowedIPs include pod CIDRs and any control-plane addresses that must
  traverse the tunnel.

## Open Questions
- Do we want to manage Rosenpass keys in k1s, or require external provisioning?
- What is the minimal mapping format for `peer -> psk`?
- Should we support multiple tunnels per node (one per site) or a single mesh?
