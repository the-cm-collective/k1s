# k1s Memory Profiling (General)

This guide shows how to profile k1s control‑plane/system memory and separate it from app memory using the lightweight toolkit in `scripts/bench/`.

## Prerequisites
- k1s controller running locally: `python -m ae.controller --loop`
- Optional: Podman or Docker installed for container‑level cgroup metrics. Without a container CLI, the snapshotter still reports process PSS totals.

## Quick Start
- Take an idle snapshot and aggregate:
```
make bench-mem-k1s LABEL=idle DURATION=30
make bench-mem-agg LABEL=idle
```
- Scale a single app and compare (uses the echo example):
```
make bench-mem-matrix-k1s LABEL_SUITE=baseline APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30
make bench-mem-combine GLOB='snapshots/*/*'
make bench-mem-plot CSV=combined/combined.csv OUTDIR=charts
```

## Rollout (During vs Post)
Capture snapshots during a rolling update and after convergence:
```
make bench-mem-rollout-k1s LABEL_SUITE=baseline-roll APP=specs/examples/echo.yaml REPLICAS=5 DURATION=30
```

## Outputs
- `snapshots/<label>/<timestamp>/raw/` – ps/free/vmstat, smaps_rollup, docker inspect, per‑container cgroup CSV
- `summary.json` / `summary.csv` – rollup per snapshot
- `combined/combined.{json,csv}` – merged across snapshots (via `mem_combine.py`)
- `charts/*.png` – control‑plane PSS, system cgroups, per‑pod overhead

## Tips
- Keep ingress on/off consistent when comparing runs.
- Warm up for ~2 minutes before heavy scenarios.
- Set `SKIP_GUARDS=1` to bypass preflight checks in CI.

## Runbook (Quick Reference)
- Environment: `PYTHONPATH=src`, `AE_RUNTIME_BACKEND=podman` (or `docker`), `AE_ALLOW_PLAINTEXT_SECRETS=1` for demos.
- Terminal A: `python -m ae.controller --loop --specs specs/ --watch` (bench scripts will auto-start if missing; logs `/tmp/k1s_ctrl_bench.log`).
- Terminal B:
  - `make bench-mem-e2e-k1s LABEL_SUITE=report-YYYYMMDD APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5`
  - `python scripts/bench/mem_combine.py snapshots/*/* && python scripts/bench/plot_overhead.py combined/combined.csv charts && python docs/build_docs.py`
- Podman preflight: `podman info` must succeed; see “Runbook: Successful Benchmark Runs” in memory.md for remedies if not.
