# Benchmarks & Tests

Use these guides to profile memory and compare k1s with k3s.

- [k1s Memory Profiling (General)](testing-memory-k1s.html)
- [Benchmarking k1s vs k3s (In‑Depth)](benchmark-k3s.html)

For quick commands and charts, see the end‑to‑end Make targets in each guide.

## Runbook: Quick Start
- Set env: `PYTHONPATH=src`, `AE_RUNTIME_BACKEND=podman` (or `docker`), `AE_ALLOW_PLAINTEXT_SECRETS=1` for demos.
- Keep controller running (or let scripts auto‑start): `python -m ae.controller --loop --specs specs/ --watch`.
- Build or ensure images are present (Podman shown):
  - `podman build -t localhost/demo-blue:latest samples/servers/blue`
  - `podman build -t localhost/demo-green:latest samples/servers/green`
- Run end‑to‑end and generate charts:
  - `make bench-mem-e2e-k1s LABEL_SUITE=report-YYYYMMDD APP=specs/examples/echo.yaml REPLICAS=1,5,10 DURATION=30 ROLL_REPLICAS=5`
  - `python scripts/bench/mem_combine.py snapshots/*/*`
  - `python scripts/bench/plot_overhead.py combined/combined.csv charts`
  - `python docs/build_docs.py`
- Preflights:
  - Podman must pass `podman info`; if not, start `podman.socket`, enable lingering with `loginctl enable-linger "$USER"`, and run `podman system migrate`.
  - If not using plaintext secrets, ensure `sops --decrypt` works; scripts fail fast with guidance otherwise.
- Outputs: `combined/combined.csv` and `charts/*.png`. The testing-memory-k1s page auto‑shows the latest results.
