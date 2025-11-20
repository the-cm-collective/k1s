# Memory Overhead Benchmarks

This guide describes how to profile and compare memory overhead for k1s versus k3s on a single host.

Goals
- Separate app memory from control-plane/system overhead.
- Produce repeatable snapshots with raw artifacts and lightweight summaries.
- Keep tooling simple: bash + Python, no invasive agents.

What we measure
- Processes (PSS/RSS/USS) via `/proc/<pid>/smaps_rollup` and `/proc/<pid>/status`:
  - k1s: `python -m ae.controller`, ingress proxy (Caddy), Docker/Podman runtime processes.
  - k3s: `k3s`, `containerd`, `coredns`, ingress controller (Traefik if enabled).
- Containers (cgroups): `memory.current` for each container cgroup via the container PID (split into app vs non‑app based on labels/names).
- Host services (cgroups): sum of `memory.current` for leaf cgroups under `system.slice` plus `init.scope` (cgroup v2), excluding `user.slice` entirely. This avoids parent+child double counting.

Outputs
- `snapshots/<label>/<timestamp>/raw/*`: raw text and JSON (ps, free, vmstat, smaps_rollup, docker/podman inspect, per‑container memory CSV).
- `summary.json`: totals and breakdowns for processes, container cgroups, host system cgroups, and MemAvailable before/after from `free -b`.
- `summary.csv`: one‑line rollup: total PSS, control‑plane PSS, app/system container bytes, host system cgroup bytes.

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
- If Podman/Docker is not installed, snapshots still run but container-level cgroup metrics are skipped (process PSS totals are still reported).
 - CI or advanced users can bypass safety checks by setting `SKIP_GUARDS=1` in the environment.

Interpreting results
- Process PSS approximates unique+fair‑share memory for control‑plane processes.
- Container `memory.current` shows cgroup‑resident memory per container (includes cache); we split into app vs non‑app containers.
- Host system cgroups reflects only OS services under `system.slice` and `init.scope` (excludes user/container trees), summed over leaf cgroups to avoid double counting.
- Per‑pod app footprint (rough) ≈ app_container_bytes / pod_count.
- MemAvailable Δ validates totals at the kernel level: `delta = MemAvailable(before) − MemAvailable(after)` for the snapshot window. Small negatives are clamped to 0.

Limitations
- cgroup paths vary across distros; the snapshotter falls back gracefully.
- If Docker is unavailable, container-level stats are skipped; process PSS is still reported.
- USS is approximated from `Private_*` in `smaps_rollup`.

Caveats
- k3s (via k3d) enables Traefik by default; the provided Ingress uses class `traefik`. Keep ingress turned on in k1s for apples-to-apples, or disable both.
- The echo image `ealen/echo-server:0.7.0` serves `/` on port 80 and has a lightweight memory footprint suitable for baseline comparisons.
- If you prefer your demo image, push it to a registry and update `specs/examples/k3s-echo.yaml` accordingly.

Automate a small matrix (k1s)
- Run idle + scale-out snapshots in one go (requires controller running and echo example available):
```
make bench-mem-matrix-k1s LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30
```
- Combine all summaries into one CSV/JSON for charting:
```
make bench-mem-combine GLOB='snapshots/*/*'
```

End-to-end (k1s one-liner)
```
make bench-mem-e2e-k1s LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
```
This runs matrix + rollout, combines all summaries, and writes charts/.

End-to-end (k1nd one-liner; compose must be up)
```
make bench-mem-e2e-k1nd LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
```
- Brings up the k1s-in-Docker dev stack (`labs-aio` compose) and then runs the same matrix + rollout sequence against that environment.
- Uses Docker for host-side preflights and container cgroup metrics; ensure the current user can run `docker ps`.
- Leave it running for repeated runs, or tear down with `make labs-aio-down` when done.

End-to-end (k1nd with teardown)
```
make bench-mem-e2e-k1nd-down LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
```
- Runs the same k1nd sequence and then executes `make labs-aio-down` to stop the compose stack.

