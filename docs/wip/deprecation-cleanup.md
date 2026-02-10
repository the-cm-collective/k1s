# Deprecation Cleanup Plan

This document tracks deprecated and legacy paths we plan to remove. Items 1–3 are
active cleanup work; items 4–7 are noted for future removal.

## Active Cleanup (Execute Now)

### 1) Remove deprecated `kind: App`

Goal:
- Require `kind: Deployment` for `ae.dev/v1alpha1` manifests.

Plan:
- Update examples/docs/tests to use `kind: Deployment`.
- Remove CLI/controller warnings and acceptance of `App`.
- Update error messages that reference “Deployment/App”.

Acceptance:
- `kind: App` is rejected by the parser.
- All shipped docs/examples use `kind: Deployment`.

### 2) Remove replica-named state APIs

Goal:
- Remove `list_replicas`, `list_replica_nodes`, `set_replica_nodes`.

Plan:
- Update apishim server to call `list_pod_nodes` only.
- Remove replica alias methods from SQLite and etcd state stores.

Acceptance:
- No references to replica-named state methods.
- Pod placement data still resolves via `list_pod_nodes`.

### 3) Remove legacy `storage_bindings` fallback

Goal:
- Stop reading/writing legacy `storage_bindings` data.

Plan:
- Remove fallback lookup in `list_volume_attachments`.
- Remove `storage_bindings` schema creation and migration.
- Remove legacy helper methods and SQL resources.

Acceptance:
- Only `volume_attachments` is used for storage placement.
- No references to `storage_bindings` remain in code or SQL resources.

## Backlog (Document Only)

### 4) Deprecated CLI flag
- `k8s-check --strict` is deprecated in favor of `--policy strict`.

### 5) Deprecated metric aliases
- Remove `ae_replica_ready` and other replica alias metrics after dashboards migrate.

### 6) Legacy scripts / CA handling
- `scripts/dev/ensure_dev_local.sh` retains legacy CA path fallback.
- `scripts/bench/run_all_baselines.sh` references deprecated legacy k1nd compose.

### 7) Legacy label scan fallback in docker runtime
- `docker_runtime.py` still scans legacy labels for containers.

## Validation
- Update and run unit tests that previously referenced `kind: App`.
- Ensure apishim pod placement still resolves via `list_pod_nodes`.

## Rollout Notes
- If users still rely on `kind: App`, this change is breaking.
- Consider a release note highlighting the removal and migration guidance.
