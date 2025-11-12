# k1nd E2E Benchmark – Run Notes (r20251112+docker+k1nd)

This documents the end-to-end memory benchmark run against “k1nd” (k1s-in-Docker via the labs AIO compose stack) on 2025-11-12.

## Overview
- Suite label: `r20251112+docker+k1nd`
- Controller + Caddy via Docker Compose (`ops/dev/labs-aio.yaml`).
- Snapshots collected with `AE_COLLECT_ENGINE=docker` to read container cgroup memory from Docker.
- Combined results generated into `combined/combined.csv` and `combined/combined.json`.

## Environment & Preflights
- Docker available; Docker Compose v2.
- Local Python venv created at `.venv-bench` and project installed editable so `python -m ae.cli` works on host.
- Port 9108 was occupied initially; resolved with `scripts/stop_all.sh` then `make labs-aio-up`.
- Secrets projection errors observed (SOPS missing); did not block app but emitted events.
- To avoid a Docker seccomp error during container start, used an “unconfined” security profile for echo in this run (see “Issues Found”).

## What Ran
1. Bring up the k1nd stack: `make bench-mem-e2e-k1nd` (initial attempt hit 9108 conflict and CLI missing).
2. Manual sequence (equivalent to the Make target):
   - `docker compose -f ops/dev/labs-aio.yaml up -d`
   - Use host venv for CLI: `. .venv-bench/bin/activate`
   - Apply echo manifest with `seccompProfileType: Unconfined` written to `state/echo-unconfined.yaml`.
   - Scale echo to 1, 5, 10 with readiness waits, snapshot 30s after each.
   - Rollout: change `MESSAGE` to force a new revision and snapshot during and after stabilization.
   - Combine snapshots: `python scripts/bench/mem_combine.py snapshots/*/*`.

Key scripts used are in `scripts/bench/`: `run_matrix.sh`, `run_rollout_k1s.sh`, `mem_snapshot.sh`.

## Results (most recent per label)
Pulled from `combined/combined.csv` lines ~272–288 for this suite.
- idle: `app_mem_bytes ≈ 293.6 MiB`
- pods-1: `≈ 293.6 MiB`
- pods-5: `≈ 293.6 MiB`
- pods-10: `≈ 293.6 MiB`
- rollout-5-during: `≈ 376.3 MiB`
- rollout-5-post: `≈ 376.5 MiB`

Notes on interpretation:
- The flat 1/5/10 numbers indicate other echo containers from prior revisions remained running during some snapshots, keeping the total roughly constant. See “Clean older revisions” in Next Steps.

## Artifacts
- Snapshots: `snapshots/r20251112+docker+k1nd-*/*/`
- Combined CSV/JSON: `combined/combined.csv`, `combined/combined.json`
- Example container memory sample: `snapshots/r20251112+docker+k1nd-pods-10/<ts>/raw/containers_mem.csv`
- Charts were not generated (matplotlib not installed).

## Issues Found
- Docker seccomp mapping error:
  - Error: `Decoding seccomp profile failed: invalid character 'r' looking for beginning of value` when starting containers.
  - Cause: Our Docker backend maps `seccompProfileType: RuntimeDefault` to `security_opt=["seccomp=runtime/default"]`. Docker expects either `seccomp=unconfined` or a path to a JSON profile; it does not accept the Kubernetes token `runtime/default`.
  - Workaround used here: switch echo manifest to `seccompProfileType: Unconfined` (in `state/echo-unconfined.yaml`).
  - Follow-up: In `src/ae/runtime/docker_runtime.py`, change mapping so RuntimeDefault results in no explicit seccomp opt (let Docker apply its default), or ship a valid JSON profile file and reference its path.
- Secrets projection failures:
  - Events showed `SecretError: SOPS binary not found and plaintext secrets are disabled`.
  - Impact: app still ran, but config/secret projections were skipped.
- Old revisions present during snapshots:
  - Multiple echo revisions (rev1–rev4) appeared in `containers_mem.csv` for `pods-10`. That keeps totals steady across 1/5/10.
  - Hypothesis: previous attempts created stray revisions; subsequent scale steps didn’t prune them. The Docker runtime has `remove_old_revisions` but the sequence did not trigger cleanup between certain transitions.
