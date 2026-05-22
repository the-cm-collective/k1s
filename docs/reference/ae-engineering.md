# ae Engineering Documentation Index

This page is the published navigation point for the source-local engineering documentation under `src/ae`.

The detailed docs are intentionally kept beside the code they describe so maintainers can update architecture notes while changing a subsystem. The published site keeps this compact index and points readers to the canonical source paths.

## Canonical Source-Local Docs
- Top-level engineering map: `src/ae/README.md`
- Top-level module reviews: `src/ae/docs/*.md`
- Package summaries: `src/ae/<package>/README.md`
- Package module reviews: `src/ae/<package>/docs/*.md`

## Package Index
| Package | Canonical doc | Responsibilities |
| --- | --- | --- |
| apishim | `src/ae/apishim/README.md` | Kubernetes-compatible API shim, storage adapters, HA authority bridge, and kubectl/Helm compatibility surface. |
| cli | `src/ae/cli/README.md` | Primary `ae` command-line interface for apply/status/logs/exec/export/auth/profile-oriented operations. |
| config | `src/ae/config/README.md` | Configuration reference loading, transport configuration parsing, and shared environment-derived settings. |
| controller | `src/ae/controller/README.md` | Core control plane: manifest loading, reconcile loop, state stores, scheduling, HA authority, and workload controllers. |
| gateway | `src/ae/gateway/README.md` | Site gateway process for NATS-mediated work delivery, result spooling, and edge/core transport bridging. |
| ha | `src/ae/ha/README.md` | High-availability support code for authority operations, fencing decisions, dashboard probes, and operational helpers. |
| ingress | `src/ae/ingress/README.md` | Ingress rendering and synchronization for Caddy, Envoy, edge-local/core-proxy routing, TLS, and tunnel helpers. |
| k8s | `src/ae/k8s/README.md` | Kubernetes import/export/check layer that translates between k1s manifests and Kubernetes-style resources. |
| kctl | `src/ae/kctl/README.md` | Small kubectl-like wrapper around k1s native commands and resource references. |
| network | `src/ae/network/README.md` | Service VIP allocation, pod CIDR allocation, bridge/iptables/overlay network providers, and overlay health checks. |
| node | `src/ae/node/README.md` | Node agent HTTP server, runtime proxying, heartbeat loop, local network helper, and Rosenpass/WireGuard support. |
| observability | `src/ae/observability/README.md` | Controller HTTP API, dashboard/static assets, Prometheus metrics snapshotting, and logging setup. |
| resources | `src/ae/resources/README.md` | Package data loader plus bundled SQL, dashboard/docs HTML, and ingress templates. |
| runtime | `src/ae/runtime/README.md` | Runtime adapter interface and Docker, Podman, direct-containerd, strict CRI, remote, and stub implementations. |
| secrets | `src/ae/secrets/README.md` | Secret reference resolution and SOPS/age integration for environment and file projections. |
| security | `src/ae/security/README.md` | Local CA issuance/revocation helpers and signed token issue/verify helpers. |
| storage | `src/ae/storage/README.md` | Storage classes, PVC/PV authority, NetFS/CSI integration, node volume management, and storage state adapters. |
| transport | `src/ae/transport/README.md` | NATS/JetStream client, subject naming, controller ingress, telemetry, outbox, and route-bundle publishing. |
| worker_stub | `src/ae/worker_stub/README.md` | Development/test worker process for exercising transport and work delivery paths. |

## Maintenance Rules
- Update the folder README when subsystem ownership, public behavior, or module boundaries change.
- Update the per-module doc when a module gains/removes entrypoints, environment variables, persistence, subprocess, network, or compatibility behavior.
- Keep generated protobufs, vendored browser assets, SQL files, and static templates summarized unless handwritten code changes their contract.
- Use `make docs-verify` after changing this published index or related docs-site mappings.
