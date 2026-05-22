# Store

- Source: `apishim/store.py`
- Last reviewed: 2026-05-13
- Size: 756 lines

## Purpose
SQLite/Postgres-backed object store with watch support for the API shim.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| K8sObject | 34 | No class docstring. |  |
| ObjectStore | 46 | SQLite-backed storage for k8s-like objects. | public methods: close, upsert, upsert_if_not_deleted, get, list, list_all, delete, watch, export_all, render_metrics |

## Runtime And Data Flow
- Internal dependencies: `ae.resources`
- External libraries: `psycopg`
- Environment inputs: `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_APISHIM_TOMBSTONE_TTL`, `AE_APISHIM_WATCH_OUTBOX_BATCH`, `AE_APISHIM_WATCH_OUTBOX_CLEANUP`, `AE_APISHIM_WATCH_OUTBOX_POLL`, `AE_APISHIM_WATCH_OUTBOX_TTL`, `AE_APISHIM_WATCH_QUEUE_SIZE`
- Side-effect surfaces: filesystem/state.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/integration/test_agent_service_proxy.py`
- `tests/integration/test_apishim_persistence.py`
- `tests/integration/test_envoy_core_local_ingress_tls.py`
- `tests/integration/test_etcd_state_adapter.py`
- `tests/integration/test_multinode_agent_flow.py`
- `tests/integration/test_overlay_vip.py`
- `tests/unit/test_agent_api.py`
- `tests/unit/test_apishim_ha_crd_authority.py`
- `tests/unit/test_apishim_ha_mode.py`
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_apishim_ha_store.py`
- `tests/unit/test_apishim_ha_workload_authority.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_apishim_patch.py`
