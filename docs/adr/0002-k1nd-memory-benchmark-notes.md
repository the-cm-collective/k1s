# ADR 0002 — k1nd memory benchmark run notes (2025-11-12)

Date: 2025-11-12
Status: Informational
Owners: bench

## Context
- An end-to-end memory benchmark run was executed on 2025-11-12 against k1nd (k1s-in-Docker via `ops/dev/labs-aio.yaml`).
- The run produced the baseline snapshot set for `r20251112+docker+k1nd`.

## Decision
- Record the run as a baseline and carry forward a short list of follow-up fixes for accuracy and repeatability.

## Key Findings
- Docker seccomp mapping: `RuntimeDefault` mapped to `seccomp=runtime/default` caused Docker errors; the run used `Unconfined` as a workaround.
- SOPS missing: secret projection errors were emitted but did not block the app.
- Old revisions: stale replicas inflated 1/5/10 pod snapshots, flattening memory deltas.
- PSS undercount: snapshots were collected without sudo, so process PSS totals were incomplete.

## Follow-ups
- Adjust Docker runtime seccomp mapping to omit `security_opt` for `RuntimeDefault`.
- Add an explicit prune/cleanup step between benchmark phases.
- Prefer running snapshots with sudo (or document cgroup-bytes as the primary metric in non-root runs).
- Normalize benchmark metadata so backend/engine labels are consistent.
