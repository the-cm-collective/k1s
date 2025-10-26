# Memory Overhead Benchmarks

This guide describes how to profile and compare memory overhead for k1s versus k3s on a single host.

Goals
- Separate app memory from control-plane/system overhead.
- Produce repeatable snapshots with raw artifacts and lightweight summaries.
- Keep tooling simple: bash + Python, no invasive agents.

What we measure
- Processes (PSS/RSS/USS) via `/proc/<pid>/smaps_rollup` and `/proc/<pid>/status`:
  - k1s: `python -m ae.controller`, ingress proxy (Caddy), Docker/container runtime.
  - k3s: `k3s`, `containerd`, `coredns`, ingress controller (Traefik if enabled).
- Containers (cgroups): `memory.current` for each container cgroup via the container PID.

Outputs
- `snapshots/<label>/<timestamp>/raw/*`: raw text and JSON (ps, free, vmstat, smaps_rollup, docker inspect, per-container memory CSV).
- `summary.json`: totals and breakdowns for processes and containers.
- `summary.csv`: one-line rollup: total PSS, control-plane PSS, app/system cgroup bytes.

Quick start
1) Take a snapshot (k1s):
```
make bench-mem-k1s LABEL=idle DURATION=30
```

2) Aggregate latest snapshot for a label:
```
make bench-mem-agg LABEL=idle
```

3) Compare scenarios by repeating with different labels:
```
make bench-mem-k1s LABEL=pods-1 DURATION=30
make bench-mem-agg LABEL=pods-1

make bench-mem-k1s LABEL=pods-5 DURATION=30
make bench-mem-agg LABEL=pods-5
```

Scenarios (suggested)
- `idle`: controller running, no apps.
- `pods-1`: one app with 1 replica.
- `pods-5`: one app with 5 replicas.
- `rollout-5`: rolling update across 5 replicas (start snapshot while rollout is in progress).
- `logs-5`: 5 replicas with the dashboard logs panel open.

Tips for consistency
- Use the same host and OS between runs; minimize background services.
- Keep ingress enabled or disabled across both systems for apples-to-apples.
- Allow a 2-minute warm-up before snapshotting busy scenarios.

Interpreting results
- Process PSS approximates unique+fair-share memory for control-plane processes.
- Container `memory.current` shows cgroup-resident memory per container (includes cache).
- System overhead (cgroup) = sum of non-app container `memory.current`.
- Per-pod overhead (rough) = system_overhead / pod_count (for pod_count > 0).

Limitations
- cgroup paths vary across distros; the snapshotter falls back gracefully.
- If Docker is unavailable, container-level stats are skipped; process PSS is still reported.
- USS is approximated from `Private_*` in `smaps_rollup`.

Next steps
- Add k3s parity targets (k3d or native k3s) to automate bringing up comparable workloads.
- Export combined CSV across labels for quick charting.

