# Etcd State

- Source: `controller/etcd_state.py`
- Last reviewed: 2026-05-13
- Size: 2960 lines

## Purpose
Provides classes EtcdHttpClient, EtcdStateStore within Core control plane: manifest loading, reconcile loop, state stores, scheduling, HA authority, and workload controllers.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| EtcdHttpClient | 120 | public methods: range, put, delete, delete_prefix, txn, grant_lease, revoke_lease, maintenance_status, maintenance_alarms, compact ... | public methods: range, put, delete, delete_prefix, txn, grant_lease, revoke_lease, maintenance_status, maintenance_alarms, compact ... |
| EtcdStateStore | 353 | Etcd-backed state store (dev-etcd). | public methods: register_inference_cell, get_inference_cell, list_inference_cells, update_inference_cell_status, delete_inference_cell, record_inference_cell_event, list_inference_cell_events, register_inference_cellset, get_inference_cellset, list_inference_cellsets ... |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _b64encode | 62 | function | Internal helper. |
| _b64decode | 67 | function | Internal helper. |
| _prefix_end | 73 | function | Internal helper. |
| _now | 84 | function | Internal helper. |
| _now_iso | 88 | function | Internal helper. |
| _dt_from_iso | 92 | function | Internal helper. |
| _parse_duration_seconds | 101 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.accelerators`, `ae.controller.health`, `ae.controller.spec`, `ae.controller.state`, `ae.ha.fencing`, `ae.observability.http_api`, `ae.runtime`
- External libraries: `requests`
- Environment inputs: `AE_ETCD_MAINTENANCE_THRESHOLD_PCT`, `AE_ETCD_QUOTA_BACKEND_BYTES`, `AE_ETCD_RETRY_BACKOFF`, `AE_ETCD_RETRY_JITTER`, `AE_ETCD_RETRY_MAX`, `AE_POD_NODE_TTL_SECONDS`, `AE_POD_STATUS_TTL_SECONDS`
- Side-effect surfaces: network/API.

## Maintenance Notes
Static review found lines worth revisiting during future refactors:
- Line 214: `# Fallback probing is only for API discovery. If we already saw a`
- Line 216: `# masking it with prefix fallback noise.`

## Related Tests And Docs
- `tests/e2e/core_edge.py`
- `tests/e2e/ha_closeout.py`
- `tests/integration/test_etcd_state_adapter.py`
- `tests/unit/test_app_ingress.py`
- `tests/unit/test_etcd_http_client.py`
- `tests/unit/test_etcd_inference_state.py`
- `tests/unit/test_etcd_site_ingress_endpoint_state.py`
- `tests/unit/test_etcd_state_maintenance.py`
- `tests/unit/test_route_bundle_sites.py`
- `tests/unit/test_workload_metrics_state.py`
