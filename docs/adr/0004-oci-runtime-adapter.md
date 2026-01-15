# ADR 0004 — OCI runtime adapter (Podman) for lower idle footprint

Date: 2025-11-10
Status: Accepted
Owners: runtime/controller

## Context
- Docker’s daemon + containerd can dominate idle control‑plane PSS on long‑lived hosts.
- k1s targets a small footprint on single-node installations.
- Podman executes containers directly via OCI runtimes (runc/crun) without a central daemon.

## Decision
- Provide a Podman/OCI runtime adapter (`AE_RUNTIME_BACKEND=podman|oci`) alongside the Docker backend.
- Keep Docker as a fallback for environments where Podman is unavailable.

## Options Considered
1) **Docker only**: simplest but higher idle footprint on many hosts; rejected.
2) **Podman only**: smaller footprint but reduces compatibility; rejected.
3) **Dual backends (chosen)**: best balance between footprint and compatibility.

## Consequences
- Runtime abstraction must remain stable across both backends (labels, ports, volumes, logs).
- Benchmarks and docs must account for backend differences (rootless vs rootful, cgroup metrics).
- Users can choose the runtime via environment with minimal behavior changes.

## Action Plan
1) Keep backend selection explicit via `AE_RUNTIME_BACKEND`.
2) Maintain feature parity in runtime adapters (ensure/exec/logs/volumes).
3) Track footprint deltas via the benchmarks toolkit.