- Engine labeling quirks:
  - Snapshot meta recorded `backend=podman` (auto-detect), but `engine_filter=docker` confirms metrics were read from Docker. This is cosmetic; still worth normalizing for clarity.
- Charts skipped:
  - `plot_overhead.py` requires matplotlib; not present in the venv.

- Low process PSS totals (~1 MiB) in CSV:
  - Observation: `process_pss_kb` around ~1064 KiB for k1nd snapshots.
  - Cause: snapshots ran without sudo; reading `/proc/<pid>/smaps_rollup` for containerized root-owned processes is denied to unprivileged users, resulting in incomplete PSS accounting. The aggregator sums readable processes only.
  - Mitigation: run snapshots with `--sudo` (supported by `run_matrix.sh` and `run_rollout_k1s.sh`) or rely on container cgroup bytes (`app_mem_bytes` and `system_mem_bytes`) for accurate totals in non-root runs.

## Next Steps
- Fix seccomp mapping for Docker backend
  - Map `RuntimeDefault` → omit `security_opt` (let Docker default apply), or provide a JSON profile path instead of `runtime/default`.
  - Keep support for `Unconfined` and `Localhost` (JSON path) as-is.
- Secrets: enable plaintext for dev or install SOPS
  - Quick local: export `AE_ALLOW_PLAINTEXT_SECRETS=1` in the controller container env (compose) during benchmarks.
  - Proper: install and configure `sops` so secretRefs work in benchmarks.
- Ensure old revisions are cleaned between steps
  - Add an explicit prune in the benchmark flow (e.g., a helper invoking the runtime’s `remove_old_revisions`, or a best-effort `docker rm` by `ae.app=<name>` label for non-current revisions) after rollouts and before the 1/5/10 matrix.
  - Alternatively, run a fresh stack per replica step to isolate measurements.
- Normalize snapshot meta
  - Set `AE_RUNTIME_BACKEND=docker` in the environment that runs `mem_snapshot.sh` so `backend` reflects Docker, matching `engine_filter`.
- Charts
  - Add `matplotlib` to the dev extras or install in the bench venv to produce plots with `python scripts/bench/plot_overhead.py combined/combined.csv charts`.
- Ports and orchestrator startup
  - Keep using `scripts/stop_all.sh` before `labs-aio-up`, or parameterize API port via `API_PORT` to avoid :9108 conflicts.
 - Process PSS accuracy
   - Re-run snapshots with `--sudo` to capture full PSS (or set `ALLOW_SUDO=1 make bench-mem-e2e-baselines-sudo`).

## Repro Cheatsheet
- Prepare venv and install: `. .venv-bench/bin/activate && python -m pip install -e .[dev]`
- Start stack: `docker compose -f ops/dev/labs-aio.yaml up -d`
- Apply echo with unconfined seccomp: `docker exec dev-controller-1 python -m ae.cli apply -f state/echo-unconfined.yaml`
- Scale and snapshot: use `scripts/bench/run_matrix.sh` with `AE_RUNTIME_BACKEND=docker SKIP_GUARDS=1`, or manually:
  - `docker exec dev-controller-1 python -m ae.cli scale echo --replicas 1|5|10` with readiness wait, then `./scripts/bench/mem_snapshot.sh --mode k1s --label r20251112+docker+k1nd-pods-<N> --duration 30`
- Rollout: apply `state/echo-rollout.yaml`, then snapshot `...-rollout-5-during` and `...-rollout-5-post`.
- Combine: `python scripts/bench/mem_combine.py snapshots/*/*`
- Plot (optional): `python scripts/bench/plot_overhead.py combined/combined.csv charts`

## Open Questions
- Should the controller/runtime proactively prune old revisions on every reconcile to simplify bench runs, or should pruning remain an explicit action in bench scripts?
- Do we want a built-in `ae prune <app>` subcommand that wraps `remove_old_revisions` for scripts to call?
- Where should we host a canonical Docker seccomp JSON profile if we decide to enforce a non-default policy?
