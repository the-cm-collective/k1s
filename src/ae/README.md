# ae Engineering Map

- Source folder: `src/ae`
- Last reviewed: 2026-05-13

## System Summary
`ae` is the k1s application engine package. It contains the controller, runtime adapters, Kubernetes-compatible API shim, node agent, ingress/network/storage layers, NATS transport, observability API/dashboard, CLIs, and bundled runtime resources.

## Primary Data Flow
- Manifests enter through `ae.cli`, `ae.kctl`, watched files, or the apishim server.
- `ae.controller` normalizes desired state, schedules placements, calls runtime adapters directly or through node agents, records state, and emits events/metrics.
- `ae.runtime`, `ae.node`, `ae.network`, `ae.storage`, and `ae.ingress` realize containers, connectivity, volumes, and public routes.
- `ae.transport`, `ae.gateway`, and `ae.ha` provide multi-node, edge, and HA authority/transport behavior.
- `ae.observability` exposes controller status, logs, metrics, dashboards, and schema/doc surfaces.

## Package Map
| Package | Folder doc | Responsibilities |
| --- | --- | --- |
| apishim | [apishim/README.md](apishim/README.md) | Kubernetes-compatible API shim, storage adapters, HA authority bridge, and kubectl/Helm compatibility surface. |
| cli | [cli/README.md](cli/README.md) | Primary `ae` command-line interface for apply/status/logs/exec/export/auth/profile-oriented operations. |
| config | [config/README.md](config/README.md) | Configuration reference loading, transport configuration parsing, and shared environment-derived settings. |
| controller | [controller/README.md](controller/README.md) | Core control plane: manifest loading, reconcile loop, state stores, scheduling, HA authority, and workload controllers. |
| gateway | [gateway/README.md](gateway/README.md) | Site gateway process for NATS-mediated work delivery, result spooling, and edge/core transport bridging. |
| ha | [ha/README.md](ha/README.md) | High-availability support code for authority operations, fencing decisions, dashboard probes, and operational helpers. |
| ingress | [ingress/README.md](ingress/README.md) | Ingress rendering and synchronization for Caddy, Envoy, edge-local/core-proxy routing, TLS, and tunnel helpers. |
| k8s | [k8s/README.md](k8s/README.md) | Kubernetes import/export/check layer that translates between k1s manifests and Kubernetes-style resources. |
| kctl | [kctl/README.md](kctl/README.md) | Small kubectl-like wrapper around k1s native commands and resource references. |
| network | [network/README.md](network/README.md) | Service VIP allocation, pod CIDR allocation, bridge/iptables/overlay network providers, and overlay health checks. |
| node | [node/README.md](node/README.md) | Node agent HTTP server, runtime proxying, heartbeat loop, local network helper, and Rosenpass/WireGuard support. |
| observability | [observability/README.md](observability/README.md) | Controller HTTP API, dashboard/static assets, Prometheus metrics snapshotting, and logging setup. |
| resources | [resources/README.md](resources/README.md) | Package data loader plus bundled SQL, dashboard/docs HTML, and ingress templates. |
| runtime | [runtime/README.md](runtime/README.md) | Runtime adapter interface and Docker, Podman, direct-containerd, strict CRI, remote, and stub implementations. |
| secrets | [secrets/README.md](secrets/README.md) | Secret reference resolution and SOPS/age integration for environment and file projections. |
| security | [security/README.md](security/README.md) | Local CA issuance/revocation helpers and signed token issue/verify helpers. |
| storage | [storage/README.md](storage/README.md) | Storage classes, PVC/PV authority, NetFS/CSI integration, node volume management, and storage state adapters. |
| transport | [transport/README.md](transport/README.md) | NATS/JetStream client, subject naming, controller ingress, telemetry, outbox, and route-bundle publishing. |
| worker_stub | [worker_stub/README.md](worker_stub/README.md) | Development/test worker process for exercising transport and work delivery paths. |

## Top-Level Modules
| File | Detailed doc | Functionality |
| --- | --- | --- |
| __main__.py | [docs/main.md](docs/main.md) | Top-level package launcher that delegates to the controller daemon entrypoint. |
| _utc.py | [docs/utc.md](docs/utc.md) | Small UTC datetime compatibility helper used where timezone-aware timestamps are needed. |
| accelerators.py | [docs/accelerators.md](docs/accelerators.md) | Normalizes accelerator/GPU inventory and exposes execution labels/capabilities used by scheduling and inference... |

## Documentation Policy
- Folder `README.md` files provide subsystem summaries and module maps.
- Per-module files under `docs/` summarize symbols, side effects, tests, and maintenance markers from static review.
- Generated protobufs, vendored browser assets, SQL resources, and static templates are summarized at folder level unless handwritten code owns behavior.
