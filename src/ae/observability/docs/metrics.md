# Metrics

- Source: `observability/metrics.py`
- Last reviewed: 2026-05-13
- Size: 262 lines

## Purpose
Metrics helpers derived from state store snapshots.

## Public Surface And Internal Entry Points
### Classes
| Class | Line | Summary | Notes |
| --- | --- | --- | --- |
| MetricsSnapshot | 14 | No class docstring. |  |
| MetricsService | 38 | Aggregates metrics from application status records. | public methods: snapshot |

### Top-Level Functions
| Function | Line | Kind | Summary |
| --- | --- | --- | --- |
| _pv_host_backing | 188 | function | Internal helper. |
| _pvc_requested_storage | 212 | function | Internal helper. |
| _quantity_bytes | 224 | function | Internal helper. |

## Runtime And Data Flow
- Internal dependencies: `ae.apishim.store`, `ae.controller.state`, `ae.storage.config`
- Environment inputs: `AE_APISHIM_DB`, `AE_APISHIM_DSN`, `AE_NODE_NOTREADY_AFTER`, `AE_STORAGE_QUOTAS`
- Side-effect surfaces: filesystem/state, network/API.

## Maintenance Notes
No explicit deprecated/TODO/legacy/fallback/workaround markers were found in this module during static review.

## Related Tests And Docs
- `tests/e2e/ha_closeout.py`
- `tests/integration/test_reconcile_flow.py`
- `tests/unit/test_apishim_ha_passive_authority.py`
- `tests/unit/test_apishim_hpa.py`
- `tests/unit/test_apishim_store_metrics.py`
- `tests/unit/test_cli.py`
- `tests/unit/test_cri_runtime_workload_metrics.py`
- `tests/unit/test_ha_core_drills_script.py`
- `tests/unit/test_ha_core_upgrade_script.py`
- `tests/unit/test_ha_edge_transport_script.py`
- `tests/unit/test_ha_transport_upgrade_script.py`
- `tests/unit/test_hpa_authority.py`
- `tests/unit/test_k8s_exporter.py`
- `tests/unit/test_lab_vm_tools.py`
- `tests/unit/test_metrics_per_app.py`
- `tests/unit/test_microk8s_helm_contracts.py`
