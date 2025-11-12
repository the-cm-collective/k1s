# Benchmarks & Tests

Use these guides to profile memory and compare k1s with k3s.

- [k1s Memory Profiling (General)](testing-memory-k1s.html)
- [Benchmarking k1s vs k3s (In‑Depth)](benchmark-k3s.html)

For quick commands and charts, see the end‑to‑end Make targets in each guide.

## Runbook: Quick Start
- Set env: `PYTHONPATH=src`, `AE_RUNTIME_BACKEND=podman` (or `docker`), `AE_ALLOW_PLAINTEXT_SECRETS=1` for demos (or start with `./scripts/init_demo.sh --with-secrets-env`).
- Keep controller running (or let scripts auto‑start): `python -m ae.controller --loop --specs specs/ --watch`.
- Build or ensure images are present (Podman shown):
  - `podman build -t localhost/demo-blue:latest samples/servers/blue`
  - `podman build -t localhost/demo-green:latest samples/servers/green`
- Run end‑to‑end and generate charts:
  - `make bench-mem-e2e-k1s LABEL_SUITE=report-YYYYMMDD APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5`
  - Or against k1nd (controller in Docker): `make bench-mem-e2e-k1nd LABEL_SUITE=report-YYYYMMDD APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5`
  - `python scripts/bench/mem_combine.py snapshots/*/*`
- `python scripts/bench/plot_overhead.py combined/combined.csv charts`
  - To cap the two wide legacy bar charts (Control Plane PSS, System cgroups) to the most recent N samples, pass `--latest N` or set `PLOT_LATEST=N` (default 60): `PLOT_LATEST=40 python scripts/bench/plot_overhead.py combined/combined.csv charts`
  - `python docs/build_docs.py`
  - Quick fix for labels: backfill the detected OCI runtime into your latest snapshots and regenerate charts with one command: `make bench-mem-backfill-oci-latest REBUILD_DOCS=1`
  - Tip: for accurate process PSS on dockerd/containerd/podman, prefer privileged snapshots:
    - Matrix: `./scripts/bench/run_matrix.sh --label-suite baseline --app specs/examples/echo.yaml --replicas 1,5,10 --duration 30 --sudo`
    - Rollout: `./scripts/bench/run_rollout_k1s.sh --label-suite baseline-roll --app specs/examples/echo.yaml --replicas 5 --duration 30 --sudo`
- Preflights:
  - Podman must pass `podman info`; if not, start `podman.socket`, enable lingering with `loginctl enable-linger "$USER"`, and run `podman system migrate`.
  - If not using plaintext secrets, ensure `sops --decrypt` works; scripts fail fast with guidance otherwise.
- Outputs: `combined/combined.csv` and `charts/*.png`. The testing-memory-k1s page auto‑shows the latest results.
