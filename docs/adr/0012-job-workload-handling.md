# ADR 0012 - Job workload handling for apishim jobs

Date: 2026-01-16
Status: Proposed
Owners: runtime/controller/apishim

## Context
- Apishim jobs (for example, Helm demo jobs) are currently treated like long-running deployments.
- This causes missing logs (pod command/args/env not mapped), restart loops (restart policy keeps re-running), and dashboards that remain "progressing" even after completion.
- We need run-to-completion semantics for jobs without changing service workloads.

## Decision
- Introduce explicit job semantics in the app model via `AppSpec.workload`, defaulting to "service".
- Map job pod template fields into `AppSpec` when converting Jobs (command, args, env, workingDir, resources, probes, securityContext).
- Track per-replica exit codes and completion timestamps in runtime state.
- Adjust runtime behavior for jobs to avoid restarting successful containers and to honor backoff limits on failures.
- Update reconciler and apishim job status synthesis to report succeeded/failed/active correctly.

## Options Considered
1) **Keep treating Jobs like Deployments**: simplest but leaves missing logs, restart loops, and incorrect status.
2) **Create a separate Job model**: clean separation but adds a parallel spec surface and doubles tooling effort.
3) **Add workload semantics to AppSpec (chosen)**: minimal API surface change and keeps existing controller/runtime flows.

## Consequences
- AppSpec gains workload metadata and job-specific hints.
- Runtime adapters must populate exit codes/finish timestamps and adjust restart policy for jobs.
- Job parity remains scoped (no full CronJob/activeDeadlineSeconds support).

## Action Plan
1) Extend `AppSpec` with workload and optional job backoff/TTL hints.
2) Map job pod template fields when converting from apishim Jobs.
3) Add exit code + finished_at plumbing in runtime adapters and state store.
4) Make job-aware status computation in reconciler and apishim job status synthesis.
5) Add unit tests for job conversion, exit-code handling, and status transitions.
6) Validate via Helm demo and dashboard updates.

## Open Questions
- Should jobs have a distinct "completed" revision status instead of reusing "ready"?
- Should failures surface immediately or only after backoff is exhausted?
- Should TTL cleanup be enabled by default for apishim jobs?
