# ADR 0009 — API shim storage and watch scalability

Date: 2026-01-14
Status: Proposed
Owners: runtime/controller

## Context
- The API shim currently uses a local SQLite database (apishim.db) for all Kubernetes object storage. This blocks HA deployments and can drift from controller state.
- Watch streams are in-process only; there is no per-resource backpressure, timeout enforcement, or metrics. Under churn we risk dropped events and silent stalls.
- Phase 6 calls for moving shim storage to a shared backend and adding reliability signals.

## Decision
- Standardize shim object storage on the shared controller database backed by Postgres when available, with SQLite kept for single-node/dev.
- Introduce a storage abstraction so the shim can target either SQLite (dev) or Postgres (HA) via a DSN (`AE_APISHIM_DSN`) and migrate schemas automatically.
- Add per-resource watch queues with bounded depth + timeouts, and emit metrics for queue depth, watch duration, reconnect count, and dropped events.

## Options Considered
1) **Keep local SQLite**: simplest, but no HA and drift risk; rejected for Phase 6 goals.
2) **Reuse controller SQLite file over a shared mount**: avoids new backend but unsafe under concurrent writers; still single-host.
3) **Move to Postgres (preferred)**: enables HA shims, concurrent writers, and durable queues; operational overhead acceptable for Phase 6.

## Consequences
- New dependency on Postgres for HA scenarios; dev remains zero-install with SQLite.
- Need migration tooling to move existing `apishim.db` contents into Postgres (one-time CLI).
- Watch handling will surface metrics and fail-fast on backpressure instead of silent stalls.

## Action Plan
1) Add a storage backend interface for ObjectStore/StateStore with drivers for SQLite (existing) and Postgres.
2) Implement DSN selection: `AE_APISHIM_DSN` (Postgres URI) switches the shim; default remains SQLite.
3) Provide a migration CLI (`ae.apishim migrate --from sqlite --to $DSN`) to import existing objects and preserve resourceVersion.
4) Add watch queues per (group/version/resource) with:
   - bounded queue length + drop counters,
   - heartbeat/timeout on each watch stream,
   - metrics: queue_depth, events_dropped_total, watch_reconnect_total, watch_duration_seconds.
5) Expose metrics over the shim’s `/metrics`; add Grafana panel template for shim health.
6) Add HA smoke test: two shim instances against shared Postgres, verify watch continuity during restart.

## Open Questions
- Should controller move to the same Postgres instance simultaneously or remain SQLite with periodic sync? (leaning shared DB for both.)
- Do we gate Postgres usage behind a feature flag or auto-detect DSN presence? Proposed: DSN opt-in for now.
- What is the minimal migration story for managed offerings (RDS, Cloud SQL) vs. local Docker Postgres?
