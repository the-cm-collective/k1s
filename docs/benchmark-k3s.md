# Benchmarking k1s vs k3s (In‑Depth)

This guide walks through building an apples‑to‑apples memory benchmark between k1s and k3s.

## Goals & Scope
- Compare control‑plane/system overhead and per‑pod overhead across common scenarios.
- Use the same demo app and replica counts.
- Keep ingress settings aligned (Traefik on k3s, Caddy on k1s) or disable both.

## Prerequisites
- k3d installed (to run k3s locally): https://k3d.io
- kubectl in PATH
- For k1s runs: `python -m ae.controller --loop` in another terminal
- Optional: Docker installed (for container cgroup memory)

## Bring up k3s (k3d)
```
make bench-k3s-up K3S_NAME=bench
```
This exposes ports 80/443 for Traefik; the provided Ingress uses host `echo.localtest.me`.

## Run Scenarios (k3s)
- Idle + scale‑out matrix:
```
make bench-mem-matrix-k3s LABEL_SUITE=baseline MANIFEST=specs/examples/k3s-echo.yaml REPLICAS=1,5,10 DURATION=30
```
- Rollout (during vs post):
```
make bench-mem-rollout-k3s LABEL_SUITE=baseline-roll DEPLOY=echo NS=default REPLICAS=5 DURATION=30
```

## Run Scenarios (k1s)
- Matrix and rollout (in a separate terminal, controller running):
```
make bench-mem-matrix-k1s LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30
make bench-mem-rollout-k1s LABEL_SUITE=baseline-roll APP=specs/examples/echo.yaml REPLICAS=5 DURATION=30
```

## Combine and Plot
```
make bench-mem-combine GLOB='snapshots/*/*'
make bench-mem-plot CSV=combined/combined.csv OUTDIR=charts
```
Charts include control‑plane PSS, system cgroup memory, and approximate per‑pod overhead.

## Caveats
- k3s via k3d enables Traefik; be consistent with ingress between systems.
- Without Docker, container cgroup metrics are skipped; process PSS totals remain available.
- You can bypass preflight checks with `SKIP_GUARDS=1` in CI.

## Teardown
```
make bench-k3s-down K3S_NAME=bench
```

