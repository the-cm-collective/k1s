# Changelog

## Unreleased (2025-12-15)

### Added
- **Node agent + remote runtime:** new `ae-node` HTTP agent and `RemoteRuntime` adapter so the controller can delegate workload lifecycle and log/exec calls to remote nodes; agent sends heartbeats to the controller’s agent API with labels/taints, optional pod CIDR/WireGuard metadata.
- **Controller multi-node plumbing:** scheduler distributes replicas across Ready nodes with nodeSelector/tolerations and storage pinning; state store now records nodes, heartbeats, and storage bindings; controller auto-registers the local node for single-node runs.
- **Service VIP dataplane:** Service controller and Docker/overlay providers allocate ClusterIPs, run per-Service HAProxy sidecars, and program endpoints from health + runtime state (skips loopback, deduplicates targets). Optional overlay provider targets WireGuard-backed networks.
- **Pod networking helpers:** Pod CIDR allocator (env-gated) and node-side bridge/WireGuard helper to plumb pod networks in lab/overlay scenarios.
- **CLI/HTTP API:** `ae services` and `ae nodes` subcommands; status/history JSON/watch tweaks; HTTP API exposes `/nodes` and richer status for dashboards. New console script `ae-node` registered in `pyproject.toml`.
- **Multinode lab & CI assets:** QEMU/libvirt lab scripts (`ops/ci/multinode-qemu.sh`, `ops/dev/multinode-lab.sh`), default test key under `ops/ci/keys/`, overlay-enabled smoke option, and sample `specs/examples/echo-multinode.yaml`. New docs `MULTINODE-TEST.md`, `docs/multinode-lab.md`, and site rebuild covering the workflow.
- **Test coverage:** Integration suites for agent flow and service VIP routing; unit suites for scheduler, agent API, pod CIDR allocator, docker provider, node state, and reconciler updates.

### Changed
- Docker runtime now prefers host-published endpoints using `AE_NODE_ADVERTISE_IP` when containers run on remote nodes; podman runtime gains parity fixes. Service endpoints are deduplicated to avoid SQLite UNIQUE violations when multiple replicas share targets.
- Reconciler/state tightening: merges file/DB manifests with hash + mtime heuristics, tracks storage bindings, and cleans service records; observer backends handle stale nodes via grace-period NotReady logic.
- CI/doc tooling: `scripts/update_docs.sh` refreshes the static site; SMOKE.md references the new multi-node smoke; doc HTML regenerated.

### Fixed
- QEMU CI script waits for cloud-init, retries repo mounts, installs pip before invoking ae binaries, and optionally runs a full smoke (apply → VIP curl → kill worker → reschedule → curl). Helper adds host key support for remote SSH kills and expands overlay disk defaults.