End-to-end (all suites, automated prep)
```
make bench-mem-e2e-all DURATION=30 REPLICAS=1,5,10 ROLL_REPLICAS=5
```
- Stops the dev/labs compose stacks, clones `specs/` into `state/bench-env/`, prunes every App manifest except the requested one (defaults to `specs/examples/echo.yaml`), and launches a dedicated controller against that sandbox.
- Runs k1s rootful (snapshots elevated), k1s rootless, and k1nd in one shot, then backfills OCI labels, recombines, regenerates charts, and rebuilds docs. Only the snapshot helpers call `sudo`; the top-level `make` runs as your user.
- Defaults: `OCI_RUNTIME=crun`, `AE_ENGINE_STRICT=1`, `AE_ALLOW_PLAINTEXT_SECRETS=1`, `PRUNE_OLD=1`. Override `LABEL_ROOTFUL`, `LABEL_ROOTLESS`, or `LABEL_K1ND` to tag the suites differently.

Minimal smoke (rootful only)
```
make bench-mem-e2e-minimal DURATION=10 REPLICAS=1 ROLL_REPLICAS=2
```
- Uses the same prep/teardown scripts but only runs the k1s rootful matrix (single replica) plus a tiny rollout. Handy while iterating on manifests or Podman tuning before committing to the full matrix.

Backfill OCI runtime into past snapshots
```
make bench-mem-backfill-oci LABEL=r20251110+podman+rootless+cg2* REBUILD_DOCS=1
```
- Detects the OCI runtime per-snapshot by reading `raw/podman_inspect.json` (`.OCIRuntime`) or `raw/docker_inspect.json` (`HostConfig.Runtime`), then writes it to `meta.json` and injects `+<oci>+` into the label (e.g., `+crun+`) without renaming folders.
- Then recombines and regenerates charts. Add `OCI=runc` to force an override if needed. Use `GLOB='snapshots/r2025*/*'` to target explicit paths.

Backfill just the latest label
```
make bench-mem-backfill-oci-latest REBUILD_DOCS=1
```
- Picks the most recently modified label directory under `snapshots/` and runs the same backfill → recombine → charts (and docs) sequence. Pass `OCI=crun` to override detection if desired.

Automate a small matrix (k3s via k3d)
- Create cluster and expose ports 80/443 for Traefik:
```
make bench-k3s-up K3S_NAME=bench
```
- Run idle + scale-out snapshots using a simple echo Deployment/Service/Ingress:
```
make bench-mem-matrix-k3s LABEL_SUITE=baseline MANIFEST=specs/examples/k3s-echo.yaml REPLICAS=1,5,10 DURATION=30
```
- Combine across runs:
```
make bench-mem-combine GLOB='snapshots/*/*'
```
- Tear down cluster when finished:
```
make bench-k3s-down K3S_NAME=bench
```

End-to-end (k3s one-liner; cluster must be up)
```
make bench-mem-e2e-k3s LABEL_SUITE=baseline MANIFEST=specs/examples/k3s-echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5
```

## Runbook: Successful Benchmark Runs

- Environment
  - `PYTHONPATH=src` in the shell running commands.
  - Runtime backend: `AE_RUNTIME_BACKEND=podman` (preferred) or `docker`.
  - Secrets (demo-friendly): set `AE_ALLOW_PLAINTEXT_SECRETS=1` unless SOPS is configured (or run `init_demo.sh --with-secrets-env`).

- Controller
  - Keep the controller running for the full duration:
    - Terminal A: `python -m ae.controller --loop --specs specs/ --watch`
  - The bench scripts auto-start it if missing and log to `/tmp/k1s_ctrl_bench.log`, but a dedicated terminal is more predictable.

