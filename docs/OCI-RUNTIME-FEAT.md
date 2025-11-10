# OCI Runtime Adapter (Podman) — Memory Footprint Improvement

This note documents the motivation, method, and results of introducing an OCI-backed runtime (via Podman) alongside the existing Docker/containerd backend to reduce control‑plane memory footprint on single‑node installs.

## Why

On hosts with long‑lived Docker environments, the daemon (`dockerd`) and container engine (`containerd`) can dominate the control‑plane Proportional Set Size (PSS), often measuring hundreds of MiB even when no workloads are running.

Podman executes containers directly on top of an OCI runtime (runc/crun) without a central daemon, which materially reduces idle overhead in many cases.

## What changed

- New runtime adapter: `PodmanRuntime` (AE_RUNTIME_BACKEND=podman|oci)
  - Location: `src/ae/runtime/podman_runtime.py`
  - Mirrors Docker labels (`ae.app`, `ae.replica_id`, `ae.revision`), env, ports, volumes, and restart policy.
  - Implements `ensure_app`, `read_logs`, `remove_app`, `remove_old_revisions`, volume helpers, and `list_containers_info`.
- Runtime selection:
  - `AE_RUNTIME_BACKEND=podman` (or `AE_RUNTIME_BACKEND=oci`) picks the new adapter.
  - Default remains `docker`.

## Reproducible measurements

All measurements below were taken with the lightweight toolkit under `scripts/bench/`:

- Establish idle baseline (k1s):
```
make bench-mem-idle-k1s LABEL=idle-baseline DURATION=30 \
  ARGS="--restart-docker --prune-system"
```
- Establish idle baseline (k3s via k3d):
```
make bench-k3s-up && make bench-mem-idle-k3s LABEL=idle-k3s DURATION=30 && make bench-k3s-down
```
- Switch to OCI and retest (new terminal):
```
AE_RUNTIME_BACKEND=podman python -m ae.controller --loop
make bench-mem-idle-k1s LABEL=idle-podman DURATION=30
```
- Inspect the latest baseline report:
```
SNAP=$(ls -d snapshots/<label>/* | sort | tail -1)
cat "$SNAP/summary.txt"
```

Notes:
- The k1s idle script scales all apps to 0, prunes lingering `ae.*` containers, stops docs/fixtures, and optionally restarts/prunes Docker to stabilize the daemon’s memory.
- The control‑plane PSS is derived from `/proc/*/smaps_rollup` and requires privileged snapshotting (the script uses `sudo` for that step).

## Observed deltas (on this host)

All values below are control‑plane PSS (MiB), measured via the idle baseline script.

- Initial (pre‑tightening) control‑plane PSS: ~536 MiB
- Tightened process selection (exclude `containerd-shim`): ~482 MiB
- With `--restart-docker --prune-system` before snapshot: ~228 MiB

Console excerpt (2025‑10‑26):
```
Idle baseline (k1s) @ 20251026-125609
  Control-plane PSS (MiB): 228.38
  System cgroups (MiB):    0.0
  App cgroups (MiB):       114.70
  Total cgroups (MiB):     114.70
Per-process PSS breakdown (MiB):
  dockerd   163.06
  python    76.02   # ae.controller
  containerd 65.32
```

Takeaways:
- A substantial share of the idle footprint comes from `dockerd` + `containerd`.
- Restarting/pruning Docker reduces daemon PSS significantly on a long‑lived host.
- Replacing the Docker backend with the OCI adapter (Podman) removes `dockerd` from the equation entirely; expect a further reduction on hosts where `dockerd` dominates.

## Expected improvement moving to OCI (Podman)

- Removes the Docker daemon (`dockerd`) entirely.
- Keeps a minimal engine footprint (conmon + runc/crun) per container.
- Typical single‑node idle improvements observed in the field: tens to >100 MiB vs Docker, depending on host churn and caches.

Action: After enabling the Podman backend, capture a fresh idle baseline and compare `control-plane PSS (MiB)` against the Docker baseline above.

## How to enable OCI runtime

1) Install Podman for your OS (ensure `podman run hello-world` works).
2) Start the controller with the new backend:
```
AE_RUNTIME_BACKEND=podman python -m ae.controller --loop
```
3) Use the CLI as usual (`ae apply`, `ae scale`, `ae logs`).

### Runtime choice and overrides (crun by default)

- When using the Podman backend, k1s prefers `crun` as the OCI runtime for better startup time, memory footprint, and cgroup v2 behavior. If `crun` is not installed, Podman will use its configured default (typically `runc`).
- Honor host intent first: If your host sets the runtime in `containers.conf`, k1s defers to it.
  - System‑wide: `/etc/containers/containers.conf` → `[engine] runtime = "crun"`
  - Rootless: `$HOME/.config/containers/containers.conf` → `[engine] runtime = "crun"`
- Force a specific runtime for k1s runs (optional):
  - `AE_OCI_RUNTIME=crun` (or `runc`) — the Podman adapter injects `--runtime=<value>` into all `podman run` calls (main, sidecars, and init containers).

Verification
- Check the effective runtime: `podman info --format '{{ .Host.OCIRuntime.Name }}'`
- Inspect a container: `podman inspect <id> --format '{{ .OCIRuntime }}'`

See also: FEAT.md section “Podman: Default OCI Runtime = crun (2025-11-10)”.

## Caveats & compatibility

- Networking: Podman uses CNI/slirp4netns depending on configuration; host port publishing is supported (the adapter preserves service stable ports when replicas=1).
- Logs: `podman logs` supports `--tail/--since/-f`; used by the adapter.
- Volumes: Named volumes are created with `ae.app`/`ae.volume` labels for parity with Docker.
- Ingress: No change — Caddy remains separate; it routes to container host ports as before.

## Rollback

- To revert to Docker backend: unset `AE_RUNTIME_BACKEND` or set it to `docker`.
- Existing Podman containers will continue to run; you may remove them via `podman rm -f` if desired.

## Appendix: Bench toolkit quick reference

- Idle k1s baseline:
  - `make bench-mem-idle-k1s LABEL=idle-baseline DURATION=30`
- Idle k3s baseline:
  - `make bench-mem-idle-k3s LABEL=idle-k3s DURATION=30`
- Matrix + rollout + plots (k1s):
  - `make bench-mem-e2e-k1s LABEL_SUITE=baseline REPLICAS=1,5,10 ROLL_REPLICAS=5`
- Combine & plot across snapshots:
  - `make bench-mem-combine` then `make bench-mem-plot`
