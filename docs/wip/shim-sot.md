# Shim Store Source-of-Truth: Reset Drift + Path to B

Date: 2026-01-17

## Context
Playground/Labs reset deletes demo apps, but they reappear shortly after. Logs show the reset handler deleting shim objects directly from the apishim DB while the running shim server still has in-memory watch state. The adapter then re-registers apps, causing reconcile drift.

## Root Cause
- `/labs/reset` deletes shim objects using a *new* `ObjectStore` instance pointed at the DB.
- The running apishim server owns watch queues in-memory; direct DB deletes do **not** publish watch events.
- The apishim adapter then re-creates controller registry entries from the still-running shim server, and the controller reconciles them again.

## Short-Term Fix (Path Through Shim Server)
Goal: ensure deletes are performed by the running shim server so watch events fire.

Plan:
- In `/labs/reset`, attempt to delete resources via the shim API (using `AE_LABS_HELM_SERVER` + token).
- Only fall back to direct DB deletion if the shim server is unreachable.
- This removes the drift by keeping the shim store, watch queues, and adapter in sync.

Notes:
- Keep the existing list of resource kinds (deployments, daemonsets, statefulsets, jobs, services, configmaps, ingresses, HPAs, etc.).
- Use the local dev CA bundle if present (`state/certs/combined-dev-ca.pem`) and allow both http/https.

## Move Toward Option B (Shim Store as SoT)
Phased path that minimizes churn:

1) Shim-mirror registry (low risk)
- Controller continues to reconcile from `app_registry`, but registry entries are sourced from shim objects.
- A sync loop maps shim resources to `AppManifest` and writes `source="apishim"` into the registry.

2) Single reconciler
- Disable apishim adapter reconcile; controller loop watches shim store (or shim API) directly.
- `app_registry` becomes a cache/derived view, not authoritative.

3) Unified write path
- CLI/specs write into shim store (or shim API); controller reads from shim store.
- Registry can be deprecated or retained as a derived view for UI/reporting.

## Open Questions
- Should reset also remove the shim namespace object or just its namespaced resources?
- Do we need a bulk-delete shim endpoint for performance or policy control?