- Images
  - Podman: `podman build -t localhost/demo-blue:latest samples/servers/blue` and `localhost/demo-green:latest samples/servers/green`.
  - Docker: `docker build -t demo-blue:latest samples/servers/blue` and `demo-green:latest`.
  - Verify: `podman images | rg 'demo-(blue|green)'` or `docker images | rg 'demo-(blue|green)'`.

- Commands (Podman-first)
  - Terminal B:
    - `export PYTHONPATH=src AE_RUNTIME_BACKEND=podman AE_ALLOW_PLAINTEXT_SECRETS=1` (or start with `./scripts/init_demo.sh --with-secrets-env`)
    - `make bench-mem-e2e-k1s LABEL_SUITE=report-YYYYMMDD APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5`
    - `python scripts/bench/mem_combine.py snapshots/*/*`
    - `python scripts/bench/plot_overhead.py combined/combined.csv charts`
    - `python docs/build_docs.py`

- Docker fallback
  - `export AE_RUNTIME_BACKEND=docker` and re-run the same commands.
  - Ensure the current user can run `docker ps` without sudo (group membership or rootless config).

- Preflights and guardrails
  - Podman readiness: `podman info` must succeed. If not, try:
    - `systemctl --user reset-failed; systemctl --user daemon-reload`
    - `systemctl --user start podman.socket || systemctl --user start podman.service`
    - `loginctl enable-linger "$USER"`; open a new login shell
    - `podman system migrate`
  - Secrets guard: if `AE_ALLOW_PLAINTEXT_SECRETS!=1` and `sops` is missing or cannot decrypt, the scripts will fail fast with guidance.
  - Logs: controller auto-start logs at `/tmp/k1s_ctrl_bench.log`.

- After run
- Combined table: `combined/combined.csv` and `combined/combined.json`.
  - Charts: `charts/control_plane_pss.png`, `charts/system_cgroups.png`, `charts/per_pod_overhead.png`.
  - Docs page `testing-memory-k1s.html` auto-appends a “Latest Benchmarks (Auto)” section from `combined/combined.csv`.

## Automated Prep/Teardown

The helper scripts `scripts/bench/bench_env_prep.sh` and `scripts/bench/bench_env_teardown.sh` power both `make bench-mem-e2e-all` and `make bench-mem-e2e-minimal`:

- Prep copies `specs/` into `state/bench-env/specs/`, removes every App manifest except the allowlist, and leaves shared configs/secrets intact so relative `configRefs` still work.
- A dedicated controller (log + pid under `state/bench-env/`) watches only that sandbox, so background demos or labs can’t steal ports or replicas.
- The scripts set `AE_SPECS_DIR`, `AE_STATE_DB`, `AE_CADDY_DIR`, and the primary manifest/app name in an env file; each benchmark stage sources it before calling `run_matrix.sh`/`run_rollout_k1s.sh`.
- Teardown stops the sandbox controller and deletes `state/bench-env/` unless you set `BENCH_KEEP_ENV=1` to inspect artifacts.
- If rootful Podman containers with the `ae.app` label are still running, prep lists them and asks whether to remove them (set `BENCH_AUTOCLEAN_PODMAN=1` to auto-approve in CI, or `=0` to refuse). This prevents stray demos from holding ports like 18080.
- Ingress writes are disabled by default for benches (`BENCH_DISABLE_INGRESS=1`). Set `BENCH_DISABLE_INGRESS=0` if a scenario actually needs Caddy updates; otherwise the sandbox exports `AE_DISABLE_INGRESS=1` and skips writing to `ops/dev/caddy/*`, avoiding permission chatter.

## All-in-one Run (sudo once)

Run k1s rootful, k1s rootless, and k1nd in one go and rebuild docs:

```
sudo make bench-mem-e2e-all DURATION=30 REPLICAS=1,5,10 ROLL_REPLICAS=5
```

Defaults: `OCI_RUNTIME=crun`, `AE_ENGINE_STRICT=1`, `AE_ALLOW_PLAINTEXT_SECRETS=1`, `PRUNE_OLD=1`.
